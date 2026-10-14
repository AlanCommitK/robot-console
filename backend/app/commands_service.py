"""命令的业务逻辑：幂等创建、状态转移（apply_event）、按机器人派发、处理过期死信。

整条链路（at-least-once，至少一次）
----------------------------------
1. POST 创建：命令入库（queued），发布到 cmd.robot.{id} 并等待 publisher confirm。
2. 机器人所在副本的消费者收到消息：apply dispatch，通过 WebSocket 发给机器人。
3. 等机器人 ack（最多 ACK_TIMEOUT_S 秒），然后手动 ack 这条 RabbitMQ 消息。
4. 机器人执行完发来 result：apply result_succeeded / result_failed。
任何一步都可能重复或乱序发生，所有状态变化都必须经过状态机（transition_locked）。
万一某一步的"推动者"彻底丢失（进程被杀、数据库出错），sweeper.py 定期兜底，把命令推到终态。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import state_machine as sm
from app.log_throttle import LogThrottle
from app.metrics import COMMAND_ACK_LATENCY, COMMAND_TRANSITIONS
from app.models import Command, CommandEvent
from app.mq import MQ, close_channel_quietly
from app.redis_bus import RedisBus
from app.schemas import (
    CommandDetailOut,
    CommandEventOut,
    CommandOut,
    CommandToRobotMsg,
    CommandUpdateEvent,
)

logger = logging.getLogger(__name__)

SendToRobot = Callable[[dict[str, Any]], Awaitable[None]]

CONSUMER_RETRY_MIN_S = 1.0
CONSUMER_RETRY_MAX_S = 15.0
# 派发前查一次 Redis 在线标记最多等这么久；查不到就照常派发（见 _owned_by_another_replica）。
OWNER_CHECK_TIMEOUT_S = 0.5


def utcnow() -> datetime:
    return datetime.now(UTC)


# =========================================================================== 状态转移


class _RecentDispatches:
    """command_id -> 本副本上"最近一次" dispatch 的时间，用来算 ack 延迟。

    只放内存、只在本副本有效就够了：机器人的 ack 一定从"收到这次派发的那条连接"回来，
    而派发它的消费者就在这条连接所在的副本上。重投时这里记录的是最新一次派发的时间，
    所以延迟不会把机器人离线的那段时间算进去。用有界 OrderedDict，防止没等到 ack 的条目无限堆积。
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._items: OrderedDict[uuid.UUID, datetime] = OrderedDict()
        self._max_size = max_size

    def remember(self, command_id: uuid.UUID, at: datetime) -> None:
        self._items[command_id] = at
        self._items.move_to_end(command_id)
        while len(self._items) > self._max_size:
            self._items.popitem(last=False)

    def pop(self, command_id: uuid.UUID) -> datetime | None:
        return self._items.pop(command_id, None)


_recent_dispatches = _RecentDispatches()


@dataclass(frozen=True)
class Transition:
    """一次已经写进会话、但还没提交的状态转移；提交成功后交给 finish_transition 收尾。"""

    command: Command
    previous: str
    new_status: str
    at: datetime


def transition_locked(
    session: AsyncSession,
    command: Command,
    event: str,
    detail: str | None = None,
    *,
    replica: str,
    now: datetime,
) -> Transition | None:
    """调用方已经用 FOR UPDATE 锁住了这一行：问状态机、改字段、追加一条事件流水（不提交）。

    返回 None = 状态机忽略了这个事件（重复/过时/乱序），什么都没改。
    apply_event（一次一个事件）和 sweeper（批量清扫）共用这个函数：所有状态变化走同一套规则。
    """
    previous = command.status
    new_status = sm.next_status(previous, event)
    if new_status is None:
        return None
    event_detail = _record_transition(command, previous, event, new_status, detail, now)
    session.add(
        CommandEvent(
            command_id=command.id,
            from_status=previous,
            to_status=new_status,
            detail=event_detail,
            replica=replica,
            at=now,
        )
    )
    return Transition(command=command, previous=previous, new_status=new_status, at=now)


def finish_transition(transition: Transition, bus: RedisBus | None) -> CommandOut:
    """事务提交成功之后才做的事：指标、ack 延迟、通知看板。

    必须放在提交之后：提交失败（回滚）的转移从来没有发生过，不能被计数，也不能让看板看到。
    """
    command = transition.command
    COMMAND_TRANSITIONS.labels(to_status=transition.new_status).inc()
    _track_ack_latency(
        command.id, transition.previous, transition.new_status, transition.at, command.dispatched_at
    )
    updated = CommandOut.model_validate(command)
    if bus is not None:
        publish_command_update(bus, updated)
    return updated


