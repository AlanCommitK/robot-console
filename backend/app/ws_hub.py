"""本副本上的 WebSocket 连接登记处：看板连接（fan-out + 背压）和机器人连接（一机一连）。

看板：有界队列 + 独立发送任务（背压）
------------------------------------
Redis 订阅任务每收到一条事件，就要发给本副本上的所有看板。如果它逐个 `await websocket.send()`，
一个网络很慢的看板就会让订阅循环卡住：所有看板一起延迟，Redis 那边待发送的数据越积越多，
超过 client-output-buffer-limit 后 Redis 会直接断开我们的订阅连接。

所以：广播只做 put_nowait（立即返回、永不阻塞），把事件放进每个看板自己的
asyncio.Queue(maxsize=1000)；每个看板有一个专属发送任务慢慢把队列里的事件发出去。
队列满了说明这个客户端跟不上，丢弃新事件并计数 rc_ws_dropped_total——慢客户端只伤害自己，
内存占用也有上限（最多 1000 条/连接）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from typing import Any

from starlette.websockets import WebSocket

from app.metrics import WS_DROPPED

logger = logging.getLogger(__name__)

DASHBOARD_QUEUE_SIZE = 1000


# --------------------------------------------------------------------------- 看板


class DashboardClient:
    def __init__(self, websocket: WebSocket, queue_size: int) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.sender: asyncio.Task[None] | None = None

    def offer(self, text: str) -> bool:
        """非阻塞入队；队列满就丢弃并计数。返回是否入队成功。"""
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            WS_DROPPED.inc()
            return False
        return True

    async def run_sender(self) -> None:
        # 只有这个任务会往这条 WebSocket 写数据，所以发送天然是串行的，不需要加锁。
        try:
            while True:
                text = await self.queue.get()
                await self.websocket.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 发送失败通常是客户端已经断开；接收循环会收到 disconnect 并负责清理。
            logger.debug("dashboard sender stopped", exc_info=True)


class DashboardHub:
    def __init__(self, queue_size: int = DASHBOARD_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._clients: set[DashboardClient] = set()

    def __len__(self) -> int:
        return len(self._clients)

    def connect(self, websocket: WebSocket) -> DashboardClient:
        client = DashboardClient(websocket, self._queue_size)
        client.sender = asyncio.create_task(client.run_sender(), name="dashboard-sender")
        self._clients.add(client)
        return client

    async def disconnect(self, client: DashboardClient) -> None:
        self._clients.discard(client)
        if client.sender is not None:
            await cancel_and_wait(client.sender)

    def broadcast(self, text: str) -> None:
        """由 Redis 订阅任务调用：把一条事件复制给本副本上的每个看板（只入队，不等待发送）。"""
        for client in tuple(self._clients):
            client.offer(text)


# --------------------------------------------------------------------------- 机器人


class RobotConnection:
    """一个已认证的机器人 WebSocket 连接，以及挂在它上面的后台任务：
    派发任务（消费这个机器人的 RabbitMQ 队列）和状态写入任务（把遥测/日志写进 Redis）。"""

    def __init__(self, robot_id: str, websocket: WebSocket) -> None:
        self.robot_id = robot_id
        self.websocket = websocket
        # 握手消息、命令消息、关闭帧可能来自不同的 task；用锁保证一次只写一条完整的消息。
        self._send_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_text(json.dumps(payload))

    def start_task(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        self._tasks.append(asyncio.create_task(coro, name=f"{name}:{self.robot_id}"))

    async def stop_tasks(self) -> None:
        """按启动顺序逐个取消并等它们真正结束。

        派发任务最先启动、也最先停：它会在 finally 里关闭自己的 RabbitMQ channel，
        未 ack 的命令随之回到队列，交给这个机器人的下一条连接。
        """
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            await cancel_and_wait(task)

    async def close_socket(self, code: int, reason: str) -> None:
        """只关闭 WebSocket（发送关闭帧），不碰后台任务。

        派发任务自己也会调用它（见 routers/ws.py）：它不能调用 close()，否则会去取消并等待它自己。
        """
        try:
            async with self._send_lock:
                await self.websocket.close(code=code, reason=reason)
        except Exception:
            logger.debug("ignoring error while closing robot socket", exc_info=True)

    async def close(self, code: int, reason: str) -> None:
        await self.stop_tasks()
        await self.close_socket(code, reason)


class RobotRegistry:
    """robot_id -> 本副本上该机器人的当前连接。每个副本上每个机器人最多一条连接。"""

    def __init__(self) -> None:
        self._connections: dict[str, RobotConnection] = {}

    def __len__(self) -> int:
        return len(self._connections)

    def get(self, robot_id: str) -> RobotConnection | None:
        return self._connections.get(robot_id)

    def register(self, connection: RobotConnection) -> RobotConnection | None:
        """登记新连接，返回被它顶替的旧连接（如果有）。调用方负责关闭旧连接（4409 replaced）。"""
        previous = self._connections.get(connection.robot_id)
        self._connections[connection.robot_id] = connection
        return previous

    def unregister(self, connection: RobotConnection) -> bool:
        """只在"它仍然是当前连接"时注销并返回 True。

        被新连接顶替掉的旧连接在清理时会得到 False：此时机器人其实仍然在线（新连接上），
        旧连接绝不能去删除在线标记或广播"离线"。
        """
        if self._connections.get(connection.robot_id) is connection:
            del self._connections[connection.robot_id]
            return True
        return False


async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """取消一个任务并等待它结束，同时不吞掉"当前任务自己被取消"的信号。

    直接 `await task` 会把被取消任务的 CancelledError 抛到这里，很容易被误当成自己被取消。
    asyncio.wait 只等待、不抛被等待任务的异常；如果是当前任务被取消，CancelledError 照常向上传播。
    """
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled() and task.exception() is not None:
        logger.warning("background task failed", exc_info=task.exception())
