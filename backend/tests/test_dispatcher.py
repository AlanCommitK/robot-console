"""CommandDispatcher 的派发前归属检查（不需要真实的 RabbitMQ/Redis/PostgreSQL）。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.commands_service import AckRegistry, CommandDispatcher, ConnectionSupersededError


class FakeBus:
    def __init__(self, owner: str | None = None, *, error: Exception | None = None) -> None:
        self.owner = owner
        self.error = error
        self.hang = False

    async def get_online_replica(self, robot_id: str) -> str | None:
        if self.hang:
            await asyncio.sleep(10)
        if self.error is not None:
            raise self.error
        return self.owner


class FakeMessage:
    def __init__(self) -> None:
        self.message_id = str(uuid.uuid4())
        self.calls: list[str] = []

    async def ack(self) -> None:
        self.calls.append("ack")

    async def nack(self, requeue: bool = True) -> None:
        self.calls.append("nack")


def make_dispatcher(bus: FakeBus) -> CommandDispatcher:
    return CommandDispatcher(
        sessionmaker=None,  # type: ignore[arg-type]
        bus=bus,  # type: ignore[arg-type]
        mq=None,  # type: ignore[arg-type]
        acks=AckRegistry(),
        replica="api-1",
        ack_timeout_s=1.0,
    )


@pytest.mark.parametrize(
    ("owner", "expected"),
    [("api-2", True), ("api-1", False), (None, False)],
)
async def test_ownership_follows_the_online_key(owner: str | None, expected: bool) -> None:
    dispatcher = make_dispatcher(FakeBus(owner))
    assert await dispatcher._owned_by_another_replica("robot-1") is expected


async def test_redis_errors_fail_open() -> None:
    dispatcher = make_dispatcher(FakeBus(error=ConnectionError("redis is down")))
    assert await dispatcher._owned_by_another_replica("robot-1") is False


async def test_slow_redis_fails_open_quickly() -> None:
    bus = FakeBus("api-2")
    bus.hang = True
    dispatcher = make_dispatcher(bus)
    started = asyncio.get_running_loop().time()
    assert await dispatcher._owned_by_another_replica("robot-1") is False
    assert asyncio.get_running_loop().time() - started < 2.0


async def test_superseded_connection_stops_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = make_dispatcher(FakeBus("api-2"))
    applied: list[Any] = []

    async def fake_apply(*args: Any, **kwargs: Any) -> None:
        applied.append(args)

    async def send(_payload: dict[str, Any]) -> None:
        raise AssertionError("a superseded connection must not receive commands")

    monkeypatch.setattr(dispatcher, "apply", fake_apply)
    message = FakeMessage()
    with pytest.raises(ConnectionSupersededError):
        await dispatcher._deliver("robot-1", message, send)  # type: ignore[arg-type]
    # 没有应用 dispatch（attempts 不变）、也没有 ack/nack：消息随 channel 关闭原样回到队列
    assert applied == []
    assert message.calls == []
