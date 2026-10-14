"""限流日志：同一类错误短时间内反复发生时，只记一条，并带上"这期间又压掉了多少条"。

为什么需要：Redis 宕机时，4 个机器人 × 每秒 5 条遥测，每条写入失败都打一条带堆栈的日志，
就是每秒 20 条一模一样的错误——真正有用的日志被淹没，日志量本身也成了负担。
限流之后每 interval_s 秒最多一条，suppressed 字段告诉你问题有多频繁，信息一点没少。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any


class LogThrottle:
    def __init__(self, interval_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval_s = interval_s
        # 单调时钟：不受系统时间被调整（NTP 校时、手动改时间）的影响，适合测"过了多久"。
        self._clock = clock
        self._next_allowed: float | None = None
        self._suppressed = 0

    def log(
        self,
        logger: logging.Logger,
        level: int,
        msg: str,
        *,
        extra: dict[str, Any] | None = None,
        exc_info: bool = False,
    ) -> bool:
        """距上一条不足 interval_s 秒就只计数、不输出；返回这次是否真的写了日志。"""
        now = self._clock()
        if self._next_allowed is not None and now < self._next_allowed:
            self._suppressed += 1
            return False
        fields = dict(extra or {})
        if self._suppressed:
            fields["suppressed"] = self._suppressed
        self._suppressed = 0
        self._next_allowed = now + self._interval_s
        logger.log(level, msg, extra=fields, exc_info=exc_info)
        return True
