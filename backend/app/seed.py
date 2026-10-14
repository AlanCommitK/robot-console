"""演示数据（SPEC §5）。幂等：运行多少次结果都一样。用法：python -m app.seed

幂等靠 INSERT ... ON CONFLICT DO NOTHING：已存在的行（按唯一键/主键判断）直接跳过，
不会报错，也不会覆盖已有数据（例如别人改过的口令）。即使两个进程同时运行也是安全的，
因为"是否冲突"由数据库在插入的那一刻原子地判断，而不是程序先查再插。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import create_engine, create_sessionmaker
from app.middleware import configure_logging
from app.models import Robot, User
from app.security import hash_password, hash_robot_token

logger = logging.getLogger("app.seed")

SEED_USERS: tuple[tuple[str, str, str], ...] = (
    ("admin", "admin123", "admin"),
    ("viewer", "viewer123", "viewer"),
)
SEED_ROBOTS: tuple[tuple[str, str, str], ...] = tuple(
    (f"robot-{n}", f"AGV-{n:02d}", f"dev-token-robot-{n}") for n in range(1, 5)
)


async def seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """插入缺失的用户和机器人；返回 (新插入的用户数, 新插入的机器人数)。"""
    user_rows = [
        {"username": username, "password_hash": hash_password(password), "role": role}
        for username, password, role in SEED_USERS
    ]
    robot_rows = [
        {"id": robot_id, "name": name, "token_hash": hash_robot_token(token)}
        for robot_id, name, token in SEED_ROBOTS
    ]
    async with sessionmaker() as session:
        users_stmt = (
            pg_insert(User)
            .values(user_rows)
            .on_conflict_do_nothing(index_elements=["username"])
            .returning(User.id)
        )
        robots_stmt = (
            pg_insert(Robot)
            .values(robot_rows)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Robot.id)
        )
        # RETURNING 只返回真正插入的行，被跳过的行不会出现，正好用来统计。
        users_inserted = len((await session.execute(users_stmt)).all())
        robots_inserted = len((await session.execute(robots_stmt)).all())
        await session.commit()
    return users_inserted, robots_inserted


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.replica_id)
    engine = create_engine(settings.database_url)
    try:
        users, robots = await seed(create_sessionmaker(engine))
    finally:
        await engine.dispose()
    logger.info("seed complete", extra={"users_inserted": users, "robots_inserted": robots})


if __name__ == "__main__":
    asyncio.run(main())
