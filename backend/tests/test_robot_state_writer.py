"""RobotStateWriter：接收循环交给它的东西都是内存操作；Redis 慢/宕机时只保留最新遥测、日志不丢。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app import robot_state_writer
from app.log_throttle import LogThrottle
from app.robot_state_writer import PENDING_LOGS_LIMIT, RobotStateWriter
from app.schemas import LogOut, RobotTelemetryMsg


class FakeBus:
    """记录写入；open 被 clear 时所有写入都卡住（模拟 Redis 极慢），fail=True 时立刻失败。"""

    def __init__(self) -> None:
        self.open = asyncio.Event()
        self.open.set()
        self.fail = False
        self.online: list[str] = []
        self.telemetry: list[dict[str, Any]] = []
        self.logs: list[str] = []

    async def _gate(self) -> None:
        await self.open.wait()
        if self.fail:
            raise ConnectionError("redis is down")

    async def set_online(
        self, robot_id: str, *, replica: str, online_ttl_s: int, event: dict[str, Any]
    ) -> None:
        await self._gate()
        self.online.append(replica)

    async def save_telemetry(
        self,
        robot_id: str,
        fields: dict[str, Any],
        *,
        replica: str,
        online_ttl_s: int,
        event: dict[str, Any],
    ) -> None:
        await self._gate()
        self.telemetry.append(fields)

    async def save_log(self, robot_id: str, log_json: str, *, event: dict[str, Any]) -> None:
        await self._gate()
        self.logs.append(json.loads(log_json)["msg"])


def telemetry(x: float) -> RobotTelemetryMsg:
    return RobotTelemetryMsg(
        type="telemetry", ts=time.time(), x=x, y=0, theta=0, speed=0, battery=50, mode="idle"
    )


def log_entry(msg: str) -> LogOut:
    return LogOut(ts=datetime.now(UTC), level="info", msg=msg)


async def eventually(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(robot_state_writer, "RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(robot_state_writer, "_redis_errors", LogThrottle(interval_s=60.0))


@pytest.fixture
async def bus_and_writer() -> AsyncIterator[tuple[FakeBus, RobotStateWriter]]:
    bus = FakeBus()
    writer = RobotStateWriter("robot-1", bus=bus, replica="api-1", online_ttl_s=5)  # type: ignore[arg-type]
    task = asyncio.create_task(writer.run())
    yield bus, writer
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_announce_online_writes_presence(
    bus_and_writer: tuple[FakeBus, RobotStateWriter],
) -> None:
    bus, writer = bus_and_writer
    assert await writer.announce_online(timeout_s=1.0)
    assert bus.online == ["api-1"]


async def test_announce_online_gives_up_quickly_when_redis_hangs(
    bus_and_writer: tuple[FakeBus, RobotStateWriter],
) -> None:
    bus, writer = bus_and_writer
    bus.open.clear()
    started = time.monotonic()
    assert not await writer.announce_online(timeout_s=0.05)
    assert time.monotonic() - started < 0.5
    bus.open.set()  # Redis 恢复后，后台任务会补上这次没写成的在线标记
    await eventually(lambda: bus.online == ["api-1"])


async def test_only_the_latest_sample_is_written_while_redis_is_slow(
    bus_and_writer: tuple[FakeBus, RobotStateWriter],
) -> None:
    bus, writer = bus_and_writer
    assert await writer.announce_online(timeout_s=1.0)
    bus.open.clear()
    writer.offer_telemetry(telemetry(1))
    await asyncio.sleep(0.02)  # 后台任务取走第 1 条，卡在 Redis 上
    for x in range(2, 51):  # 期间到达的 49 条只会互相覆盖，offer 从不等待
        writer.offer_telemetry(telemetry(x))
    assert bus.telemetry == []
    bus.open.set()
    await eventually(lambda: len(bus.telemetry) == 2)
    await asyncio.sleep(0.05)
    assert [fields["x"] for fields in bus.telemetry] == [1, 50]
    assert all(fields["replica"] == "api-1" for fields in bus.telemetry)


async def test_logs_survive_a_redis_outage_in_order(
    bus_and_writer: tuple[FakeBus, RobotStateWriter],
) -> None:
    bus, writer = bus_and_writer
    bus.fail = True
    for msg in ("a", "b", "c"):
        writer.offer_log(log_entry(msg))
    await asyncio.sleep(0.05)  # 后台任务在失败-重试之间循环
    assert bus.logs == []
    bus.fail = False
    await eventually(lambda: bus.logs == ["a", "b", "c"])


async def test_pending_logs_are_bounded_oldest_dropped() -> None:
    bus = FakeBus()
    writer = RobotStateWriter("robot-1", bus=bus, replica="api-1", online_ttl_s=5)  # type: ignore[arg-type]
    for n in range(PENDING_LOGS_LIMIT + 50):
        writer.offer_log(log_entry(str(n)))
    task = asyncio.create_task(writer.run())
    try:
        await eventually(lambda: len(bus.logs) == PENDING_LOGS_LIMIT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bus.logs == [str(n) for n in range(50, PENDING_LOGS_LIMIT + 50)]


async def test_repeated_failures_are_logged_once_per_interval(
    bus_and_writer: tuple[FakeBus, RobotStateWriter], caplog: pytest.LogCaptureFixture
) -> None:
    bus, writer = bus_and_writer
    bus.fail = True
    with caplog.at_level(logging.WARNING, logger="app.robot_state_writer"):
        for x in range(20):
            writer.offer_telemetry(telemetry(x))
            await asyncio.sleep(0.015)
    failures = [r for r in caplog.records if r.name == "app.robot_state_writer"]
    assert len(failures) == 1
