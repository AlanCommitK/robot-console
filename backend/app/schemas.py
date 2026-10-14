"""Pydantic 模型：REST 请求/响应、WebSocket 消息、Redis 事件的"形状"（SPEC §8–§11）。

把每一种 JSON 都写成一个类型，有三个好处：入口处自动校验、/docs 里自动生成文档、
读代码时一眼能看到契约长什么样。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Role = Literal["admin", "viewer"]
RobotMode = Literal["idle", "moving", "charging", "error"]
LogLevel = Literal["info", "warn", "error"]

# 2100-01-01 的 epoch 秒。用来拒绝荒谬的时间戳（否则转换成 datetime 时可能直接溢出报错）。
_MAX_EPOCH_S = 4_102_444_800


class _FiniteModel(BaseModel):
    # Python 的 json 模块能解析 NaN/Infinity，但 PostgreSQL 的 jsonb 和浏览器的 JSON 都不接受它们；
    # 在入口处就拒绝，避免脏数据流到下游。
    model_config = ConfigDict(allow_inf_nan=False)


class _StrictModel(BaseModel):
    # 操作员提交的命令：多余的字段直接 422，拼写错误（例如 "spd"）不会被悄悄忽略。
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")


# --------------------------------------------------------------------------- 认证


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    username: str
    role: Role
    expires_at: datetime


class MeOut(BaseModel):
    username: str
    role: Role


class WsTicketOut(BaseModel):
    ticket: str
    expires_in: int  # 秒；票据只能用一次，过期前没用掉就作废


# --------------------------------------------------------------------------- 机器人


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    x: float
    y: float
    theta: float
    speed: float
    battery: float
    mode: str


class RobotOut(BaseModel):
    id: str
    name: str
    online: bool
    replica: str | None
    last_seen: datetime | None
    state: TelemetryOut | None


class LogOut(BaseModel):
    ts: datetime
    level: str
    msg: str


# --------------------------------------------------------------------------- 命令


class MoveToParams(_StrictModel):
    x: float = Field(ge=-50, le=50)
    y: float = Field(ge=-50, le=50)


class SetSpeedParams(_StrictModel):
    speed: float = Field(ge=0, le=2.0)


class StopParams(_StrictModel):
    pass


class MoveToCommandIn(_StrictModel):
    type: Literal["move_to"]
    params: MoveToParams


class SetSpeedCommandIn(_StrictModel):
    type: Literal["set_speed"]
    params: SetSpeedParams


class StopCommandIn(_StrictModel):
    type: Literal["stop"]
    params: StopParams = Field(default_factory=StopParams)


# "可辨识联合"（discriminated union）：先看 type 字段，再用对应的模型校验 params。
# 好处：错误信息精确（只报 move_to 的错，不会把三种可能的错误都列出来），OpenAPI 里也是 oneOf。
CommandIn = Annotated[
    MoveToCommandIn | SetSpeedCommandIn | StopCommandIn, Field(discriminator="type")
]


class CommandOut(BaseModel):
    # from_attributes=True：允许直接从 ORM 对象（models.Command）构造，按属性名取值。
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    robot_id: str
    type: str
    params: dict[str, Any]
    status: str
    attempts: int
    error: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None
    acked_at: datetime | None
    finished_at: datetime | None


class CommandEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    detail: str | None
    replica: str | None
    at: datetime


class CommandDetailOut(CommandOut):
    events: list[CommandEventOut]


# --------------------------------------------------------------------------- 健康检查


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    replica: str
    checks: dict[str, str]


# --------------------------------------------------------------------------- 机器人 -> 服务器
# 机器人消息不设 extra="forbid"：以后模拟器多发一个字段，不应该让整条遥测被丢掉。


class RobotTelemetryMsg(_FiniteModel):
    type: Literal["telemetry"]
    ts: float = Field(gt=0, lt=_MAX_EPOCH_S)
    x: float
    y: float
    theta: float
    speed: float
    battery: float = Field(ge=0, le=100)
    mode: RobotMode


class RobotAckMsg(BaseModel):
    type: Literal["ack"]
    command_id: uuid.UUID


class RobotResultMsg(BaseModel):
    type: Literal["result"]
    command_id: uuid.UUID
    status: Literal["succeeded", "failed"]
    detail: str | None = Field(default=None, max_length=500)


class RobotLogMsg(BaseModel):
    type: Literal["log"]
    level: LogLevel
    msg: str = Field(max_length=500)


RobotMessage = Annotated[
    RobotTelemetryMsg | RobotAckMsg | RobotResultMsg | RobotLogMsg, Field(discriminator="type")
]
# TypeAdapter：让"不是 BaseModel 的类型"（这里是联合类型）也能直接 validate_json。
# 非法 JSON 和不符合任何一种消息的 JSON 都抛 ValidationError，调用方统一处理。
robot_message_adapter: TypeAdapter[RobotMessage] = TypeAdapter(RobotMessage)


# --------------------------------------------------------------------------- 服务器 -> 机器人


class HelloMsg(BaseModel):
    type: Literal["hello"] = "hello"
    replica: str
    server_time: float


class CommandToRobotMsg(BaseModel):
    type: Literal["command"] = "command"
    command_id: uuid.UUID
    command: str
    params: dict[str, Any]
    attempt: int


# --------------------------------------------------------------------------- 看板 <-> 服务器


class DashboardPingMsg(BaseModel):
    type: Literal["ping"]


dashboard_message_adapter: TypeAdapter[DashboardPingMsg] = TypeAdapter(DashboardPingMsg)


# Redis 频道 rc:events 上的事件；看板 WebSocket 原样收到这些 JSON（SPEC §10）。


class TelemetryEvent(BaseModel):
    type: Literal["telemetry"] = "telemetry"
    robot_id: str
    replica: str
    ts: float
    x: float
    y: float
    theta: float
    speed: float
    battery: float
    mode: str


class RobotStatusEvent(BaseModel):
    type: Literal["robot_status"] = "robot_status"
    robot_id: str
    online: bool
    replica: str


class CommandUpdateEvent(BaseModel):
    type: Literal["command_update"] = "command_update"
    command: CommandOut


class LogEvent(BaseModel):
    type: Literal["log"] = "log"
    robot_id: str
    ts: datetime
    level: str
    msg: str
