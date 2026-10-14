"""遥测落库（SPEC §14）：内存缓冲 + 降采样 + 定时批量写入。

为什么不每条遥测都 INSERT 一次
------------------------------
4 个机器人 × 5 Hz = 每秒 20 条；每条单独一个事务就是每秒 20 次提交，每次提交都要等 WAL 刷盘。
攒一秒再用一条多行 INSERT（INSERT ... VALUES (...), (...), ...）写入，只需要一次往返、一次提交。
同时每个机器人每 0.5 秒最多保留一条：图表只需要这个精度，表的增长速度也降到原来的 2/5。

代价：进程被强杀时，缓冲里最多 TELEMETRY_FLUSH_INTERVAL_S 秒的遥测会丢——对监控曲线可以接受。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.metrics import TELEMETRY_ROWS_WRITTEN
from app.models import Telemetry

logger = logging.getLogger(__name__)

MIN_SAMPLE_GAP_S = 0.5
MAX_BUFFERED_ROWS = 10_000
# 每条 INSERT 最多 1000 行：PostgreSQL 单条语句最多 32767 个绑定参数（8 列 × 1000 行远低于上限）。
ROWS_PER_INSERT = 1000


@dataclass(frozen=True)
class TelemetrySample:
    robot_id: str
    ts: datetime
    x: float
    y: float
    theta: float
    speed: float
    battery: float
    mode: str


class TelemetryWriter:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        flush_interval_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._flush_interval_s = flush_interval_s
        # 用单调时钟（monotonic）做降采样，而不是机器人上报的 ts：
        # 机器人的时钟可能不准甚至乱跳，不能让它决定我们写多少行。
        self._clock = clock
        self._last_accepted: dict[str, float] = {}
        self._buffer: list[TelemetrySample] = []
        self._stopped = asyncio.Event()

    def offer(self, sample: TelemetrySample) -> bool:
        """接收一条样本；距同一机器人上一条被接收的样本不足 0.5 秒则丢弃。"""
        now = self._clock()
        last = self._last_accepted.get(sample.robot_id)
        if last is not None and now - last < MIN_SAMPLE_GAP_S:
            return False
        if len(self._buffer) >= MAX_BUFFERED_ROWS:
            return False  # 数据库长时间写不进去时保护内存
        self._last_accepted[sample.robot_id] = now
        self._buffer.append(sample)
        return True

    async def run(self) -> None:
        """常驻任务：每隔 flush_interval_s 秒写一次；stop() 之后做最后一次写入再退出。

        关闭时用 stop() 而不是 task.cancel()：取消可能正好打断一次写入，
        而那一批数据已经从缓冲区换出来了，会直接丢失。
        """
        while not self._stopped.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._flush_interval_s)
            await self.flush()

    def stop(self) -> None:
        self._stopped.set()

    async def flush(self) -> int:
        """把缓冲区整体换出来写入数据库，返回写入行数。失败时记录日志并丢弃这一批。"""
        if not self._buffer:
            return 0
        # 先"交换"再写：await 期间新到的遥测进入新的空列表，不会和正在写的这一批混在一起。
        batch, self._buffer = self._buffer, []
        rows = [asdict(sample) for sample in batch]
        try:
            async with self._sessionmaker() as session:
                for start in range(0, len(rows), ROWS_PER_INSERT):
                    # .values(列表) 生成一条多行 INSERT ... VALUES (...), (...), ...
                    await session.execute(
                        insert(Telemetry).values(rows[start : start + ROWS_PER_INSERT])
                    )
                await session.commit()
        except Exception:
            logger.exception("telemetry flush failed; batch dropped", extra={"rows": len(rows)})
            return 0
        TELEMETRY_ROWS_WRITTEN.inc(len(rows))
        return len(rows)
