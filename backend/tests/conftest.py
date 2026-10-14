"""pytest 公共配置。

单元测试：不需要任何外部服务，直接运行 `pytest -q`。
集成测试：标记为 @pytest.mark.integration，只有设置了环境变量 RUN_INTEGRATION=1 才会运行，
需要真实的 PostgreSQL / Redis / RabbitMQ（地址从 DATABASE_URL / REDIS_URL / RABBITMQ_URL 读取），
并且数据库已经执行过 `alembic upgrade head`。种子数据由下面的 fixture 自动写入（幂等）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

RUN_INTEGRATION = os.environ.get("RUN_INTEGRATION") == "1"

# 测试固定使用一个 ≥ 32 字节的密钥（PyJWT 对更短的 HMAC 密钥会发出 InsecureKeyLengthWarning）；
# 环境里已经设置了 JWT_SECRET 时不覆盖。
os.environ.setdefault("JWT_SECRET", "integration-test-secret-that-is-32-bytes-or-more")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if RUN_INTEGRATION:
        return
    skip = pytest.mark.skip(reason="integration test: set RUN_INTEGRATION=1 to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


async def _seed_database() -> None:
    from app.config import get_settings
    from app.db import create_engine, create_sessionmaker
    from app.seed import seed

    engine = create_engine(get_settings().database_url)
    try:
        await seed(create_sessionmaker(engine))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    """启动完整的应用（包括 lifespan：连接 RabbitMQ、启动 Redis 订阅等）供集成测试使用。"""
    from app.main import create_app

    asyncio.run(_seed_database())
    # 用 with 进入 TestClient 才会触发 lifespan 的启动和关闭。
    with TestClient(create_app()) as test_client:
        yield test_client


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


@pytest.fixture(scope="session")
def admin_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, 'admin', 'admin123')}"}


@pytest.fixture(scope="session")
def viewer_headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, 'viewer', 'viewer123')}"}
