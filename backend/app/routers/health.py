"""探活与指标：/api/health（Docker/Nginx 用）和 /metrics（Prometheus 抓取，不经过 /api 前缀）。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.deps import Services, ServicesDep
from app.schemas import HealthOut

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_S = 2.0


@router.get(
    "/api/health",
    response_model=HealthOut,
    responses={503: {"model": HealthOut, "description": "A dependency is down"}},
)
async def health(services: ServicesDep) -> JSONResponse:
    # 三个检查并发执行，每个最多等 2 秒：一个依赖卡死不能把健康检查本身也拖死。
    db, redis, rabbitmq = await asyncio.gather(
        _run_check(lambda: _check_db(services)),
        _run_check(services.bus.ping),
        _run_check(lambda: _check_rabbitmq(services)),
    )
    checks = {"db": db, "redis": redis, "rabbitmq": rabbitmq}
    healthy = all(result == "ok" for result in checks.values())
    body = HealthOut(
        status="ok" if healthy else "degraded",
        replica=services.settings.replica_id,
        checks=checks,
    )
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=body.model_dump())


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _run_check(check: Callable[[], Awaitable[None]]) -> str:
    try:
        await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_S)
    except TimeoutError:
        return f"timeout after {CHECK_TIMEOUT_S:g}s"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return "ok"


async def _check_db(services: Services) -> None:
    async with services.sessionmaker() as session:
        await session.execute(text("SELECT 1"))


async def _check_rabbitmq(services: Services) -> None:
    if not services.mq.is_connected():
        raise ConnectionError("not connected to rabbitmq")
