"""Alembic 迁移入口（基于 Alembic 官方的 async 模板）。

数据库地址：从环境变量 DATABASE_URL 读取（通过 app.config.Settings，和应用完全同一个来源）。
驱动选择：直接用应用的异步驱动 asyncpg 跑迁移，不再额外安装同步驱动（psycopg）。
Alembic 的迁移操作本身是同步 API，所以用 `connection.run_sync(...)` 把同步的迁移函数
放到异步连接上执行——这正是 SQLAlchemy 为"在 async 引擎上跑同步代码"提供的桥。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 供 `alembic revision --autogenerate` 对比"模型"和"真实数据库"的差异。
target_metadata = Base.metadata


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式（`alembic upgrade head --sql`）：不连数据库，只把 SQL 打印出来。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # NullPool：迁移是一次性命令，用完即走，不需要连接池。
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
