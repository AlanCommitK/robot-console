"""WebSocket 接口：机器人 /ws/robot（SPEC §9）与看板 /ws/dashboard（SPEC §10）。

为什么认证失败要"先 accept 再用 4401 关闭"，而不是直接拒绝握手
----------------------------------------------------------------
浏览器的 WebSocket API 拿不到握手失败时的 HTTP 状态码，只会看到一个笼统的 error 和关闭码 1006，
分不清是"令牌过期"还是"网络断了"。先接受连接、再用应用自定义的关闭码（4000-4999 段）关闭，
客户端就能读到 event.code === 4401，从而跳回登录页，而不是无休止地重连。

凭证怎么传：机器人用 Authorization 请求头，看板用一次性票据（原因见 security.py 顶部）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.commands_service import ConnectionSupersededError
from app.deps import Services, get_services
from app.metrics import TELEMETRY_MESSAGES, WS_CONNECTIONS, WS_INVALID_MESSAGES
from app.models import Robot
from app.redis_bus import OWNED_BY_OTHER_REPLICA
from app.robot_state_writer import RobotStateWriter
from app.schemas import (
    HelloMsg,
    LogOut,
    RobotAckMsg,
    RobotLogMsg,
    RobotResultMsg,
    RobotStatusEvent,
    RobotTelemetryMsg,
    dashboard_message_adapter,
    robot_message_adapter,
)
from app.security import TokenClaims, parse_ws_ticket_payload, verify_robot_token
from app.telemetry_writer import TelemetrySample
from app.ws_hub import RobotConnection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4409
CLOSE_SERVER_ERROR = 1011
PONG_TEXT = '{"type":"pong"}'
# 连接建立/断开时写 Redis 在线标记最多等这么久：Redis 故障时不能让连接的建立和清理一直挂着。
PRESENCE_TIMEOUT_S = 2.0


# =========================================================================== 机器人


@router.websocket("/ws/robot")
async def robot_socket(websocket: WebSocket, robot_id: str = "") -> None:
    # robot_id 设成可选：缺参数时也走"accept 后 4401 关闭"，而不是让框架直接拒绝握手。
    services = get_services(websocket)
    await websocket.accept()
    try:
        authorized = await _authenticate_robot(services, robot_id, _bearer_token(websocket))
    except Exception:
        logger.exception("robot authentication failed", extra={"robot_id": robot_id})
        await websocket.close(code=CLOSE_SERVER_ERROR, reason="server error")
        return
    if not authorized:
        logger.warning(
            "robot rejected",
            extra={
                "robot_id": robot_id,
                # 旧版客户端把 token 放在查询参数里：现在不再接受，给个明确的排查线索。
                "token_in_query": "token" in websocket.query_params,
            },
        )
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return

    connection = RobotConnection(robot_id, websocket)
    previous = services.robots.register(connection)
    if previous is not None:
        # 同一个机器人在本副本上的旧连接（例如对方没察觉的半开连接）：先停掉它的后台任务
        # （关闭 channel，让未 ack 的消息回到队列），再用 4409 关闭，最后才启动新连接的消费者，
        # 保证同一时刻只有一个消费者在给这个机器人派发命令。
        await previous.close(code=CLOSE_REPLACED, reason="replaced")

    WS_CONNECTIONS.labels(kind="robot").inc()
    logger.info("robot connected", extra={"robot_id": robot_id})
    state = RobotStateWriter(
        robot_id,
        bus=services.bus,
        replica=services.settings.replica_id,
        online_ttl_s=services.settings.robot_online_ttl_s,
    )
    try:
        hello = HelloMsg(replica=services.settings.replica_id, server_time=time.time())
        await connection.send_json(hello.model_dump())
        # 先写在线标记、再开始派发：派发前的归属检查读的就是这个标记
        # （见 RobotStateWriter.announce_online）。
        await state.announce_online(PRESENCE_TIMEOUT_S)
        connection.start_task("dispatcher", _run_dispatcher(services, connection))
        connection.start_task("state-writer", state.run())
        await _robot_receive_loop(services, connection, state)
    except WebSocketDisconnect:
        # 机器人刚连上就走了（例如模拟器正在退出），hello 都没发出去。这是正常现象，
        # 不该让 uvicorn 当成未处理异常打一条 ERROR 日志；清理照样由下面的 finally 完成。
        logger.info("robot left during the handshake", extra={"robot_id": robot_id})
    finally:
        await connection.stop_tasks()
        if services.robots.unregister(connection):
            await _announce_offline(services, robot_id)
        WS_CONNECTIONS.labels(kind="robot").dec()
        logger.info("robot disconnected", extra={"robot_id": robot_id})


def _bearer_token(websocket: WebSocket) -> str:
    """从握手请求的 `Authorization: Bearer <token>` 头里取出 token；格式不对返回空串。"""
    scheme, _, token = websocket.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def _authenticate_robot(services: Services, robot_id: str, token: str) -> bool:
    if not robot_id or not token:
        return False
    async with services.sessionmaker() as session:
        robot = await session.get(Robot, robot_id)
    return robot is not None and verify_robot_token(token, robot.token_hash)


async def _run_dispatcher(services: Services, connection: RobotConnection) -> None:
    try:
        await services.dispatcher.run_for_robot(connection.robot_id, connection.send_json)
    except ConnectionSupersededError:
        # 机器人已经在别的副本上有了更新的连接，这条是过时的（多半是半开连接）：用 4409 关掉它。
        # 关闭帧发出后接收循环会收到断开，处理器的 finally 负责剩下的清理。
        logger.info(
            "robot connection superseded by another replica",
            extra={"robot_id": connection.robot_id},
        )
        await connection.close_socket(CLOSE_REPLACED, "replaced by a newer connection")


async def _robot_receive_loop(
    services: Services, connection: RobotConnection, state: RobotStateWriter
) -> None:
    while True:
        text = await _receive_text(connection.websocket)
        if text is None:  # 对方断开
            return
        await _handle_robot_text(services, connection.robot_id, state, text)


async def _handle_robot_text(
    services: Services, robot_id: str, state: RobotStateWriter, text: str
) -> None:
    """处理一条机器人消息。任何错误都只影响这一条消息，绝不能让连接断掉。

    这里不直接等待 Redis：遥测和日志只交给 RobotStateWriter（内存操作，立即返回），
    所以 ack/result 永远不会排在一次慢吞吞的 Redis 调用后面（原因见 robot_state_writer.py）。
    """
    try:
        message = robot_message_adapter.validate_json(text)
    except ValidationError as exc:
        WS_INVALID_MESSAGES.labels(kind="robot").inc()
        logger.warning(
            "invalid robot message ignored",
            extra={"robot_id": robot_id, "error": exc.errors(include_url=False)[:3]},
        )
        return
    try:
        match message:
            case RobotTelemetryMsg():
                _on_telemetry(services, robot_id, state, message)
            case RobotAckMsg():
                await services.dispatcher.on_robot_ack(robot_id, message.command_id)
            case RobotResultMsg():
                await services.dispatcher.on_robot_result(
                    robot_id,
                    message.command_id,
                    succeeded=message.status == "succeeded",
                    detail=message.detail,
                )
            case RobotLogMsg():
                state.offer_log(LogOut(ts=datetime.now(UTC), level=message.level, msg=message.msg))
    except Exception:
        logger.exception(
            "failed to handle robot message", extra={"robot_id": robot_id, "type": message.type}
        )


def _on_telemetry(
    services: Services, robot_id: str, state: RobotStateWriter, msg: RobotTelemetryMsg
) -> None:
    TELEMETRY_MESSAGES.inc()
    # 两个去处都只是内存操作：落库缓冲（PostgreSQL 历史曲线）和"最新状态"槽（Redis 实时状态）。
    services.telemetry.offer(
        TelemetrySample(
            robot_id=robot_id,
            ts=datetime.fromtimestamp(msg.ts, UTC),
            x=msg.x,
            y=msg.y,
            theta=msg.theta,
            speed=msg.speed,
            battery=msg.battery,
            mode=msg.mode,
        )
    )
    state.offer_telemetry(msg)


async def _announce_offline(services: Services, robot_id: str) -> None:
    replica = services.settings.replica_id
    try:
        async with asyncio.timeout(PRESENCE_TIMEOUT_S):
            owner = await services.bus.clear_online_if_owner(robot_id, replica=replica)
            if owner == OWNED_BY_OTHER_REPLICA:
                # 机器人已经重连到别的副本：它是在线的，我们不能广播"离线"。
                return
            await services.bus.publish(
                RobotStatusEvent(robot_id=robot_id, online=False, replica=replica).model_dump()
            )
    except Exception:
        # 删不掉也没关系：在线标记是带 TTL 的租约，最多 ROBOT_ONLINE_TTL_S 秒后自己过期。
        logger.warning(
            "failed to announce robot offline", extra={"robot_id": robot_id}, exc_info=True
        )


# =========================================================================== 看板


@router.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket, ticket: str = "") -> None:
    services = get_services(websocket)
    await websocket.accept()
    try:
        claims = await _redeem_ticket(services, ticket)
    except Exception:
        # Redis 不可用：用 1011 而不是 4401 关闭——这不是"票据无效"，前端应当退避重连而不是登出。
        logger.warning("could not redeem dashboard ticket", exc_info=True)
        await websocket.close(code=CLOSE_SERVER_ERROR, reason="server error")
        return
    if claims is None:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return

    client = services.hub.connect(websocket)
    WS_CONNECTIONS.labels(kind="dashboard").inc()
    try:
        # 连接的寿命不能超过签发票据的那个 JWT：到点后 asyncio.timeout 会打断接收循环。
        async with asyncio.timeout(max(0.0, claims.expires_at.timestamp() - time.time())):
            await _dashboard_receive_loop(websocket, client.offer)
    except TimeoutError:
        await _close_quietly(websocket, CLOSE_UNAUTHORIZED, "token expired")
    finally:
        await services.hub.disconnect(client)
        WS_CONNECTIONS.labels(kind="dashboard").dec()


async def _redeem_ticket(services: Services, ticket: str) -> TokenClaims | None:
    """兑换一次性票据：票据不存在、已用过、已过期、或签发它的 JWT 已过期，都返回 None。"""
    if not ticket:
        return None
    payload = await services.bus.take_ws_ticket(ticket)
    if payload is None:
        return None
    try:
        claims = parse_ws_ticket_payload(payload)
    except ValueError:
        logger.warning("malformed dashboard ticket payload ignored")
        return None
    return claims if claims.expires_at.timestamp() > time.time() else None


async def _dashboard_receive_loop(websocket: WebSocket, offer: Callable[[str], bool]) -> None:
    while True:
        text = await _receive_text(websocket)
        if text is None:
            return
        try:
            dashboard_message_adapter.validate_json(text)
        except ValidationError:
            WS_INVALID_MESSAGES.labels(kind="dashboard").inc()
            logger.warning("invalid dashboard message ignored")
            continue
        # pong 也走同一个队列：只有发送任务会写这条 WebSocket，写操作天然串行。
        offer(PONG_TEXT)


# =========================================================================== 公共


async def _receive_text(websocket: WebSocket) -> str | None:
    """读下一条消息并统一成文本；对方断开时返回 None。

    不用 websocket.receive_text()：对方发来二进制帧时它会直接抛 KeyError，把连接搞崩。
    """
    if websocket.client_state != WebSocketState.CONNECTED:
        return None
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    if message.get("text") is not None:
        return message["text"]
    raw = message.get("bytes") or b""
    # 解码失败时用替换字符，后续 JSON 校验自然会把它当作非法消息处理。
    return raw.decode("utf-8", errors="replace")


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        logger.debug("ignoring error while closing websocket", exc_info=True)
