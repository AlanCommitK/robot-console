"""FastAPI 依赖项：取共享资源、开数据库会话、解析当前用户、校验角色。

认证/鉴权为什么用依赖项而不是中间件：见 middleware.py 顶部的说明。简单说，
"这个接口需要登录""这个接口需要 admin"是按接口声明的，依赖项正好可以按接口挂、可以层层组合
（require_admin 依赖 get_current_user），而且会自动出现在 /docs 的鉴权说明里。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.requests import HTTPConnection

from app.commands_service import CommandDispatcher
from app.config import Settings
from app.mq import MQ
from app.redis_bus import RedisBus
from app.security import TokenClaims, decode_access_token, role_allowed
from app.telemetry_writer import TelemetryWriter
from app.ws_hub import DashboardHub, RobotRegistry


@dataclass
class Services:
    """进程级的共享资源，在 lifespan 启动时创建一次，挂在 app.state.services 上。"""

    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    bus: RedisBus
    mq: MQ
    hub: DashboardHub
    robots: RobotRegistry
    dispatcher: CommandDispatcher
    telemetry: TelemetryWriter


def get_services(connection: HTTPConnection) -> Services:
    # HTTPConnection 是 Request 和 WebSocket 的共同父类，HTTP 接口和 WebSocket 接口都能用这个依赖。
    services: Services = connection.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    # yield 依赖：请求开始时打开会话，响应发出后（无论成功还是异常）自动关闭、归还连接。
    async with services.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

# auto_error=False：缺少 Authorization 头时不让 FastAPI 自动报错，由我们统一返回 401。
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


# 依赖项写成 async def：普通 def 会被 FastAPI 放进线程池执行，而 JWT 校验只要几微秒，
# 直接在事件循环里跑反而更省。
async def get_current_user(
    services: ServicesDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenClaims:
    if credentials is None:
        raise _unauthorized("missing bearer token")
    try:
        return decode_access_token(credentials.credentials, secret=services.settings.jwt_secret)
    except jwt.InvalidTokenError as exc:  # 签名错误、格式错误、已过期……统一 401
        raise _unauthorized("invalid or expired token") from exc


CurrentUser = Annotated[TokenClaims, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> TokenClaims:
    # 401 = "你是谁？"（没登录/令牌无效）；403 = "我知道你是谁，但你没有这个权限"。
    if not role_allowed(user.role, ("admin",)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user


AdminUser = Annotated[TokenClaims, Depends(require_admin)]
