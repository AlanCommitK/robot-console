"""数据库连接：异步引擎（连接池）+ 会话工厂。由 main.py 的 lifespan 在启动时创建。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    # 引擎是"惰性"的：这里不会连数据库，第一次执行 SQL 时才从连接池里建连接。
    # pool_pre_ping：从池里借出连接前先发一个极轻的探测。数据库重启后池里的旧连接已经失效，
    # 不探测的话，重启后的第一个请求会直接报错。
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False：commit 之后不把 ORM 对象的属性标成"过期"。
    # 默认行为是 commit 后再访问属性会自动重新查库（隐式 IO），而在 async 代码里隐式 IO 是不允许的
    # （会抛 MissingGreenlet）。我们经常在 commit 之后还要读字段来组装响应，所以关掉它。
    return async_sessionmaker(engine, expire_on_commit=False)
