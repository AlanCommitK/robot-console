"""每条机器人连接一个后台任务，把实时状态（最新遥测、日志、在线标记）写进 Redis。

为什么不在 WebSocket 接收循环里直接写
------------------------------------
同一条 WebSocket 连接上的消息是按顺序处理的：接收循环处理完上一条，才会去读下一条。
如果每条遥测都在循环里 await 一次 Redis（HSET + SET + PUBLISH），Redis 一旦变慢或宕机，
每次调用都要等到超时/重试结束（几百毫秒到好几秒），排在遥测后面的 ack/result 只能干等——
派发循环在 ACK_TIMEOUT_S 内等不到 ack，命令就以 ack_timeout 失败了。
遥测只是"看板上的最新位置"，却拖垮了真正要紧的命令确认。

现在接收循环只做内存操作，立即返回：
- 遥测：只保留"最新一条"，新的直接覆盖旧的。后台任务每次醒来只写最新那条——Redis 慢的时候，
  中间的样本自然被跳过，反正看板只关心最新值（历史曲线来自 PostgreSQL，见 telemetry_writer.py）；
- 日志：放进有界队列，后台任务按顺序写入（LPUSH + LTRIM + PUBLISH），写成功才出队，满了丢最旧的。
每条连接的内存占用有上限：一条遥测 + 最多 PENDING_LOGS_LIMIT 条日志。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from app.log_throttle import LogThrottle
from app.redis_bus import RedisBus
from app.schemas import LogEvent, LogOut, RobotStatusEvent, RobotTelemetryMsg, TelemetryEvent

logger = logging.getLogger(__name__)

PENDING_LOGS_LIMIT = 100
RETRY_DELAY_S = 1.0

# 整个进程共用一个限流器：Redis 宕机时所有连接的写入会一起失败，日志里每 10 秒一条就够了。
_redis_errors = LogThrottle(interval_s=10.0)


@dataclass(frozen=True)
class _Sample:
    msg: RobotTelemetryMsg
    received_at: float  # 服务器收到它的时间（epoch 秒），写进 last_seen


class RobotStateWriter:
    def __init__(
        self, robot_id: str, *, bus: RedisBus, replica: str, online_ttl_s: int
    ) -> None:
        self.robot_id = robot_id
        self._bus = bus
        self._replica = replica
        self._online_ttl_s = online_ttl_s
        self._latest: _Sample | None = None
        self._pending_logs: deque[LogOut] = deque(maxlen=PENDING_LOGS_LIMIT)
        self._online_announced = False
        # "有新东西要写"的信号：生产者 set，后台任务醒来后 clear。
        # 后台任务忙着写的时候，再多次 set 也只会唤醒它一次，正好把这期间的新数据合并成一次写入。
        self._wakeup = asyncio.Event()

    # ------------------------------------------------------------------ 接收循环调用（不等待）

    def offer_telemetry(self, msg: RobotTelemetryMsg) -> None:
        self._latest = _Sample(msg, time.time())
        self._wakeup.set()

    def offer_log(self, entry: LogOut) -> None:
        self._pending_logs.append(entry)  # deque(maxlen) 满了会自动挤掉最旧的一条
        self._wakeup.set()

    # ------------------------------------------------------------------ 连接建立时

    async def announce_online(self, timeout_s: float) -> bool:
        """写在线标记 + 广播 robot_status(online)。在开始派发命令之前调用，最多等 timeout_s 秒。

        要在派发之前：派发前的"归属检查"（commands_service._owned_by_another_replica）读的就是
        这个标记，标记还指向旧副本的话，新连接会把自己误判成过时的连接。
        失败（Redis 不可用）也不阻塞连接：后台任务会继续重试，第一条遥测写入时也会续上标记。
        """
        try:
            async with asyncio.timeout(timeout_s):
                await self._announce()
        except Exception as exc:
            self._log_failure(exc)
            self._wakeup.set()  # 让后台任务稍后重试
            return False
        return True

    # ------------------------------------------------------------------ 后台任务

    async def run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            try:
                await self._write_pending()
            except Exception as exc:
                self._log_failure(exc)
                # 别每条遥测都去撞一次已经宕机的 Redis；稍等再试，期间新样本照样覆盖旧样本。
                await asyncio.sleep(RETRY_DELAY_S)
                self._wakeup.set()

    async def _write_pending(self) -> None:
        if not self._online_announced:
            await self._announce()
        sample, self._latest = self._latest, None
        if sample is not None:
            await self._write_telemetry(sample)
        while self._pending_logs:
            entry = self._pending_logs[0]
            await self._bus.save_log(
                self.robot_id,
                entry.model_dump_json(),
                event=LogEvent(
                    robot_id=self.robot_id, ts=entry.ts, level=entry.level, msg=entry.msg
                ).model_dump(mode="json"),
            )
            # 写成功才出队。await 期间队列可能满了、刚写的这条已被挤掉，
            # 这时不能再 popleft（会误删还没写的下一条）。
            if self._pending_logs and self._pending_logs[0] is entry:
                self._pending_logs.popleft()

    async def _announce(self) -> None:
        await self._bus.set_online(
            self.robot_id,
            replica=self._replica,
            online_ttl_s=self._online_ttl_s,
            event=RobotStatusEvent(
                robot_id=self.robot_id, online=True, replica=self._replica
            ).model_dump(),
        )
        self._online_announced = True

    async def _write_telemetry(self, sample: _Sample) -> None:
        msg = sample.msg
        fields: dict[str, str | float] = {
            "ts": msg.ts,
            "x": msg.x,
            "y": msg.y,
            "theta": msg.theta,
            "speed": msg.speed,
            "battery": msg.battery,
            "mode": msg.mode,
            "replica": self._replica,
            "last_seen": sample.received_at,
        }
        event = TelemetryEvent(
            robot_id=self.robot_id,
            replica=self._replica,
            ts=msg.ts,
            x=msg.x,
            y=msg.y,
            theta=msg.theta,
            speed=msg.speed,
            battery=msg.battery,
            mode=msg.mode,
        )
        await self._bus.save_telemetry(
            self.robot_id,
            fields,
            replica=self._replica,
            online_ttl_s=self._online_ttl_s,
            event=event.model_dump(),
        )

    def _log_failure(self, exc: Exception) -> None:
        _redis_errors.log(
            logger,
            logging.WARNING,
            "failed to write robot state to redis",
            extra={"robot_id": self.robot_id, "error": repr(exc)},
        )
