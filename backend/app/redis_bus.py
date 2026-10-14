"""Redis：机器人实时状态缓存（键）+ 跨副本事件总线（pub/sub 频道 rc:events）。

为什么需要 pub/sub
------------------
两个 API 副本在 Nginx 后面轮询负载均衡：机器人可能连在 api-1，看板却连在 api-2。
api-1 收到遥测后 PUBLISH 到 rc:events，所有副本都订阅这个频道，再各自转发给"连在自己身上"的看板。
这样任何看板都能看到任何机器人，副本之间不需要互相知道对方的存在。

每个副本只有一个订阅者（不是每个看板一个）
------------------------------------------
订阅会独占一条 Redis 连接。每个看板一条订阅的话，100 个看板就是 100 条连接，同一条消息
Redis 要发 100 遍。我们让每个副本只开一条订阅连接，收到一次，在进程内复制给本地看板（fan-out）。

pub/sub 是"发后即忘"（at-most-once）
-----------------------------------
订阅者断线期间发布的消息直接丢失，Redis 不会补发。这对看板可以接受：它是尽力而为的实时视图，
刷新页面时会通过 REST 接口从数据库/缓存重新拉一遍真实状态。需要可靠投递的命令走 RabbitMQ。

发件箱（outbox）：业务代码从不等待"通知看板"
--------------------------------------------
命令状态变化（command_update）只是给看板的通知，状态本身早已提交到 PostgreSQL。
如果每次都 await 一次 PUBLISH，Redis 一慢，处理机器人 ack/result 的代码也跟着卡住。
所以 publish_nowait() 只把事件放进本副本的有界队列（立即返回），由 run_outbox() 这个后台任务
按先后顺序发出去；队列满了（Redis 长时间不可用）就丢弃并限流记日志。
同一副本内事件的先后顺序不变；跨副本的顺序本来就无法保证，前端按 (状态等级, attempts, updated_at)
丢弃旧版本（frontend/src/pages/dashboardState.ts）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from app.log_throttle import LogThrottle

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "rc:events"
LOG_HISTORY_SIZE = 200
OUTBOX_SIZE = 1000
OUTBOX_RETRY_DELAY_S = 1.0
SUBSCRIBER_MAX_RETRY_S = 5.0

# 原子的"比较后删除"：只有 online 键的值仍然是本副本时才删除。
# 不能先 GET 再 DEL 两条命令：两条命令之间，机器人可能已经重连到另一个副本并改写了这个键，
# 我们就会把别人的在线标记删掉。Lua 脚本在 Redis 里是原子执行的，中间不会插入其他命令。
# 返回值：1 = 删除了；0 = 键已经不存在（例如 TTL 到期）；-1 = 键属于别的副本。
_DELETE_IF_OWNER_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
elseif current then
  return -1
end
return 0
"""
OWNED_BY_OTHER_REPLICA = -1


def state_key(robot_id: str) -> str:
    return f"robot:{robot_id}:state"


def online_key(robot_id: str) -> str:
    return f"robot:{robot_id}:online"


def logs_key(robot_id: str) -> str:
    return f"robot:{robot_id}:logs"


def ws_ticket_key(ticket: str) -> str:
    return f"ws-ticket:{ticket}"


def create_redis(url: str) -> Redis:
    # 显式写出超时和重试：redis-py 8 默认会重试 10 次，Redis 宕机时每条命令都要卡好几秒，
    # 会把 WebSocket 消息处理一起卡住。这里只重试 2 次，失败就快速报错，由调用方决定怎么办。
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
        health_check_interval=30,
        retry=Retry(ExponentialBackoff(cap=1.0, base=0.1), retries=2),
    )


@dataclass(frozen=True)
class RobotLiveState:
    online_replica: str | None  # robot:{id}:online 的值；None 表示键不存在（离线）
    state: dict[str, str]  # robot:{id}:state 哈希的全部字段（可能为空）