async def apply_event(
    session: AsyncSession,
    command_id: uuid.UUID,
    event: str,
    detail: str | None = None,
    *,
    replica: str,
    bus: RedisBus | None,
    robot_id: str | None = None,
) -> CommandOut | None:
    """在一个事务里给一条命令应用一个事件；返回更新后的命令，事件被忽略时返回 None。

    robot_id：如果给了，只有命令属于这个机器人时才应用（防止一个机器人去 ack 别人的命令）。

    为什么要 SELECT ... FOR UPDATE
    ------------------------------
    同一条命令的事件可能被"同时"处理：副本 A 正在 dispatch，副本 B 的过期消费者正在 expire。
    如果两边都用普通 SELECT 读到 queued，各自算出新状态再写回，后写的会覆盖先写的
    （丢失更新），事件流水也会自相矛盾。
    FOR UPDATE 给这一行加排他行锁：第二个事务会在这里阻塞，直到第一个事务提交；
    然后 PostgreSQL 会把"已提交的最新版本"返回给它（READ COMMITTED 下加锁读取的语义），
    所以第二个事务是基于真实的当前状态去问状态机的——例如在 expired 上再 dispatch 会得到 None。
    "读 -> 判断 -> 写"因此变成了针对这一行的串行操作。
    """
    stmt = (
        select(Command)
        .where(Command.id == command_id)
        .with_for_update()
        # 如果这个对象已经在 session 的缓存（identity map）里，强制用刚查到的数据覆盖它。
        .execution_options(populate_existing=True)
    )
    command = (await session.execute(stmt)).scalar_one_or_none()
    if command is None or (robot_id is not None and command.robot_id != robot_id):
        await session.rollback()
        logger.warning(
            "event for unknown command ignored",
            extra={"command_id": str(command_id), "event": event, "robot_id": robot_id},
        )
        return None

    transition = transition_locked(session, command, event, detail, replica=replica, now=utcnow())
    if transition is None:
        # 重复或过时的事件：什么都不改。rollback 结束事务，立刻释放行锁。
        # 状态要在 rollback 之前读出来：rollback 会让会话里的对象全部"过期"，之后再读属性
        # 会触发一次隐式查询，而 async 模式下不允许隐式 IO。
        status = command.status
        await session.rollback()
        logger.info(
            "event ignored by state machine",
            extra={"command_id": str(command_id), "event": event, "status": status},
        )
        return None
    await session.commit()  # 命令行 + 事件行在同一个事务里提交：要么都成功，要么都没发生
    return finish_transition(transition, bus)


def _record_transition(
    command: Command,
    previous: str,
    event: str,
    new_status: str,
    detail: str | None,
    now: datetime,
) -> str | None:
    """把一次状态转移写到 ORM 对象上；返回写进事件流水的 detail。"""
    command.status = new_status
    command.updated_at = now
    if event == sm.DISPATCH:
        command.attempts += 1  # 每一次投递（包括重投）都算一次尝试
        if command.dispatched_at is None:
            command.dispatched_at = now
        detail = detail or f"attempt {command.attempts}"
    # 没收到 ack 却直接收到了结果：结果证明机器人收到了命令，补上 acked_at。
    implied_ack = previous == sm.DISPATCHED and event in (sm.RESULT_SUCCEEDED, sm.RESULT_FAILED)
    if new_status == sm.ACKED or implied_ack:
        command.acked_at = now
    if sm.is_terminal(new_status):
        command.finished_at = now
    if event in (sm.ACK_TIMEOUT, sm.RESULT_TIMEOUT):
        command.error = event  # "ack_timeout" / "result_timeout"：一眼看出是哪一段超时
    elif new_status == sm.FAILED:
        command.error = detail or "failed"
    elif new_status == sm.EXPIRED:
        command.error = "expired"
    return detail


def _track_ack_latency(
    command_id: uuid.UUID,
    previous: str,
    new_status: str,
    now: datetime,
    first_dispatched_at: datetime | None,
) -> None:
    if new_status == sm.DISPATCHED:
        _recent_dispatches.remember(command_id, now)
        return
    if previous != sm.DISPATCHED:
        return
    dispatched_at = _recent_dispatches.pop(command_id) or first_dispatched_at
    if new_status == sm.ACKED and dispatched_at is not None:
        COMMAND_ACK_LATENCY.observe(max(0.0, (now - dispatched_at).total_seconds()))


