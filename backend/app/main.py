"""应用入口：组装 FastAPI 应用、中间件、路由，以及启动/关闭时的资源管理（lifespan）。

启动：uvicorn app.main:app
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import state_machine
from app.commands_service import AckRegistry, CommandDispatcher
from app.config import Settings, get_settings
from app.db import create_engine, create_sessionmaker
from app.deps import Services
from app.metrics import init_label_sets
from app.middleware import (
    AccessLogMiddleware,
    MetricsMiddleware,
    RequestIdMiddleware,
    configure_logging,
)
from app.mq import MQ
from app.redis_bus import RedisBus, create_redis
from app.routers import auth, commands, health, robots, ws
from app.sweeper import CommandSweeper, SweepLimits
from app.telemetry_writer import TelemetryWriter
from app.ws_hub import DashboardHub, RobotRegistry

logger = logging.getLogger(__name__)

FLUSH_ON_SHUTDOWN_TIMEOUT_S = 10.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.replica_id)
    init_label_sets(state_machine.STATUSES)

    app = FastAPI(
        title="robot-console API",
        version="1.0.0",
        lifespan=lifespan,
        # 文档放在 /api 下面：Nginx 只把 /api/ 和 /ws/ 转给后端，默认的 /docs 会落到前端的
        # SPA 兜底规则、返回 index.html。Swagger 页面会去请求 openapi_url，所以它也要在 /api 下。
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings

    # Starlette 的 add_middleware 把新中间件放在最外层，所以这里按"由内到外"的顺序添加。
    # 最终一个 HTTP 请求经过的顺序（由外到内）：
    #   RequestId -> AccessLog -> Metrics -> CORS -> 路由
    # RequestId 必须在最外层：内层的访问日志、异常日志都要能读到它设置的 request id。
    app.add_middleware(
        CORSMiddleware,  # 只给 vite 开发服务器用；生产环境前后端同源（都经过 Nginx），用不到
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Replica"],
    )
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware, replica=settings.replica_id)

    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    # 前缀写在各个 APIRouter(prefix=...) 上，而不是 include_router(prefix=...)：
    # 这个 FastAPI 版本里，后者不会出现在 scope["route"].path 里，指标的路由标签会缺少前缀。
    for router in (auth.router, robots.router, commands.router, ws.router, health.router):
        app.include_router(router)
    return app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """yield 之前是启动逻辑，之后是关闭逻辑；uvicorn 在开始/停止接收请求前后各调用一次。"""
    settings: Settings = app.state.settings
    _warn_about_dev_settings(settings)
    services = await _start_services(settings)
    app.state.services = services
    sweeper = CommandSweeper(
        sessionmaker=services.sessionmaker,
        bus=services.bus,
        replica=settings.replica_id,
        limits=SweepLimits.from_settings(
            command_ttl_ms=settings.command_ttl_ms, result_timeout_s=settings.result_timeout_s
        ),
    )

    background = [
        # 每个副本只有一个 Redis 订阅任务，收到事件后在进程内复制给本地看板。
        asyncio.create_task(
            services.bus.run_subscriber(services.hub.broadcast), name="redis-subscriber"
        ),
        # 把发件箱里的看板通知（command_update）按顺序发到 Redis，业务代码从不等待它。
        asyncio.create_task(services.bus.run_outbox(), name="redis-outbox"),
        asyncio.create_task(services.dispatcher.run_expired_consumer(), name="expired-consumer"),
        # 每个副本都跑一个 sweeper，靠 SELECT ... FOR UPDATE SKIP LOCKED 互不重复。
        asyncio.create_task(sweeper.run(), name="command-sweeper"),
    ]
    flusher = asyncio.create_task(services.telemetry.run(), name="telemetry-flusher")
    logger.info("api started")
    try:
        yield
    finally:
        # 走到这里时，uvicorn 已经停止接收新连接，并已让现有 WebSocket 处理器退出
        # （它们在 finally 里关掉了各自的 RabbitMQ channel，未 ack 的命令已回到队列）。
        logger.info("api shutting down")
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        services.telemetry.stop()  # 让落库任务做完最后一次写入后自己退出
        _, pending = await asyncio.wait({flusher}, timeout=FLUSH_ON_SHUTDOWN_TIMEOUT_S)
        for task in pending:
            task.cancel()
        await services.mq.close()
        await services.redis.aclose()
        await services.engine.dispose()
        logger.info("api stopped")


async def _start_services(settings: Settings) -> Services:
    # 数据库引擎和 Redis 客户端都是惰性连接的；只有 RabbitMQ 需要在启动时连上，
    # connect() 会一直重试直到 broker 可用。
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    redis = create_redis(settings.redis_url)
    bus = RedisBus(redis)
    mq = MQ(settings.rabbitmq_url, settings.command_ttl_ms)
    await mq.connect()
    # 发布用的 channel 在这里（不在任何 HTTP 请求里）建好：第一个下发命令的请求不用再等它，
    # aio-pika 内部为它创建的后台任务也不会继承某个请求的 request_id（见 mq._in_fresh_context）。
    await mq.open_publish_channel()
    dispatcher = CommandDispatcher(
        sessionmaker=sessionmaker,
        bus=bus,
        mq=mq,
        acks=AckRegistry(),
        replica=settings.replica_id,
        ack_timeout_s=settings.ack_timeout_s,
    )
    services = Services(
        settings=settings,
        engine=engine,
        sessionmaker=sessionmaker,
        redis=redis,
        bus=bus,
        mq=mq,
        hub=DashboardHub(),
        robots=RobotRegistry(),
        dispatcher=dispatcher,
        telemetry=TelemetryWriter(sessionmaker, settings.telemetry_flush_interval_s),
    )
    return services


def _warn_about_dev_settings(settings: Settings) -> None:
    # 默认密钥写在公开的代码里：任何人都能用它签出 role=admin 的 JWT，长度再长也没用。
    if settings.jwt_secret == Settings.model_fields["jwt_secret"].default:
        logger.warning("JWT_SECRET is the public dev default; anyone can forge tokens with it")
    elif len(settings.jwt_secret.encode()) < 32:
        logger.warning(
            "JWT_SECRET is shorter than 32 bytes; use a long random secret outside local dev"
        )


async def _validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 也保持 {"detail": "<字符串>"} 的统一错误格式；结构化的明细放在 errors 里。"""
    errors = exc.errors()
    summary = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in errors
    )
    return JSONResponse(
        status_code=422,
        content={"detail": summary or "invalid request", "errors": jsonable_encoder(errors)},
    )


app = create_app()