class RedisBus:
    def __init__(self, redis: Redis, *, outbox_size: int = OUTBOX_SIZE) -> None:
        self._redis = redis
        self._delete_if_owner = redis.register_script(_DELETE_IF_OWNER_LUA)
        self._outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=outbox_size)
        self._outbox_errors = LogThrottle(interval_s=10.0)
        self._subscriber_errors = LogThrottle(interval_s=10.0)

    # ------------------------------------------------------------------ 发布

    async def publish(self, event: dict[str, Any]) -> None:
        await self._redis.publish(EVENTS_CHANNEL, json.dumps(event))

    def publish_nowait(self, event: dict[str, Any]) -> None:
        """不等待 Redis：放进发件箱就返回（见模块说明）。发件箱满了就丢弃这条通知。"""
        try:
            self._outbox.put_nowait(json.dumps(event))
        except asyncio.QueueFull:
            self._outbox_errors.log(
                logger,
                logging.WARNING,
                "redis outbox full; event dropped",
                extra={"size": self._outbox.maxsize},
            )

    async def run_outbox(self) -> None:
        """常驻任务：按顺序把发件箱里的事件 PUBLISH 出去。

        失败的那条直接丢掉（尽力而为），稍等再继续。
        """
        while True:
            text = await self._outbox.get()
            try:
                await self._redis.publish(EVENTS_CHANNEL, text)
            except Exception as exc:
                self._outbox_errors.log(
                    logger,
                    logging.WARNING,
                    "redis publish failed; event dropped",
                    extra={"error": repr(exc), "backlog": self._outbox.qsize()},
                )
                # Redis 宕机时别每条事件都立刻撞一次墙；积压的事件留在队列里，恢复后继续发。
                await asyncio.sleep(OUTBOX_RETRY_DELAY_S)

    async def save_telemetry(
        self,
        robot_id: str,
        fields: dict[str, str | float],
        *,
        replica: str,
        online_ttl_s: int,
        event: dict[str, Any],
    ) -> None:
        # pipeline：三条命令一次发出去、一次收回结果，只花一个网络往返（RTT）。
        # transaction=False：这里不需要 MULTI/EXEC 的原子性，只是想省往返。
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.hset(state_key(robot_id), mapping=fields)
            # 在线标记是一个"租约"：每条遥测都续期 TTL 秒。副本崩溃、来不及清理时，
            # 键会在 TTL 后自动消失，机器人自然显示为离线。
            pipe.set(online_key(robot_id), replica, ex=online_ttl_s)
            pipe.publish(EVENTS_CHANNEL, json.dumps(event))
            await pipe.execute()

    async def set_online(
        self, robot_id: str, *, replica: str, online_ttl_s: int, event: dict[str, Any]
    ) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.set(online_key(robot_id), replica, ex=online_ttl_s)
            pipe.publish(EVENTS_CHANNEL, json.dumps(event))
            await pipe.execute()

    async def clear_online_if_owner(self, robot_id: str, *, replica: str) -> int:
        result = await self._delete_if_owner(keys=[online_key(robot_id)], args=[replica])
        return int(result)

    async def save_log(self, robot_id: str, log_json: str, *, event: dict[str, Any]) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.lpush(logs_key(robot_id), log_json)
            # LPUSH 把最新的放在表头，LTRIM 0 199 只保留最新 200 条：一个有界的环形日志。
            pipe.ltrim(logs_key(robot_id), 0, LOG_HISTORY_SIZE - 1)
            pipe.publish(EVENTS_CHANNEL, json.dumps(event))
            await pipe.execute()

    # ------------------------------------------------------------------ 读取

    async def get_live_states(self, robot_ids: Sequence[str]) -> dict[str, RobotLiveState]:
        async with self._redis.pipeline(transaction=False) as pipe:
            for robot_id in robot_ids:
                pipe.get(online_key(robot_id))
                pipe.hgetall(state_key(robot_id))
            results = await pipe.execute()
        states: dict[str, RobotLiveState] = {}
        for index, robot_id in enumerate(robot_ids):
            online_replica, state = results[2 * index], results[2 * index + 1]
            states[robot_id] = RobotLiveState(online_replica=online_replica, state=state or {})
        return states

    async def get_logs(self, robot_id: str, limit: int) -> list[str]:
        return await self._redis.lrange(logs_key(robot_id), 0, limit - 1)

    async def get_online_replica(self, robot_id: str) -> str | None:
        """robot:{id}:online 的值 = 机器人当前连在哪个副本上；None 表示离线（键不存在）。"""
        replica: str | None = await self._redis.get(online_key(robot_id))
        return replica

    async def ping(self) -> None:
        await self._redis.ping()

    # ------------------------------------------------------------------ 看板 WebSocket 票据

    async def save_ws_ticket(self, ticket: str, payload: str, *, ttl_s: int) -> None:
        # EX：键自带过期时间，没被用掉的票据 ttl_s 秒后由 Redis 自己删除，不用写清理任务。
        await self._redis.set(ws_ticket_key(ticket), payload, ex=ttl_s)

    async def take_ws_ticket(self, ticket: str) -> str | None:
        """取出并删除票据（GETDEL，一条命令、原子执行）；票据不存在/已过期/已被用过时返回 None。

        为什么不能 GET 再 DEL：两条命令之间，另一个副本可能也 GET 到了同一张票据，
        一张票据就能建立两条连接。GETDEL 在 Redis 里一步完成，第二个人一定拿到 None。
        """
        payload: str | None = await self._redis.getdel(ws_ticket_key(ticket))
        return payload

    # ------------------------------------------------------------------ 订阅

    async def run_subscriber(self, on_message: Callable[[str], None]) -> None:
        """常驻任务：订阅 rc:events，把每条消息（原始 JSON 字符串）交给 on_message。

        断线后自动重连（指数退避）。on_message 必须是同步且不阻塞的：它只负责把消息放进各个
        看板的队列，真正的网络发送由每个看板自己的发送任务完成（见 ws_hub.py）。
        """
        delay = 1.0
        while True:
            pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(EVENTS_CHANNEL)
                logger.info("subscribed to redis channel", extra={"channel": EVENTS_CHANNEL})
                delay = 1.0
                while True:
                    # 带超时的轮询：没有消息时最多等 1 秒返回 None，然后继续等。
                    message = await pubsub.get_message(timeout=1.0)
                    if message is not None and message["type"] == "message":
                        on_message(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception:
                self._subscriber_errors.log(
                    logger,
                    logging.WARNING,
                    "redis subscriber failed; reconnecting",
                    extra={"retry_in_s": delay},
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                # 上限只有 5 秒：订阅断开期间所有看板都收不到推送，Redis 恢复后要尽快接上；
                # 每个副本只有这一条订阅连接，每 5 秒试一次对 Redis 毫无压力。
                delay = min(delay * 2, SUBSCRIBER_MAX_RETRY_S)
            finally:
                await _close_quietly(pubsub)


async def _close_quietly(pubsub: Any) -> None:
    try:
        await pubsub.aclose()
    except Exception:
        logger.debug("ignoring error while closing pubsub", exc_info=True)
