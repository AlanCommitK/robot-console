"""Prometheus 指标定义（SPEC §13），全部注册在 prometheus_client 的默认注册表里。

没有 replica 标签：Prometheus 抓取每个副本时会自动加上 instance 标签，
自己再加一个只会重复，还会让标签组合数（cardinality）翻倍。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, disable_created_metrics

# 默认情况下每个 Counter/Histogram 还会多导出一条 *_created 时间戳序列，
# 对本项目没有用，只会让 /metrics 输出更难读，所以关掉。
disable_created_metrics()

HTTP_REQUEST_DURATION = Histogram(
    "rc_http_request_duration_seconds",
    "HTTP request latency by route template",
    ["method", "route", "status"],
)

WS_CONNECTIONS = Gauge(
    "rc_ws_connections",
    "Open WebSocket connections on this replica",
    ["kind"],
)

TELEMETRY_MESSAGES = Counter(
    "rc_telemetry_messages_total",
    "Telemetry messages received from robots",
)

WS_INVALID_MESSAGES = Counter(
    "rc_ws_invalid_messages_total",
    "WebSocket messages that failed JSON/schema validation",
    ["kind"],
)

WS_DROPPED = Counter(
    "rc_ws_dropped_total",
    "Dashboard events dropped because the client's send queue was full",
)

COMMAND_TRANSITIONS = Counter(
    "rc_command_transitions_total",
    "Command state transitions by target status",
    ["to_status"],
)

COMMAND_ACK_LATENCY = Histogram(
    "rc_command_ack_latency_seconds",
    "Time from (latest) dispatch to robot ack",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0),
)

TELEMETRY_ROWS_WRITTEN = Counter(
    "rc_telemetry_rows_written_total",
    "Telemetry rows inserted into PostgreSQL",
)


def init_label_sets(statuses: tuple[str, ...]) -> None:
    """预先创建已知的标签组合，让它们从 0 开始存在。

    带标签的指标只有在第一次 .labels(...) 时才会出现在 /metrics 里。如果一个机器人都没连过，
    rc_ws_connections{kind="robot"} 这条序列根本不存在，告警表达式
    sum(rc_ws_connections{kind="robot"}) < 1 的结果是"空"而不是 0，告警就永远不会触发。
    """
    for kind in ("robot", "dashboard"):
        WS_CONNECTIONS.labels(kind=kind)
        WS_INVALID_MESSAGES.labels(kind=kind)
    for status in statuses:
        COMMAND_TRANSITIONS.labels(to_status=status)