def publish_command_update(bus: RedisBus, command: CommandOut) -> None:
    """尽力而为、而且不等待：状态已经提交到数据库，这条通知只影响看板的实时刷新。

    放进 Redis 发件箱就返回（见 redis_bus.py）。处理机器人 ack/result 的代码因此永远不会
    因为 Redis 变慢或宕机而卡住。
    """
    bus.publish_nowait(CommandUpdateEvent(command=command).model_dump(mode="json"))


# =========================================================================== 创建 / 查询


class PublishFailedError(Exception):
    """命令已入库，但没能可靠地发布到 RabbitMQ；命令已被标记为 failed（publish_failed）。"""


@dataclass(frozen=True)
class CreateResult:
    command: CommandOut
    created: bool  # False：命中了已有的 Idempotency-Key，返回的是之前创建的那条命令


async def create_command(
    session: AsyncSession,
    *,
    robot_id: str,
    command_type: str,
    params: dict[str, Any],
    idempotency_key: str | None,
    created_by: str,
    replica: str,
    mq: MQ,
    bus: RedisBus,
) -> CreateResult:
    """幂等地创建并发布一条命令。

    幂等键（Idempotency-Key）：前端每次点击生成一个新 key；网络超时后用同一个 key 重试时，
    服务端保证不会创建第二条命令，而是把第一条原样返回。
    真正的保证来自数据库唯一约束 (robot_id, idempotency_key)，而不是"先查一下有没有"：
    两个并发请求可能同时查到"没有"，然后都去插入。唯一约束让其中一个 INSERT 失败
    （后到的那个会等先到的事务结束，再报唯一冲突），我们捕获 IntegrityError，查出胜出的那条返回。
    """
    if idempotency_key is not None:
        # 快速路径：绝大多数重试走这里，避免一次注定失败的 INSERT（数据库日志里会留下一条错误）。
        existing = await _find_by_idempotency_key(session, robot_id, idempotency_key)
        if existing is not None:
            return CreateResult(CommandOut.model_validate(existing), created=False)

    now = utcnow()
    command = Command(
        id=uuid.uuid4(),
        robot_id=robot_id,
        type=command_type,
        params=params,
        status=sm.QUEUED,
        idempotency_key=idempotency_key,
        attempts=0,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    session.add(command)
    try:
        await session.flush()  # 立刻执行 INSERT：唯一约束冲突会在这一行抛出
    except IntegrityError:
        await session.rollback()
        existing = None
        if idempotency_key is not None:
            existing = await _find_by_idempotency_key(session, robot_id, idempotency_key)
        if existing is None:
            raise  # 不是幂等键冲突，是别的完整性错误，不能吞掉
        return CreateResult(CommandOut.model_validate(existing), created=False)

    session.add(
        CommandEvent(
            command_id=command.id,
            from_status=None,
            to_status=sm.QUEUED,
            detail="created",
            replica=replica,
            at=now,
        )
    )
    # 先提交再发布：消费者收到消息时，这一行必须已经能被查到，否则 dispatch 会找不到命令。
    # 如果进程恰好死在提交之后、发布之前，这条命令在 RabbitMQ 里没有消息、永远不会被派发或过期，
    # 由 sweeper.py 在 COMMAND_TTL_MS + 60 s 之后把它清扫成 expired。
    await session.commit()
    COMMAND_TRANSITIONS.labels(to_status=sm.QUEUED).inc()
    created = CommandOut.model_validate(command)
    # queued 通知在发布到 RabbitMQ 之前放进发件箱：同一副本的发件箱按顺序发送，
    # 本副本后续的状态变化一定排在它后面（跨副本的乱序由前端按版本号丢弃，见 redis_bus.py）。
    publish_command_update(bus, created)

    try:
        await mq.publish_command(robot_id, command.id, command_type, params)
    except Exception as exc:
        logger.exception("failed to publish command", extra={"command_id": str(command.id)})
        await apply_event(
            session, command.id, sm.RESULT_FAILED, "publish_failed", replica=replica, bus=bus
        )
        raise PublishFailedError(str(exc)) from exc
    return CreateResult(created, created=True)


async def _find_by_idempotency_key(
    session: AsyncSession, robot_id: str, idempotency_key: str
) -> Command | None:
    stmt = select(Command).where(
        Command.robot_id == robot_id, Command.idempotency_key == idempotency_key
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_commands(
    session: AsyncSession, *, robot_id: str | None, limit: int
) -> list[CommandOut]:
    stmt = select(Command).order_by(Command.created_at.desc()).limit(limit)
    if robot_id:
        stmt = stmt.where(Command.robot_id == robot_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [CommandOut.model_validate(row) for row in rows]


async def get_command_detail(
    session: AsyncSession, command_id: uuid.UUID
) -> CommandDetailOut | None:
    command = await session.get(Command, command_id)
    if command is None:
        return None
    # 按自增 id 排序而不是按时间：同一条命令的转移被行锁串行化，id 的先后就是转移的先后；
    # 时间戳则可能来自时钟略有偏差的不同副本。
    stmt = (
        select(CommandEvent)
        .where(CommandEvent.command_id == command_id)
        .order_by(CommandEvent.id)
    )
    events = (await session.execute(stmt)).scalars().all()
    return CommandDetailOut(
        **CommandOut.model_validate(command).model_dump(),
        events=[CommandEventOut.model_validate(event) for event in events],
    )


# =========================================================================== 派发


class AckRegistry:
    """command_id -> 正在等待机器人 ack 的 Future。只存在于本副本、只在派发期间存在。"""

    def __init__(self) -> None:
        self._waiters: dict[uuid.UUID, tuple[str, asyncio.Future[None]]] = {}

    def register(self, command_id: uuid.UUID, robot_id: str) -> asyncio.Future[None]:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[command_id] = (robot_id, waiter)
        return waiter

    def resolve(self, command_id: uuid.UUID, robot_id: str) -> bool:
        entry = self._waiters.get(command_id)
        if entry is None or entry[0] != robot_id or entry[1].done():
            return False
        entry[1].set_result(None)
        return True

    def discard(self, command_id: uuid.UUID, waiter: asyncio.Future[None]) -> None:
        entry = self._waiters.get(command_id)
        if entry is not None and entry[1] is waiter:
            del self._waiters[command_id]


class _StopConsuming(Exception):
    """消费循环遇到它就结束，不再重试（具体原因见子类）。"""


class _RobotGoneError(_StopConsuming):
    """往机器人的 WebSocket 发送失败：连接已经断了，停止消费，消息留给下一次连接。"""


class ConnectionSupersededError(_StopConsuming):
    """Redis 在线标记指向别的副本：这条连接已被机器人在另一个副本上的新连接取代。

    由 WebSocket 处理器捕获，用 4409 关闭这条旧连接（见 routers/ws.py）。
    """


class CommandDispatcher:
    """把 RabbitMQ 消息驱动成状态转移：按机器人派发命令、处理过期死信、接收机器人的 ack/结果。"""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bus: RedisBus,
        mq: MQ,
        acks: AckRegistry,
        replica: str,
        ack_timeout_s: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bus = bus
        self._mq = mq
        self._acks = acks
        self._replica = replica
        self._ack_timeout_s = ack_timeout_s
        self._owner_check_errors = LogThrottle(interval_s=10.0)

    async def apply(
        self,
        command_id: uuid.UUID,
        event: str,
        detail: str | None = None,
        *,
        robot_id: str | None = None,
    ) -> CommandOut | None:
        async with self._sessionmaker() as session:
            return await apply_event(
                session,
                command_id,
                event,
                detail,
                replica=self._replica,
                bus=self._bus,
                robot_id=robot_id,
            )

    # ------------------------------------------------------------------ 机器人 -> 服务器

    async def on_robot_ack(self, robot_id: str, command_id: uuid.UUID) -> None:
        # 先唤醒等待中的派发循环（它随后去 ack RabbitMQ 消息），再把 ack 写进数据库。
        # 两步都是幂等的：重复的 ack 找不到 Future；状态机也会忽略 acked 上的 ack。
        self._acks.resolve(command_id, robot_id)
        await self.apply(command_id, sm.ACK, robot_id=robot_id)

    async def on_robot_result(
        self, robot_id: str, command_id: uuid.UUID, *, succeeded: bool, detail: str | None
    ) -> None:
        self._acks.resolve(command_id, robot_id)  # 结果隐含"已收到"，不必再等 ack
        event = sm.RESULT_SUCCEEDED if succeeded else sm.RESULT_FAILED
        await self.apply(command_id, event, detail, robot_id=robot_id)

    # ------------------------------------------------------------------ 消费循环（两个队列共用）

    async def _keep_consuming(
        self, consume_once: Callable[[], Awaitable[None]], *, name: str, extra: dict[str, Any]
    ) -> None:
        """反复执行 consume_once（开 channel -> 消费到 channel 关闭），直到它抛出 _StopConsuming。

        - consume_once 正常返回 = channel 被关掉了（RabbitMQ 断线/重启）。稍等后重新打开；
          打开时会先等连接恢复（MQ._open_channel），所以断线期间不会空转。
        - 其他异常：记日志，指数退避（1 s -> 15 s）后重试，避免"失败-立刻重试-再失败"的空转。
        注意：asyncio.CancelledError 继承自 BaseException，`except Exception` 抓不到它，
        所以"机器人断开 / 进程退出"时的取消信号总能穿过这里的重试逻辑传出去。
        """
        delay = CONSUMER_RETRY_MIN_S
        while True:
            try:
                await consume_once()
            except _StopConsuming:
                raise
            except Exception:
                logger.exception(
                    "consumer failed; retrying",
                    extra={"consumer": name, "retry_in_s": delay, **extra},
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, CONSUMER_RETRY_MAX_S)
                continue
            logger.warning(
                "consumer stopped (channel closed); reopening", extra={"consumer": name, **extra}
            )
            delay = CONSUMER_RETRY_MIN_S
            await asyncio.sleep(CONSUMER_RETRY_MIN_S)

    # ------------------------------------------------------------------ 每个机器人的派发循环

    async def run_for_robot(self, robot_id: str, send: SendToRobot) -> None:
        """机器人连在本副本期间一直运行；机器人断开时由 WebSocket 处理器取消。

        ConnectionSupersededError 会原样抛给调用方（WebSocket 处理器负责用 4409 关闭连接）。
        """
        try:
            await self._keep_consuming(
                functools.partial(self._consume_robot_queue, robot_id, send),
                name="robot-queue",
                extra={"robot_id": robot_id},
            )
        except _RobotGoneError:
            return  # 机器人已经断开；未 ack 的消息已回到队列，留给下一次连接

    async def _consume_robot_queue(self, robot_id: str, send: SendToRobot) -> None:
        channel, queue = await self._mq.open_robot_queue(robot_id)
        try:
            async with queue.iterator() as messages:
                async for message in messages:
                    await self._deliver(robot_id, message, send)
        finally:
            # 关闭 channel = 告诉 RabbitMQ"这个消费者没了"。broker 会把这个 channel 上已投递、
            # 但还没 ack 的消息放回队列（requeue），而且消息保留原来的过期时间点（TTL 不重新计时）。
            # 机器人重连后（可能连到另一个副本），这条消息会被再次投递，attempt 变成 2；
            # 如果机器人离线超过了剩余 TTL，消息会在队列里过期，经 DLX 被标记为 expired。
            await close_channel_quietly(channel)

    async def _deliver(
        self, robot_id: str, message: AbstractIncomingMessage, send: SendToRobot
    ) -> None:
        command_id = _parse_command_id(message)
        if command_id is None:
            logger.warning(
                "message without a valid message_id dropped", extra={"robot_id": robot_id}
            )
            await message.ack()
            return
        if await self._owned_by_another_replica(robot_id):
            # 不 ack、也不应用 dispatch：抛出异常 -> 迭代器取消消费者、channel 关闭 ->
            # 这条消息原样回到队列，由机器人真正所在的副本派发。attempts 不变，不算一次失败。
            raise ConnectionSupersededError(robot_id)
        try:
            command = await self.apply(command_id, sm.DISPATCH, robot_id=robot_id)
        except Exception:
            # 例如数据库暂时不可用：把消息放回队列稍后重试；sleep 防止"失败-立刻重投-再失败"的空转。
            logger.exception(
                "dispatch failed; message requeued", extra={"command_id": str(command_id)}
            )
            await message.nack(requeue=True)
            await asyncio.sleep(1.0)
            return
        if command is None:
            # 状态机拒绝了 dispatch：命令已是终态（例如已过期、发布失败），或者机器人早已 ack 过。
            # 不再发给机器人，直接把消息从队列里删掉。
            await message.ack()
            return

        # 先登记 Future 再发送：模拟器收到命令后会立刻回 ack，如果先发送后登记，
        # ack 有可能在登记之前就被处理掉，我们就会白等到超时。
        waiter = self._acks.register(command_id, robot_id)
        try:
            payload = CommandToRobotMsg(
                command_id=command.id,
                command=command.type,
                params=command.params,
                attempt=command.attempts,
            ).model_dump(mode="json")
            try:
                await send(payload)
            except Exception as exc:
                raise _RobotGoneError(robot_id) from exc
            acked = await self._wait_for_ack(waiter)
        finally:
            self._acks.discard(command_id, waiter)

        if not acked:
            await self.apply(
                command_id,
                sm.ACK_TIMEOUT,
                f"no ack within {self._ack_timeout_s:g}s",
                robot_id=robot_id,
            )
        # 手动 ack：只有确认机器人收到了（或已判定超时失败）才把消息从队列里删除。
        # 在这之前不管是进程崩溃还是机器人断线，消息都还在 RabbitMQ 手里，不会丢。
        # 和 prefetch_count=1 配合：这条 ack 之后 broker 才会推送这个机器人的下一条命令。
        await message.ack()

    async def _owned_by_another_replica(self, robot_id: str) -> bool:
        """派发前确认：Redis 里的在线标记 robot:{id}:online 是不是指向别的副本？

        "一个机器人只有一条连接"在单个副本内由 RobotRegistry 保证，跨副本却不行：机器人重连到了
        另一个副本，本副本上的旧连接却是半开的（对方早已消失，TCP 还没发现），它的消费者仍在和
        新连接竞争同一个队列——RabbitMQ 轮流投递，一半的命令会被发进一条死连接、白等 ack 超时。
        在线标记由最新的那条连接在建立时写入，并随着遥测不断续期；半开的旧连接收不到遥测，
        不会去改写它。所以标记指向谁，机器人就真正连在谁那里。
        查不到（Redis 故障/超时）时照常派发：宁可退回到"没有这层保护"，
        也不能因为 Redis 挂了就停发命令。
        """
        try:
            async with asyncio.timeout(OWNER_CHECK_TIMEOUT_S):
                owner = await self._bus.get_online_replica(robot_id)
        except Exception as exc:
            self._owner_check_errors.log(
                logger,
                logging.WARNING,
                "robot ownership check failed; dispatching anyway",
                extra={"robot_id": robot_id, "error": repr(exc)},
            )
            return False
        return owner is not None and owner != self._replica

    async def _wait_for_ack(self, waiter: asyncio.Future[None]) -> bool:
        try:
            await asyncio.wait_for(waiter, timeout=self._ack_timeout_s)
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------------ 过期死信

    async def run_expired_consumer(self) -> None:
        """常驻任务：消费 cmd.expired（死信）。所有副本都消费同一个队列——竞争消费者模式，
        每条消息只会被其中一个副本拿到；某个副本挂了，其余副本照常处理。

        和机器人队列一样用普通 channel + 自己重开：如果交给 aio-pika 自动恢复，消费循环一旦因异常
        退出，被丢下的消费者会在重连后复活成幽灵消费者，按 prefetch 扣住一批死信永远不 ack，
        那些命令就再也不会被标记为 expired。
        """
        await self._keep_consuming(self._consume_expired_queue, name="expired-queue", extra={})

    async def _consume_expired_queue(self) -> None:
        channel, queue = await self._mq.open_expired_queue()
        try:
            async with queue.iterator() as messages:
                async for message in messages:
                    await self._handle_expired(message)
        finally:
            await close_channel_quietly(channel)

    async def _handle_expired(self, message: AbstractIncomingMessage) -> None:
        command_id = _parse_command_id(message)
        if command_id is None:
            logger.warning("dead-lettered message without a valid message_id dropped")
            await message.ack()
            return
        try:
            await self.apply(
                command_id, sm.EXPIRE, "TTL elapsed while waiting in the queue (dead-lettered)"
            )
        except Exception:
            logger.exception(
                "expire failed; message requeued", extra={"command_id": str(command_id)}
            )
            await message.nack(requeue=True)
            await asyncio.sleep(1.0)
            return
        await message.ack()


def _parse_command_id(message: AbstractIncomingMessage) -> uuid.UUID | None:
    try:
        return uuid.UUID(message.message_id or "")
    except ValueError:
        return None
