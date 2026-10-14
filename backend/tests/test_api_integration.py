"""集成测试：真实的 PostgreSQL / Redis / RabbitMQ + 完整的应用（含 lifespan）。

运行：RUN_INTEGRATION=1 pytest -q（需要先 alembic upgrade head；种子数据由 conftest 自动写入）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aio_pika
import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect
from yarl import URL

from app import state_machine as sm
from app.config import get_settings
from app.db import create_engine, create_sessionmaker
from app.deps import Services
from app.models import Command, CommandEvent
from app.mq import EXPIRED_QUEUE, robot_queue_name
from app.redis_bus import online_key, ws_ticket_key
from app.security import create_access_token
from app.sweeper import SweepLimits, sweep_stuck_commands

pytestmark = pytest.mark.integration


def wait_for[T](fetch: Callable[[], T | None], timeout_s: float = 5.0) -> T:
    """反复调用 fetch，直到它返回真值（异步流程需要一点时间才能在接口上可见）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = fetch()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"condition not met within {timeout_s}s")


def run_db[T](scenario: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
    """在测试线程里用一个独立的引擎跑一段数据库操作（应用自己的引擎属于另一个事件循环）。"""

    async def main() -> T:
        engine = create_engine(get_settings().database_url)
        try:
            return await scenario(create_sessionmaker(engine))
        finally:
            await engine.dispose()

    return asyncio.run(main())


@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    client.close()


def services_of(client: TestClient) -> Services:
    services: Services = client.app.state.services  # type: ignore[attr-defined]
    return services


def robot_ws(client: TestClient, robot_id: str) -> WebSocketTestSession:
    """机器人用 Authorization 头认证（每次给一个新字典：TestClient 会往里面添加握手头）。"""
    token = f"dev-token-robot-{robot_id.rsplit('-', 1)[1]}"
    return client.websocket_connect(
        f"/ws/robot?robot_id={robot_id}", headers={"Authorization": f"Bearer {token}"}
    )


def issue_ticket(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/ws-ticket", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == 30
    ticket: str = body["ticket"]
    return ticket


def expect_close(ws: WebSocketTestSession) -> int:
    """一直读到服务端关闭连接为止（期间可能夹杂着其他事件），返回关闭码。"""
    with pytest.raises(WebSocketDisconnect) as closed:
        while True:
            ws.receive_json()
    return closed.value.code


# --------------------------------------------------------------------------- 健康检查 / 中间件


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"db": "ok", "redis": "ok", "rabbitmq": "ok"}
    assert response.headers["X-Replica"] == body["replica"]
    assert response.headers["X-Request-ID"]


def test_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/api/health", headers={"X-Request-ID": "it-req-123"})
    assert response.headers["X-Request-ID"] == "it-req-123"


def test_openapi_docs_are_served_under_api(client: TestClient) -> None:
    # Nginx 只把 /api/ 转给后端，文档必须在 /api 下才能通过 http://localhost:8080 打开
    schema = client.get("/api/openapi.json")
    assert schema.status_code == 200
    assert "/api/ws-ticket" in schema.json()["paths"]
    docs = client.get("/api/docs")
    assert docs.status_code == 200
    assert "/api/openapi.json" in docs.text
    assert client.get("/docs").status_code == 404


# --------------------------------------------------------------------------- 认证


def test_login_ok(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["access_token"].count(".") == 2
    assert body["expires_at"]


@pytest.mark.parametrize(
    "credentials",
    [{"username": "admin", "password": "wrong"}, {"username": "nobody", "password": "admin123"}],
)
def test_login_bad_credentials(client: TestClient, credentials: dict[str, str]) -> None:
    response = client.post("/api/auth/login", json=credentials)
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


def test_me(client: TestClient, viewer_headers: dict[str, str]) -> None:
    response = client.get("/api/me", headers=viewer_headers)
    assert response.status_code == 200
    assert response.json() == {"username": "viewer", "role": "viewer"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}])
