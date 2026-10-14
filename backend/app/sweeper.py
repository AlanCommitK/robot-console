"""兜底清扫：把卡在非终态、却再也不会有人推动的命令推到终态。每个副本每 30 秒扫一次。

为什么会有"卡住"的命令
--------------------
正常情况下每条命令都会被某个事件推到终态：机器人的 result、ack 超时、DLX 的过期通知……
但这些事件的"来源"本身可能丢失：
- 进程恰好死在"命令已提交到数据库"和"消息发布到 RabbitMQ"之间：库里是 queued，
  RabbitMQ 里却根本没有这条消息，它永远不会被派发，也永远不会过期；
- 应用 ack/result 时数据库刚好出错：机器人不会再发第二次，命令停在 dispatched/acked；
- 机器人 ack 之后崩溃重启，结果随之丢失，命令永远停在 acked。

规则
----
- queued / dispatched 超过 COMMAND_TTL_MS + 60 s 没有任何更新 -> expire（detail 注明是被清扫的）。
  正常情况下消息最多在队列里待 COMMAND_TTL_MS 就会经 DLX 过期；多留 60 s 余量，保证 sweeper
  只处理"DLX 已经不会再来"的命令，不和正常流程抢。
- acked 超过 RESULT_TIMEOUT_S 还没有结果 -> result_timeout -> failed。

为什么用 SELECT ... FOR UPDATE SKIP LOCKED
-----------------------------------------
每个副本都跑 sweeper（没有"主副本"，挂掉哪一个都不影响清扫）。两个副本同时扫描时：
- 普通的 FOR UPDATE：后到的副本会在已被锁住的行上排队；等前一个提交后，PostgreSQL 用这一行的
  最新版本重新检查 WHERE，行已是终态就不再返回。浪费的是这段等锁的时间；
- SKIP LOCKED：已被其他事务锁住的行直接跳过，各自只拿到"此刻没人在处理"的行。
一行被某个副本锁住后，直到它提交别人都拿不到；提交后它已是终态，不再满足清扫条件。
所以同一行永远只会被一个副本处理——这也是"用数据库表当任务队列"的标准写法。
状态变化仍走状态机（transition_locked），和 apply_event 是同一套代码：恰好同时到达的正常事件
（比如迟到的 result）会在 apply_event 的 FOR UPDATE 上等清扫事务提交，再基于新状态判断，互不覆盖。

查询条件只涉及非终态的行。数据量很大时可以给 commands 加一个部分索引
（... ON commands (updated_at) WHERE status IN ('queued','dispatched','acked')），
让每次扫描只碰活跃的命令，而不是整张表。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import state_machine as sm
from app.commands_service import finish_transition, transition_locked, utcnow
from app.models import Command
from app.redis_bus import RedisBus
from app.schemas import CommandOut

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 30.0
SWEEP_BATCH_SIZE = 100
EXPIRE_GRACE_S = 60.0


@dataclass(frozen=True)
class SweepLimits:
    stale_after_s: float  # queued/dispatched 多久没有更新算"卡住"
    result_timeout_s: float  # acked 之后最多等多久的结果

    @classmethod
    def from_settings(cls, *, command_ttl_ms: int, result_timeout_s: float) -> SweepLimits:
        return cls(
            stale_after_s=command_ttl_ms / 1000 + EXPIRE_GRACE_S,
            result_timeout_s=result_timeout_s,
        )


async def sweep_stuck_commands(
    session: AsyncSession,
    *,
    limits: SweepLimits,
    replica: str,
    bus: RedisBus | None,
    now: datetime | None = None,
    batch_size: int = SWEEP_BATCH_SIZE,
) -> list[CommandOut]:
    """在一个事务里认领一批卡住的命令（最多 batch_size 条）并推到终态；返回被改动的命令。"""
    now = now or utcnow()
    stale_before = now - timedelta(seconds=limits.stale_after_s)
    result_before = now - timedelta(seconds=limits.result_timeout_s)
    stmt = (
        select(Command)
        .where(
            or_(
                and_(
                    Command.status.in_((sm.QUEUED, sm.DISPATCHED)),
                    Command.updated_at < stale_before,
                ),
                and_(Command.status == sm.ACKED, Command.acked_at < result_before),
            )
        )
        .order_by(Command.updated_at)
        .limit(batch_size)
        # 锁住选中的行，跳过别人正锁着的行（见模块说明）
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    transitions = []
    for command in rows:
        event, detail = _sweep_event(command, limits)
        transition = transition_locked(session, command, event, detail, replica=replica, now=now)
        if transition is not None:
            transitions.append(transition)
    await session.commit()  # 整批一起提交，同时释放所有行锁
    return [finish_transition(transition, bus) for transition in transitions]


def _sweep_event(command: Command, limits: SweepLimits) -> tuple[str, str]:
    if command.status == sm.ACKED:
        return sm.RESULT_TIMEOUT, f"swept: no result within {limits.result_timeout_s:g}s after ack"
    return sm.EXPIRE, f"swept: no update for over {limits.stale_after_s:g}s while {command.status}"


class CommandSweeper:
    """常驻任务：每 interval_s 秒清扫一次。每个副本各跑一个，靠 SKIP LOCKED 互不干扰。"""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bus: RedisBus,
        replica: str,
        limits: SweepLimits,
        interval_s: float = SWEEP_INTERVAL_S,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bus = bus
        self._replica = replica
        self._limits = limits
        self._interval_s = interval_s

    async def run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                # 例如数据库暂时不可用：记下来，下一轮再试。sweeper 只是兜底，晚一轮没有关系。
                logger.exception("command sweep failed")
            await asyncio.sleep(self._interval_s)

    async def sweep_once(self) -> int:
        """一直清扫到"这一批不满"为止（积压很多时分批处理，每批一个短事务）；返回处理的条数。"""
        total = 0
        while True:
            async with self._sessionmaker() as session:
                swept = await sweep_stuck_commands(
                    session, limits=self._limits, replica=self._replica, bus=self._bus
                )
            for command in swept:
                logger.warning(
                    "stuck command swept",
                    extra={
                        "command_id": str(command.id),
                        "robot_id": command.robot_id,
                        "status": command.status,
                        "error": command.error,
                    },
                )
            total += len(swept)
            if len(swept) < SWEEP_BATCH_SIZE:
                return total
