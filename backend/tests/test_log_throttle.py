"""LogThrottle：限流窗口内只记一条，并在下一条里报告期间被压掉的次数。"""

from __future__ import annotations

import logging

import pytest

from app.log_throttle import LogThrottle


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_logs_once_per_interval_and_reports_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    throttle = LogThrottle(interval_s=10.0, clock=clock)
    logger = logging.getLogger("test.throttle")

    with caplog.at_level(logging.WARNING, logger="test.throttle"):
        assert throttle.log(logger, logging.WARNING, "redis down", extra={"robot_id": "r1"})
        for _ in range(5):
            clock.now += 1.0
            assert not throttle.log(logger, logging.WARNING, "redis down")
        clock.now += 5.0  # 距第一条正好 10 秒：窗口结束，可以再记一条
        assert throttle.log(logger, logging.WARNING, "redis down")

    records = [r for r in caplog.records if r.name == "test.throttle"]
    assert len(records) == 2
    assert records[0].robot_id == "r1"  # type: ignore[attr-defined]
    assert not hasattr(records[0], "suppressed")
    assert records[1].suppressed == 5  # type: ignore[attr-defined]
