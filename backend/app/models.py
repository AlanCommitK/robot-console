"""ORM 模型：和 alembic/versions/0001_initial.py 建出来的表一一对应（SPEC §6）。

表结构的"真相"在迁移文件里；这里的模型用于在 Python 里读写这些表。两边的约束名、索引名
保持一致，以后用 `alembic revision --autogenerate` 比对差异时才不会出现误报。

没有定义 relationship()：异步模式下关系属性的懒加载会触发隐式 IO（被禁止），
本项目所有关联数据都用显式的 select 查询，读代码时一眼就能看到每条 SQL。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        CheckConstraint("role IN ('admin', 'viewer')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Robot(Base):
    __tablename__ = "robots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # 只存 token 的 sha256 十六进制摘要（固定 64 个字符），从不存明文。原因见 security.py。
    token_hash: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (
        CheckConstraint("type IN ('move_to', 'stop', 'set_speed')", name="ck_commands_type"),
        # 幂等的根基：同一个机器人下 idempotency_key 不能重复。PostgreSQL 里 NULL 互不相等，
        # 所以不带 key 的命令（key 为 NULL）永远不会互相冲突。
        UniqueConstraint(
            "robot_id", "idempotency_key", name="uq_commands_robot_id_idempotency_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(64), ForeignKey("robots.id"))
    type: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommandEvent(Base):
    """命令状态变化的审计流水：每一次成功的状态转移写一行（谁、何时、从哪到哪）。"""

    __tablename__ = "command_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    command_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commands.id"))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text)
    replica: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Telemetry(Base):
    __tablename__ = "telemetry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(64), ForeignKey("robots.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    x: Mapped[float] = mapped_column(Double)
    y: Mapped[float] = mapped_column(Double)
    theta: Mapped[float] = mapped_column(Double)
    speed: Mapped[float] = mapped_column(Double)
    battery: Mapped[float] = mapped_column(Double)
    mode: Mapped[str] = mapped_column(String(16))


# 复合索引。列顺序 (robot_id, 时间 DESC) 对应最常见的查询："某个机器人最近的 N 条"。
Index("ix_commands_robot_id_created_at", Command.robot_id, Command.created_at.desc())
Index("ix_telemetry_robot_id_ts", Telemetry.robot_id, Telemetry.ts.desc())
# SPEC 之外补的一个索引：PostgreSQL 不会自动给外键列建索引，
# 没有它的话"查某条命令的全部事件"会随着表变大退化成全表扫描。
Index("ix_command_events_command_id", CommandEvent.command_id)
