"""RedisBus 发件箱：publish_nowait 从不等待；后台任务按顺序发送；Redis 出错不会让它停下。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import redis_bus
from app.redis_bus import EVENTS_CHANNEL, RedisBus


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.failures_left = 0

    def register_script(self, _script: str) -> object:
        return object()

    async def publish(self, channel: str, text: str) -> None:
        assert channel == EVENTS_CHANNEL
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionError("redis is down")
        self.published.append(json.loads(text))


async def drain(bus: RedisBus, redis: FakeRedis, expected: int) -> None:
    task = asyncio.create_task(bus.run_outbox())
    try:
        for _ in range(200):
            if len(redis.published) >= expected:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"only {len(redis.published)} of {expected} events published")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_events_are_published_in_order() -> None:
    redis = FakeRedis()
    bus = RedisBus(redis)  # type: ignore[arg-type]
    for n in range(5):
        bus.publish_nowait({"n": n})
    await drain(bus, redis, 5)
    assert [event["n"] for event in redis.published] == [0, 1, 2, 3, 4]


async def test_full_outbox_drops_new_events_instead_of_blocking() -> None:
    redis = FakeRedis()
    bus = RedisBus(redis, outbox_size=3)  # type: ignore[arg-type]
    for n in range(10):
        bus.publish_nowait({"n": n})  # 没有后台任务在发：前 3 条进队，其余丢弃
    await drain(bus, redis, 3)
    assert [event["n"] for event in redis.published] == [0, 1, 2]


async def test_publish_failure_drops_that_event_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redis_bus, "OUTBOX_RETRY_DELAY_S", 0.01)
    redis = FakeRedis()
    redis.failures_left = 1
    bus = RedisBus(redis)  # type: ignore[arg-type]
    for n in range(3):
        bus.publish_nowait({"n": n})
    await drain(bus, redis, 2)
    assert [event["n"] for event in redis.published] == [1, 2]
