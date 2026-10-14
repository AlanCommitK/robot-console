"""RabbitMQ 管道层（aio-pika）：连接、声明拓扑、发布命令、为每个机器人开消费 channel。

这里只管"消息怎么进出 RabbitMQ"，不含业务逻辑；收到消息后怎么驱动状态机在 commands_service.py。

拓扑（SPEC §12）
----------------
1. 每个机器人一个持久队列 cmd.robot.{robot_id}，参数 x-message-ttl = COMMAND_TTL_MS、
   x-dead-letter-exchange = cmd.dlx。
2. API 通过"默认交换机"发布命令：routing key 写队列名，消息就直接进入同名队列。
3. 机器人在线时，它所在副本的消费者从队列里取消息、派发给机器人。
4. 消息在队列里待满 TTL 还没被取走（机器人一直不在线），RabbitMQ 把它转投到死信交换机
   cmd.dlx（fanout 类型），再由它投进持久队列 cmd.expired。
5. 所有副本竞争消费 cmd.expired，拿到消息的那个副本把对应命令标记为 expired。

死信交换机（DLX）把"超时"变成了一条普通消息：RabbitMQ 到期会主动把消息投到 cmd.expired，
任何一个副本收到后应用 expire 事件即可，这是过期的主路径。只有"根本没有消息可以过期"的命令
（例如进程恰好在数据库提交之后、发布之前被杀）才需要 sweeper.py 定期扫库兜底。

连接是 robust 的，channel 却故意不是
------------------------------------
connect_robust() 返回的"健壮连接"会在 RabbitMQ 重启/断网后自动重连，这正是我们想要的。
但它的 connection.channel() 返回的是 robust channel：aio-pika 把它登记在连接上，重连后自动
重新打开，并把当时登记过的队列声明、消费者（连同回调函数）原样恢复。问题在于"恢复"不区分
这个 channel 还有没有人在用——如果我们的代码已经放弃了它（机器人在断线期间离开、
消费循环因异常退出、发布 channel 出错被丢弃），重连后它照样被"复活"：
- 消费 channel 复活成"幽灵消费者"：broker 按 prefetch 把消息推给它，回调把消息塞进一个
  再也没人读的内存队列，消息永远不 ack——命令既不会下发，也不会过期（未 ack 的消息不受 TTL 约束）；
- 被丢弃的发布 channel 复活成一个没人用、却一直占着的 channel（泄漏）。

所以这里所有 channel 都用普通的 aio_pika.Channel：断线时它就关闭，永远不会被自动恢复。
"重新打开"由使用它的代码自己负责（commands_service.py 里的消费循环、
本文件的 _get_publish_channel），这样每个 channel 的寿命都清清楚楚地跟着拥有它的那段代码走。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from collections.abc import Coroutine
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractQueue, AbstractRobustConnection

logger = logging.getLogger(__name__)

COMMAND_DLX = "cmd.dlx"
EXPIRED_QUEUE = "cmd.expired"
PUBLISH_TIMEOUT_S = 5.0
EXPIRED_PREFETCH = 10


def robot_queue_name(robot_id: str) -> str:
    return f"cmd.robot.{robot_id}"


class MQ:
    def __init__(self, url: str, command_ttl_ms: int) -> None:
        self._url = url
        # 队列参数必须在所有声明它的地方完全一致（发布端、消费端、所有副本）。
        # RabbitMQ 里队列一旦以某组参数创建，再用不同参数声明会被拒绝（PRECONDITION_FAILED）。
        self._robot_queue_arguments: dict[str, Any] = {
            "x-message-ttl": command_ttl_ms,
            "x-dead-letter-exchange": COMMAND_DLX,
        }
        self._connection: AbstractRobustConnection | None = None
        self._publish_channel: AbstractChannel | None = None
        self._publish_channel_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 连接

    async def connect(self, *, max_delay_s: float = 15.0) -> None:
        """一直重试直到 RabbitMQ 可用（指数退避）。

        connect_robust 的"自动重连"只在第一次连上之后才生效；如果启动时 RabbitMQ 还没起来，
        它会直接抛异常。所以第一次连接要自己重试。
        """
        delay = 0.5
        while True:
            try:
                self._connection = await aio_pika.connect_robust(self._url)
                logger.info("connected to rabbitmq")
                return
            except Exception as exc:
                logger.warning(
                    "rabbitmq not available yet; retrying",
                    extra={"retry_in_s": delay, "error": repr(exc)},
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay_s)

    @property
    def connection(self) -> AbstractRobustConnection:
        if self._connection is None:
            raise RuntimeError("MQ.connect() has not been called")
        return self._connection

    def is_connected(self) -> bool:
        connection = self._connection
        return connection is not None and not connection.is_closed and connection.connected.is_set()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def _open_channel(self, *, publisher_confirms: bool = False) -> AbstractChannel:
        """在当前连接上打开一个普通（非 robust）channel。

        先等连接就绪：RabbitMQ 断线期间这里会一直等到 aio-pika 重连成功，调用方不用自己轮询；
        调用方用 asyncio.timeout 或任务取消来决定最多等多久。
        """
        await self.connection.ready()
        channel = aio_pika.Channel(
            self.connection,
            publisher_confirms=publisher_confirms,
            # 只对开了发布确认的 channel 有意义：消息无法路由（没有匹配的队列）时 broker 会退回它，
            # 让 publish() 抛异常，而不是当作发送成功。
            on_return_raises=publisher_confirms,
        )
        await channel  # await 一个新建的 Channel = 真正向 broker 打开它
        return channel

    # ------------------------------------------------------------------ 过期队列（DLX）

    async def open_expired_queue(self) -> tuple[AbstractChannel, AbstractQueue]:
        """开一个新 channel，声明 cmd.dlx + cmd.expired 并绑定，返回 (channel, cmd.expired 队列)。

        每次（重新）打开都完整声明一遍拓扑。声明是幂等的：已存在且参数相同就什么都不做。
        RabbitMQ 的数据被清空过（例如删了卷）时，这一步会把 DLX 重新建出来——否则机器人队列里
        过期的消息会因为"死信交换机不存在"被 RabbitMQ 直接丢弃。
        """
        channel = await self._open_channel()
        try:
            await channel.set_qos(prefetch_count=EXPIRED_PREFETCH)
            dlx = await channel.declare_exchange(COMMAND_DLX, ExchangeType.FANOUT, durable=True)
            queue = await channel.declare_queue(EXPIRED_QUEUE, durable=True)
            await queue.bind(dlx)  # fanout 交换机忽略 routing key，所有死信都进 cmd.expired
        except BaseException:
            await close_channel_quietly(channel)
            raise
        return channel, queue

    # ------------------------------------------------------------------ 每个机器人的消费 channel

    async def open_robot_queue(self, robot_id: str) -> tuple[AbstractChannel, AbstractQueue]:
        """为一个在线机器人开一个专用 channel（prefetch_count=1）并声明它的队列。

        为什么每个机器人一个 channel：
        - prefetch 是按 channel/消费者生效的。prefetch_count=1 表示"手里有一条没 ack 的消息时，
          不要再推第二条"，保证同一个机器人的命令严格一条一条执行，顺序不乱。
        - 机器人断线时只关闭它自己的 channel：RabbitMQ 会把这个 channel 上所有未 ack 的消息
          重新放回队列（requeue），别的机器人完全不受影响。

        这个 channel 的寿命跟着机器人的 WebSocket 走，所以必须是普通 channel（见模块说明）：
        机器人恰好在 RabbitMQ 断线期间离开时，消费者没法在 broker 上取消；如果用 robust channel，
        重连后它会被复活成幽灵消费者，按 prefetch=1 吞掉一条命令、永远不 ack。
        """
        channel = await self._open_channel()
        try:
            await channel.set_qos(prefetch_count=1)
            queue = await channel.declare_queue(
                robot_queue_name(robot_id), durable=True, arguments=self._robot_queue_arguments
            )
        except BaseException:
            await close_channel_quietly(channel)
            raise
        return channel, queue

    # ------------------------------------------------------------------ 发布

    async def open_publish_channel(self) -> None:
        """启动时（lifespan 里）就把发布用的 channel 建好，而不是等第一个 POST 请求来了再建。

        失败也不影响启动：只记一条日志，第一次发布时 _get_publish_channel 会再打开一次。
        """
        try:
            async with asyncio.timeout(PUBLISH_TIMEOUT_S):
                await self._get_publish_channel()
        except Exception:
            logger.warning("could not open the publish channel at startup", exc_info=True)

    async def publish_command(
        self, robot_id: str, command_id: uuid.UUID, command: str, params: dict[str, Any]
    ) -> None:
        """发布一条持久化命令消息，并等待 broker 的发布确认（publisher confirm）。

        publisher confirms：channel 开启确认模式后，broker 在消息被可靠接收（持久消息写入磁盘）后
        回一个 basic.ack；publish() 会一直等到这个确认才返回。拿到确认之前我们不能告诉客户端
        "命令已入队"——否则 broker 恰好在此刻崩溃，消息就悄无声息地丢了。
        """
        queue_name = robot_queue_name(robot_id)
        body = json.dumps(
            {"command_id": str(command_id), "command": command, "params": params}
        ).encode()
        message = Message(
            body,
            # PERSISTENT + 持久队列：broker 重启后消息还在。
            delivery_mode=DeliveryMode.PERSISTENT,
            # message_id 会跟着消息一起进入死信，过期消费者靠它找到对应的命令。
            message_id=str(command_id),
            content_type="application/json",
        )
        channel: AbstractChannel | None = None
        try:
            # 整个发布过程（取 channel、声明队列、发布、等确认）共用一个总超时。
            # RabbitMQ 断线期间，打开 channel 会一直等到重连成功；不设总超时的话，
            # HTTP 请求会一直挂着，而不是按约定返回 503。
            async with asyncio.timeout(PUBLISH_TIMEOUT_S):
                channel = await self._get_publish_channel()
                # 发布前先声明队列（幂等）：确保消息"有地方去"。
                # 默认交换机按 routing key 投递到同名队列，队列不存在的话消息会被直接丢弃。
                await channel.declare_queue(
                    queue_name, durable=True, arguments=self._robot_queue_arguments
                )
                await channel.default_exchange.publish(message, routing_key=queue_name)
        except Exception:
            # 出错后这个 channel 的状态不可信（可能已被 broker 关闭），丢掉，下次发布重新开一个。
            if channel is not None:
                await self._discard_publish_channel(channel)
            raise

    async def _get_publish_channel(self) -> AbstractChannel:
        async with self._publish_channel_lock:
            channel = self._publish_channel
            if channel is None or channel.is_closed:
                # 断线后普通 channel 会自己变成 closed，这里在下一次发布时重新打开一个。
                channel = await _in_fresh_context(self._open_channel(publisher_confirms=True))
                self._publish_channel = channel
            return channel

    async def _discard_publish_channel(self, channel: AbstractChannel) -> None:
        async with self._publish_channel_lock:
            if self._publish_channel is channel:
                self._publish_channel = None
        await close_channel_quietly(channel)


async def _in_fresh_context[T](coro: Coroutine[Any, Any, T]) -> T:
    """在一个空的 contextvars 上下文里运行协程（用来打开长期存在的发布 channel）。

    asyncio 的任务和 future 回调会复制"创建它们那一刻"的 contextvars。aiormq 打开 channel 时
    会创建一个读帧的后台任务并注册关闭回调，它们会永远带着当时的上下文。如果这一刻正好处在
    某个 HTTP 请求里，之后这个 channel 出问题时 aio-pika/aiormq 打出的每一行日志都会带上那个
    毫不相干的 request_id（见 middleware.py 的 request_id_var），排查时非常误导人。
    放进一个以空上下文创建的任务里打开，就切断了这种"继承"。
    """
    return await asyncio.create_task(coro, context=contextvars.Context())


async def close_channel_quietly(channel: AbstractChannel) -> None:
    try:
        await channel.close()
    except Exception:
        logger.debug("ignoring error while closing channel", exc_info=True)
