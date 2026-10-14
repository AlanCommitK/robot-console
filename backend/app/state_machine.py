"""命令状态机：一个纯函数 next_status(current, event)，不做任何 IO，便于穷举测试。

为什么"不合法/重复/乱序"的事件返回 None 而不是抛异常
----------------------------------------------------
命令链路是 at-least-once（至少一次）投递：RabbitMQ 会重投、机器人断线重连会重发 ack、
DLX 过期通知可能和机器人的结果同时到达、多个 API 副本会并发处理同一条命令的事件。
在这种系统里，"同一个事件来两次"和"事件晚到"是常态，不是错误。

所以状态机只回答一个问题："在当前状态下，这个事件会不会带来新的状态？"
- 会 -> 返回新状态，调用方据此更新数据库；
- 不会（重复 ack、终态之后又来的结果、成功之后才到的过期通知……）-> 返回 None，
  调用方什么都不做，直接返回。

这样"处理一次"和"处理很多次"的结果完全一样（幂等），事件的到达顺序也不会把状态改乱：
终态（succeeded/failed/expired）一旦写入，任何事件都无法再改变它。
"""

from __future__ import annotations

from typing import Final

# ---- 状态 ----
QUEUED: Final = "queued"
DISPATCHED: Final = "dispatched"
ACKED: Final = "acked"
SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
EXPIRED: Final = "expired"

STATUSES: Final = (QUEUED, DISPATCHED, ACKED, SUCCEEDED, FAILED, EXPIRED)
TERMINAL_STATUSES: Final = frozenset({SUCCEEDED, FAILED, EXPIRED})

# ---- 事件 ----
DISPATCH: Final = "dispatch"
ACK: Final = "ack"
RESULT_SUCCEEDED: Final = "result_succeeded"
RESULT_FAILED: Final = "result_failed"
EXPIRE: Final = "expire"
ACK_TIMEOUT: Final = "ack_timeout"
RESULT_TIMEOUT: Final = "result_timeout"

EVENTS: Final = (
    DISPATCH,
    ACK,
    RESULT_SUCCEEDED,
    RESULT_FAILED,
    EXPIRE,
    ACK_TIMEOUT,
    RESULT_TIMEOUT,
)

# 完整的转移表：(当前状态, 事件) -> 新状态。表里没有的组合一律视为"忽略"。
_TRANSITIONS: Final[dict[tuple[str, str], str]] = {
    (QUEUED, DISPATCH): DISPATCHED,
    # 重投：机器人断线时消息没被 ack，重连后 RabbitMQ 再投一次；调用方会把 attempts + 1。
    (DISPATCHED, DISPATCH): DISPATCHED,
    (DISPATCHED, ACK): ACKED,
    # 没收到 ack 就先收到了结果：结果本身就证明机器人收到了命令（隐含 ack）。
    (DISPATCHED, RESULT_SUCCEEDED): SUCCEEDED,
    (DISPATCHED, RESULT_FAILED): FAILED,
    (ACKED, RESULT_SUCCEEDED): SUCCEEDED,
    (ACKED, RESULT_FAILED): FAILED,
    # 命令已入库但发布到 RabbitMQ 失败时（SPEC §8 要求应用 result_failed，
    # detail 'publish_failed'），命令还停在 queued。没有这一行的话，这个事件会被忽略，
    # 命令会永远卡在 queued（消息根本没进队列，也就不会过期）。
    (QUEUED, RESULT_FAILED): FAILED,
    # 过期有两个来源：DLX 的死信通知（主路径），以及 sweeper 对"长时间没有任何更新"的兜底清扫。
    (QUEUED, EXPIRE): EXPIRED,
    (DISPATCHED, EXPIRE): EXPIRED,
    (DISPATCHED, ACK_TIMEOUT): FAILED,
    # 机器人 ack 了却迟迟不报结果（例如 ack 之后崩溃重启，结果随之丢失）：由 sweeper 在
    # RESULT_TIMEOUT_S 之后判失败。acked 状态的消息早已从 RabbitMQ 删除，不会再有 DLX 过期，
    # 没有这一行它会永远停在 acked。
    (ACKED, RESULT_TIMEOUT): FAILED,
}


def next_status(current: str, event: str) -> str | None:
    """返回事件触发后的新状态；None 表示"这个事件在当前状态下没有意义，忽略它"。"""
    return _TRANSITIONS.get((current, event))


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
