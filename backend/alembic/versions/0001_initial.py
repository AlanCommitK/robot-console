"""initial schema (SPEC §6)

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-13

手写而不是 autogenerate：第一版表结构就是项目契约，逐列写出来最容易对照 SPEC 检查。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "users",
        # Integer 主键在 PostgreSQL 上会被渲染成 SERIAL（自增序列）。
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        _created_at(),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("role IN ('admin', 'viewer')", name="ck_users_role"),
    )

    op.create_table(
        "robots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        _created_at(),
    )

    op.create_table(
        "commands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("robot_id", sa.String(64), sa.ForeignKey("robots.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("type IN ('move_to', 'stop', 'set_speed')", name="ck_commands_type"),
        # NULL 在 PostgreSQL 的唯一约束里互不相等：不带 Idempotency-Key 的命令永远不会冲突。
        sa.UniqueConstraint(
            "robot_id", "idempotency_key", name="uq_commands_robot_id_idempotency_key"
        ),
    )
    op.create_index(
        "ix_commands_robot_id_created_at",
        "commands",
        ["robot_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "command_events",
        # BigInteger 主键 -> BIGSERIAL。
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("command_id", sa.Uuid(), sa.ForeignKey("commands.id"), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("replica", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # SPEC 之外的补充：外键列不会自动建索引，按 command_id 查事件需要它。
    op.create_index("ix_command_events_command_id", "command_events", ["command_id"])

    op.create_table(
        "telemetry",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("robot_id", sa.String(64), sa.ForeignKey("robots.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("x", sa.Double(), nullable=False),
        sa.Column("y", sa.Double(), nullable=False),
        sa.Column("theta", sa.Double(), nullable=False),
        sa.Column("speed", sa.Double(), nullable=False),
        sa.Column("battery", sa.Double(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
    )
    op.create_index("ix_telemetry_robot_id_ts", "telemetry", ["robot_id", sa.text("ts DESC")])


def downgrade() -> None:
    # 按外键依赖的反方向删除：先删引用别人的表。
    op.drop_table("telemetry")
    op.drop_table("command_events")
    op.drop_table("commands")
    op.drop_table("robots")
    op.drop_table("users")