def test_auth_required(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get("/api/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --------------------------------------------------------------------------- 机器人


def test_list_robots(client: TestClient, viewer_headers: dict[str, str]) -> None:
    response = client.get("/api/robots", headers=viewer_headers)
    assert response.status_code == 200, response.text
    robots = {robot["id"]: robot for robot in response.json()}
    assert {"robot-1", "robot-2", "robot-3", "robot-4"} <= set(robots)
    assert robots["robot-1"]["name"] == "AGV-01"
    for robot in robots.values():
        assert set(robot) == {"id", "name", "online", "replica", "last_seen", "state"}
        assert isinstance(robot["online"], bool)


def test_unknown_robot_is_404(client: TestClient, viewer_headers: dict[str, str]) -> None:
    assert client.get("/api/robots/robot-404", headers=viewer_headers).status_code == 404


# --------------------------------------------------------------------------- 命令


def test_admin_creates_command_and_key_replay_returns_same_id(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    headers = {**admin_headers, "Idempotency-Key": str(uuid.uuid4())}
    body = {"type": "move_to", "params": {"x": 1.5, "y": -2}}

    first = client.post("/api/robots/robot-1/commands", json=body, headers=headers)
    assert first.status_code == 202, first.text
    created = first.json()
    assert created["robot_id"] == "robot-1"
    assert created["type"] == "move_to"
    assert created["params"] == {"x": 1.5, "y": -2.0}
    assert created["status"] == "queued"
    assert created["attempts"] == 0
    assert created["created_by"] == "admin"

    replay = client.post("/api/robots/robot-1/commands", json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == created["id"]

    detail = client.get(f"/api/commands/{created['id']}", headers=admin_headers)
    assert detail.status_code == 200
    first_event = detail.json()["events"][0]
    assert first_event["from_status"] is None
    assert first_event["to_status"] == "queued"

    listing = client.get("/api/commands?robot_id=robot-1&limit=200", headers=admin_headers)
    assert created["id"] in {command["id"] for command in listing.json()}


def test_commands_without_key_are_never_deduplicated(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    body = {"type": "stop", "params": {}}
    first = client.post("/api/robots/robot-1/commands", json=body, headers=admin_headers)
    second = client.post("/api/robots/robot-1/commands", json=body, headers=admin_headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]


def test_viewer_cannot_create_command(client: TestClient, viewer_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/robots/robot-1/commands",
        json={"type": "stop", "params": {}},
        headers=viewer_headers,
    )
    assert response.status_code == 403


def test_invalid_command_params_are_422(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/robots/robot-1/commands",
        json={"type": "move_to", "params": {"x": 51, "y": 0}},
        headers=admin_headers,
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_command_for_unknown_robot_is_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/robots/robot-404/commands",
        json={"type": "stop", "params": {}},
        headers=admin_headers,
    )
    assert response.status_code == 404


def test_unknown_command_is_404(client: TestClient, viewer_headers: dict[str, str]) -> None:
    response = client.get(f"/api/commands/{uuid.uuid4()}", headers=viewer_headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- WebSocket 认证


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("/ws/robot?robot_id=robot-1", {"Authorization": "Bearer wrong"}),
        ("/ws/robot?robot_id=robot-1", {"Authorization": "Basic dev-token-robot-1"}),
        ("/ws/robot?robot_id=robot-1", {}),
        ("/ws/robot", {"Authorization": "Bearer dev-token-robot-1"}),
        # 查询参数里的 token 不再被接受：凭证不能出现在 URL 里
        ("/ws/robot?robot_id=robot-1&token=dev-token-robot-1", {}),
        ("/ws/dashboard?ticket=not-a-ticket", {}),
        ("/ws/dashboard", {}),
    ],
)
def test_websocket_auth_failure_closes_with_4401(
    client: TestClient, url: str, headers: dict[str, str]
) -> None:
    with client.websocket_connect(url, headers=dict(headers)) as ws:
        assert expect_close(ws) == 4401


def test_dashboard_jwt_in_query_is_not_accepted(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    jwt_token = viewer_headers["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect(f"/ws/dashboard?token={jwt_token}") as ws:
        assert expect_close(ws) == 4401


def test_ws_ticket_requires_a_valid_jwt(client: TestClient) -> None:
    assert client.post("/api/ws-ticket").status_code == 401
    bad = client.post("/api/ws-ticket", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401


def test_ws_ticket_is_stored_with_a_30s_ttl(
    client: TestClient, viewer_headers: dict[str, str], redis_client: redis.Redis
) -> None:
    ticket = issue_ticket(client, viewer_headers)
    assert 0 < int(redis_client.ttl(ws_ticket_key(ticket))) <= 30  # type: ignore[arg-type]


def test_dashboard_ticket_works_once(client: TestClient, viewer_headers: dict[str, str]) -> None:
    ticket = issue_ticket(client, viewer_headers)
    with client.websocket_connect(f"/ws/dashboard?ticket={ticket}") as ws:
        ws.send_json({"type": "ping"})
        for _ in range(200):  # 期间可能夹杂着其他事件，读到 pong 为止
            if ws.receive_json() == {"type": "pong"}:
                break
        else:
            pytest.fail("no pong received")
    # 同一张票据第二次使用：已被 GETDEL 删除
    with client.websocket_connect(f"/ws/dashboard?ticket={ticket}") as ws:
        assert expect_close(ws) == 4401


def test_expired_dashboard_ticket_is_rejected(
    client: TestClient, viewer_headers: dict[str, str], redis_client: redis.Redis
) -> None:
    ticket = issue_ticket(client, viewer_headers)
    redis_client.pexpire(ws_ticket_key(ticket), 1)  # 让 Redis 立刻过期它，而不是真等 30 秒
    time.sleep(0.05)
    with client.websocket_connect(f"/ws/dashboard?ticket={ticket}") as ws:
        assert expect_close(ws) == 4401


def test_dashboard_socket_is_closed_when_the_jwt_expires(client: TestClient) -> None:
    # 签一个大约 2~3 秒后过期的 JWT 去换票：连接不能比 JWT 活得更久
    settings = get_settings()
    token, expires_at = create_access_token(
        "viewer",
        "viewer",
        secret=settings.jwt_secret,
        expire_minutes=1,
        now=datetime.now(UTC) - timedelta(seconds=57),
    )
    ticket = issue_ticket(client, {"Authorization": f"Bearer {token}"})
    with client.websocket_connect(f"/ws/dashboard?ticket={ticket}") as ws:
        assert expect_close(ws) == 4401
    assert datetime.now(UTC) >= expires_at - timedelta(milliseconds=50)


# --------------------------------------------------------------------------- 机器人 WebSocket


def test_robot_socket_hello_telemetry_and_logs(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    def robot_state() -> dict[str, Any] | None:
        robot: dict[str, Any] = client.get("/api/robots/robot-3", headers=viewer_headers).json()
        return robot if robot["online"] and robot["state"] else None

    with robot_ws(client, "robot-3") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert isinstance(hello["server_time"], float)

        ws.send_text("this is not json")  # 非法消息只会被忽略，连接不受影响
        ws.send_json(
            {
                "type": "telemetry",
                "ts": time.time(),
                "x": 3.0,
                "y": 4.0,
                "theta": 0.1,
                "speed": 0.4,
                "battery": 66.0,
                "mode": "moving",
            }
        )
        robot = wait_for(robot_state)
        assert robot["replica"] == hello["replica"]
        assert robot["state"]["battery"] == 66.0
        assert robot["last_seen"]

        ws.send_json({"type": "log", "level": "info", "msg": "integration hello"})
        logs = wait_for(
            lambda: client.get("/api/robots/robot-3/logs?limit=5", headers=viewer_headers).json()
        )
        assert logs[0]["msg"] == "integration hello"
        assert logs[0]["level"] == "info"

        # 显式断开，并在 with 块内等待清理完成：TestClient 退出 with 时会先发 disconnect、
        # 紧接着取消服务端任务，来不及等处理器的 finally 跑完。
        ws.close(1000)
        wait_for(
            lambda: not client.get("/api/robots/robot-3", headers=viewer_headers).json()["online"]
        )


# --------------------------------------------------------------------------- 命令链路


def create_robot_4_command(
    client: TestClient, admin_headers: dict[str, str], body: dict[str, Any]
) -> str:
    headers = {**admin_headers, "Idempotency-Key": str(uuid.uuid4())}
    response = client.post("/api/robots/robot-4/commands", json=body, headers=headers)
    assert response.status_code == 202, response.text
    command_id: str = response.json()["id"]
    return command_id


def command_detail(client: TestClient, headers: dict[str, str], command_id: str) -> dict[str, Any]:
    response = client.get(f"/api/commands/{command_id}", headers=headers)
    assert response.status_code == 200, response.text
    detail: dict[str, Any] = response.json()
    return detail


def wait_for_command(
    client: TestClient,
    headers: dict[str, str],
    command_id: str,
    status: str,
    attempts: int | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    def fetch() -> dict[str, Any] | None:
        detail = command_detail(client, headers, command_id)
        if detail["status"] != status or attempts not in (None, detail["attempts"]):
            return None
        return detail

    return wait_for(fetch, timeout_s=timeout_s)


def receive_command(ws: WebSocketTestSession, command_id: str) -> dict[str, Any]:
    """读到指定的命令为止。排在它前面的旧命令先回 ack：prefetch=1，不 ack 就收不到下一条。"""
    while True:
        message: dict[str, Any] = ws.receive_json()
        if message["type"] != "command":
            continue
        if message["command_id"] == command_id:
            return message
        ws.send_json({"type": "ack", "command_id": message["command_id"]})


def disconnect_robot_4(
    client: TestClient, ws: WebSocketTestSession, viewer_headers: dict[str, str]
) -> None:
    # 同上：在 with 块内等服务端清理完（派发任务已停止 = channel 已关闭，未 ack 的消息回到队列）。
    ws.close(1000)
    wait_for(
        lambda: not client.get("/api/robots/robot-4", headers=viewer_headers).json()["online"]
    )


def transitions(detail: dict[str, Any]) -> list[tuple[str | None, str]]:
    return [(event["from_status"], event["to_status"]) for event in detail["events"]]


def finish_command(ws: WebSocketTestSession, command_id: str) -> None:
    ws.send_json({"type": "ack", "command_id": command_id})
    ws.send_json({"type": "result", "command_id": command_id, "status": "succeeded"})


def test_command_dispatch_ack_result_round_trip(
    client: TestClient, admin_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    with robot_ws(client, "robot-4") as ws:
        assert ws.receive_json()["type"] == "hello"
        body = {"type": "set_speed", "params": {"speed": 0.7}}
        command_id = create_robot_4_command(client, admin_headers, body)
        # 先等到 dispatched 再读 WebSocket：receive_json() 没有超时，
        # 链路坏了应该断言失败，而不是让测试卡住。
        wait_for_command(client, admin_headers, command_id, "dispatched")
        message = receive_command(ws, command_id)
        assert (message["command"], message["params"], message["attempt"]) == (
            "set_speed",
            {"speed": 0.7},
            1,
        )

        ws.send_json({"type": "ack", "command_id": command_id})
        wait_for_command(client, admin_headers, command_id, "acked")
        ws.send_json(
            {"type": "result", "command_id": command_id, "status": "succeeded", "detail": None}
        )
        ws.send_json({"type": "ack", "command_id": command_id})  # 迟到的重复 ack：状态机忽略
        wait_for_command(client, admin_headers, command_id, "succeeded")
        disconnect_robot_4(client, ws, viewer_headers)  # 之后上面的消息一定都已处理完

    detail = command_detail(client, admin_headers, command_id)
    assert detail["attempts"] == 1
    assert detail["dispatched_at"] and detail["acked_at"] and detail["finished_at"]
    assert transitions(detail) == [
        (None, "queued"),
        ("queued", "dispatched"),
        ("dispatched", "acked"),
        ("acked", "succeeded"),
    ]


def test_unacked_command_is_redelivered_after_reconnect(
    client: TestClient, admin_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    with robot_ws(client, "robot-4") as ws:
        assert ws.receive_json()["type"] == "hello"
        command_id = create_robot_4_command(client, admin_headers, {"type": "stop", "params": {}})
        wait_for_command(client, admin_headers, command_id, "dispatched")
        assert receive_command(ws, command_id)["attempt"] == 1
        # 收到了但不回 ack 就断线：RabbitMQ 里这条消息还没被确认，channel 关闭后它回到队列。
        disconnect_robot_4(client, ws, viewer_headers)

    with robot_ws(client, "robot-4") as ws:
        assert ws.receive_json()["type"] == "hello"
        wait_for_command(client, admin_headers, command_id, "dispatched", attempts=2)
        assert receive_command(ws, command_id)["attempt"] == 2  # 同一条命令被重投
        # 不发 ack、直接发结果：结果本身证明机器人收到了命令（隐含 ack）。
        ws.send_json(
            {
                "type": "result",
                "command_id": command_id,
                "status": "failed",
                "detail": "obstacle detected",
            }
        )
        wait_for_command(client, admin_headers, command_id, "failed")
        disconnect_robot_4(client, ws, viewer_headers)

    detail = command_detail(client, admin_headers, command_id)
    assert (detail["attempts"], detail["error"]) == (2, "obstacle detected")
    assert detail["acked_at"] is not None
    assert transitions(detail) == [
        (None, "queued"),
        ("queued", "dispatched"),
        ("dispatched", "dispatched"),
        ("dispatched", "failed"),
    ]


def test_stale_connection_yields_when_the_robot_is_online_elsewhere(
    client: TestClient,
    admin_headers: dict[str, str],
    viewer_headers: dict[str, str],
    redis_client: redis.Redis,
) -> None:
    with robot_ws(client, "robot-4") as ws:
        replica = ws.receive_json()["replica"]
        wait_for(lambda: redis_client.get(online_key("robot-4")) == replica)
        # 模拟"机器人已经重连到另一个副本"：那个副本改写了在线标记，本副本上这条连接成了过时的旧连接
        redis_client.set(online_key("robot-4"), "api-elsewhere", ex=60)
        command_id = create_robot_4_command(client, admin_headers, {"type": "stop", "params": {}})
        assert expect_close(ws) == 4409

    # 旧连接没有派发它：命令还在 queued，attempts 没有增加（不算一次失败）
    detail = wait_for_command(client, admin_headers, command_id, "queued", attempts=0)
    assert transitions(detail) == [(None, "queued")]

    with robot_ws(client, "robot-4") as ws:  # 机器人连回本副本：在线标记被改回来，照常派发
        assert ws.receive_json()["type"] == "hello"
        wait_for_command(client, admin_headers, command_id, "dispatched", attempts=1)
        assert receive_command(ws, command_id)["attempt"] == 1
        finish_command(ws, command_id)
        wait_for_command(client, admin_headers, command_id, "succeeded")
        disconnect_robot_4(client, ws, viewer_headers)


# --------------------------------------------------------------------------- sweeper


def add_command(
    session: AsyncSession, status: str, *, updated: datetime, acked: datetime | None = None
) -> uuid.UUID:
    command = Command(
        id=uuid.uuid4(),
        robot_id="robot-1",
        type="stop",
        params={},
        status=status,
        attempts=0 if status == sm.QUEUED else 1,
        created_by="sweeper-test",
        created_at=updated,
        updated_at=updated,
        dispatched_at=None if status == sm.QUEUED else updated,
        acked_at=acked,
        finished_at=updated if sm.is_terminal(status) else None,
    )
    session.add(command)
    return command.id


async def load_command(
    session: AsyncSession, command_id: uuid.UUID
) -> tuple[Command, list[CommandEvent]]:
    command = await session.get(Command, command_id)
    assert command is not None
    stmt = select(CommandEvent).where(CommandEvent.command_id == command_id)
    return command, list((await session.execute(stmt)).scalars().all())


SWEEP_LIMITS = SweepLimits(stale_after_s=90, result_timeout_s=300)


def test_sweeper_moves_stuck_commands_to_terminal_states(client: TestClient) -> None:
    async def scenario(
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> dict[str, tuple[Command, list[CommandEvent]]]:
        now = datetime.now(UTC)
        old = now - timedelta(minutes=10)
        async with sessionmaker() as session:
            ids = {
                "queued": add_command(session, sm.QUEUED, updated=old),
                "dispatched": add_command(session, sm.DISPATCHED, updated=old),
                "acked": add_command(session, sm.ACKED, updated=old, acked=old),
                "recent_acked": add_command(
                    session, sm.ACKED, updated=now - timedelta(seconds=60), acked=now
                ),
                "recent_queued": add_command(session, sm.QUEUED, updated=now),
                "succeeded": add_command(session, sm.SUCCEEDED, updated=old),
            }
            await session.commit()
        async with sessionmaker() as session:
            await sweep_stuck_commands(
                session, limits=SWEEP_LIMITS, replica="sweeper-test", bus=None
            )
        async with sessionmaker() as session:
            return {name: await load_command(session, cid) for name, cid in ids.items()}

    rows = run_db(scenario)
    for name in ("queued", "dispatched"):
        command, events = rows[name]
        assert (command.status, command.error) == ("expired", "expired")
        [event] = events  # 恰好一次转移：不会被处理两遍
        assert (event.from_status, event.to_status) == (name, "expired")
        assert event.detail == f"swept: no update for over 90s while {name}"
    command, events = rows["acked"]
    assert (command.status, command.error) == ("failed", "result_timeout")
    assert command.finished_at is not None
    [event] = events
    assert (event.from_status, event.to_status) == ("acked", "failed")
    assert event.detail == "swept: no result within 300s after ack"
    untouched = {"recent_acked": "acked", "recent_queued": "queued", "succeeded": "succeeded"}
    for name, status in untouched.items():
        command, events = rows[name]
        assert command.status == status
        assert events == []


def test_sweeper_skips_rows_locked_by_another_transaction(client: TestClient) -> None:
    async def scenario(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
        old = datetime.now(UTC) - timedelta(minutes=10)
        async with sessionmaker() as session:
            locked_id = add_command(session, sm.QUEUED, updated=old)
            free_id = add_command(session, sm.QUEUED, updated=old)
            await session.commit()
        async with sessionmaker() as other_replica:
            # 模拟"另一个副本的事务正锁着这一行"
            lock = select(Command).where(Command.id == locked_id).with_for_update()
            await other_replica.execute(lock)
            async with sessionmaker() as session:
                # SKIP LOCKED：被锁住的行直接跳过，绝不在锁上等待。
                # （如果用的是普通 FOR UPDATE，这里会一直等到 other_replica 结束，于是超时失败。）
                await asyncio.wait_for(
                    sweep_stuck_commands(
                        session, limits=SWEEP_LIMITS, replica="sweeper-test", bus=None
                    ),
                    timeout=5,
                )
            async with sessionmaker() as session:
                locked_during = (await load_command(session, locked_id))[0].status
                free_during = (await load_command(session, free_id))[0].status
            await other_replica.rollback()  # 释放行锁
        async with sessionmaker() as session:
            await sweep_stuck_commands(
                session, limits=SWEEP_LIMITS, replica="sweeper-test", bus=None
            )
        async with sessionmaker() as session:
            locked_after = (await load_command(session, locked_id))[0].status
        return locked_during, free_during, locked_after

    assert run_db(scenario) == ("queued", "expired", "expired")


# --------------------------------------------------------------------------- RabbitMQ 断线
# 这两个测试会断开应用自己的 AMQP 连接，放在最后，免得影响前面的测试。


async def consumer_count(services: Services, queue_name: str) -> int:
    """被动声明（passive）一个已存在的队列，broker 会顺带告诉我们它当前有几个消费者。"""
    channel = aio_pika.Channel(services.mq.connection, publisher_confirms=False)
    await channel
    try:
        queue = await channel.declare_queue(queue_name, passive=True)
        assert queue.declaration_result is not None
        return int(queue.declaration_result.consumer_count)
    finally:
        await channel.close()


async def start_rabbitmq_outage(services: Services) -> URL:
    """把重连地址换成一个连不上的端口，再断开连接：aio-pika 会一直重连失败，直到地址被换回来。"""
    connection = services.mq.connection
    real_url = connection.url
    connection.url = real_url.with_port(1)
    assert connection.transport is not None
    await connection.transport.connection.close(ConnectionError("simulated broker outage"))
    return real_url


async def end_rabbitmq_outage(services: Services, real_url: URL) -> None:
    services.mq.connection.url = real_url


def wait_for_rabbitmq(client: TestClient) -> None:
    wait_for(lambda: services_of(client).mq.is_connected(), timeout_s=20)


def test_robot_consumer_survives_a_rabbitmq_outage(
    client: TestClient, admin_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    services = services_of(client)
    queue = robot_queue_name("robot-4")
    with robot_ws(client, "robot-4") as ws:
        assert ws.receive_json()["type"] == "hello"
        wait_for(lambda: client.portal.call(consumer_count, services, queue) == 1)
        real_url = client.portal.call(start_rabbitmq_outage, services)
        time.sleep(1.0)
        client.portal.call(end_rabbitmq_outage, services, real_url)
        wait_for_rabbitmq(client)
        # 机器人一直在线：消费循环发现 channel 被关后重新打开，
        # 而且每个队列只有一个消费者（没有被 aio-pika 复活的旧消费者）。
        for name in (queue, EXPIRED_QUEUE):
            wait_for(lambda name=name: client.portal.call(consumer_count, services, name) == 1)
        command_id = create_robot_4_command(client, admin_headers, {"type": "stop", "params": {}})
        wait_for_command(client, admin_headers, command_id, "dispatched")
        receive_command(ws, command_id)
        finish_command(ws, command_id)
        wait_for_command(client, admin_headers, command_id, "succeeded")
        disconnect_robot_4(client, ws, viewer_headers)


def test_no_ghost_consumer_when_the_robot_leaves_during_an_outage(
    client: TestClient, admin_headers: dict[str, str], viewer_headers: dict[str, str]
) -> None:
    services = services_of(client)
    queue = robot_queue_name("robot-4")
    with robot_ws(client, "robot-4") as ws:
        assert ws.receive_json()["type"] == "hello"
        wait_for(lambda: client.portal.call(consumer_count, services, queue) == 1)
        real_url = client.portal.call(start_rabbitmq_outage, services)
        disconnect_robot_4(client, ws, viewer_headers)  # 机器人恰好在 RabbitMQ 断线期间离开
    client.portal.call(end_rabbitmq_outage, services, real_url)
    wait_for_rabbitmq(client)
    time.sleep(2.0)  # 给 aio-pika 恢复 robust channel 的时间：旧实现会在这一步复活一个幽灵消费者
    assert client.portal.call(consumer_count, services, queue) == 0

    # 命令照常排队，机器人回来后照常执行（幽灵消费者会把它扣住，永远不下发）
    command_id = create_robot_4_command(client, admin_headers, {"type": "stop", "params": {}})
    with robot_ws(client, "robot-4") as ws:
        assert ws.receive_json()["type"] == "hello"
        wait_for_command(client, admin_headers, command_id, "dispatched")
        receive_command(ws, command_id)
        finish_command(ws, command_id)
        wait_for_command(client, admin_headers, command_id, "succeeded")
        disconnect_robot_4(client, ws, viewer_headers)


def test_metrics_use_route_templates(client: TestClient, viewer_headers: dict[str, str]) -> None:
    client.get("/api/robots/robot-1", headers=viewer_headers)
    text = client.get("/metrics").text
    assert 'route="/api/robots/{robot_id}"' in text
    assert 'route="/api/robots/robot-1"' not in text
    for name in (
        "rc_ws_connections",
        "rc_telemetry_messages_total",
        "rc_command_transitions_total",
        "rc_command_ack_latency_seconds",
        "rc_telemetry_rows_written_total",
    ):
        assert name in text
