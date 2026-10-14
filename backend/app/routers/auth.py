"""登录、当前用户、看板 WebSocket 票据。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import select

from app.deps import CurrentUser, ServicesDep, SessionDep
from app.models import User
from app.schemas import LoginIn, MeOut, TokenOut, WsTicketOut
from app.security import (
    WS_TICKET_TTL_S,
    create_access_token,
    new_ws_ticket,
    verify_password,
    ws_ticket_payload,
)

router = APIRouter(prefix="/api", tags=["auth"])

# 一个随机字符串的 bcrypt 哈希（代价因子 12，与真实用户相同）。用户名不存在时也用它跑一遍校验，
# 让"用户名不存在"和"密码错误"花的时间一样长，攻击者就无法靠响应时间探测哪些用户名存在。
_DUMMY_PASSWORD_HASH = "$2b$12$v1e1muXpRIVVmyi6ccrvXODHjPBkHAYLpV3MkYK0VaHcbUc780JCu"


@router.post("/auth/login", response_model=TokenOut, responses={401: {"description": "Bad login"}})
async def login(body: LoginIn, session: SessionDep, services: ServicesDep) -> TokenOut:
    stmt = select(User).where(User.username == body.username)
    user = (await session.execute(stmt)).scalar_one_or_none()
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    # bcrypt 故意算得很慢（约 0.2 秒 CPU）。直接在 async 函数里调用会把整个事件循环卡住，
    # 这个副本上所有的 WebSocket 都会跟着停顿；放进线程池执行（bcrypt 计算时会释放 GIL）。
    password_ok = await asyncio.to_thread(verify_password, body.password, password_hash)
    if user is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password"
        )
    settings = services.settings
    token, expires_at = create_access_token(
        user.username,
        user.role,
        secret=settings.jwt_secret,
        expire_minutes=settings.jwt_expire_minutes,
    )
    return TokenOut(
        access_token=token, username=user.username, role=user.role, expires_at=expires_at
    )


@router.get("/me", response_model=MeOut)
async def me(user: CurrentUser) -> MeOut:
    return MeOut(username=user.username, role=user.role)


@router.post(
    "/ws-ticket",
    response_model=WsTicketOut,
    responses={401: {"description": "Missing/invalid JWT"}, 503: {"description": "Redis down"}},
)
async def issue_ws_ticket(user: CurrentUser, services: ServicesDep) -> WsTicketOut:
    """用 JWT（在请求头里）换一张看板 WebSocket 的一次性票据：/ws/dashboard?ticket=...

    为什么不直接把 JWT 放进 WebSocket 的 URL：见 security.py 顶部"凭证不放进 URL"。
    票据存在 Redis 里而不是某个副本的内存里：换票的请求和随后的 WebSocket 握手
    会被 Nginx 分到哪个副本都不一定。
    """
    ticket = new_ws_ticket()
    try:
        await services.bus.save_ws_ticket(ticket, ws_ticket_payload(user), ttl_s=WS_TICKET_TTL_S)
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="live updates unavailable (redis)",
        ) from exc
    return WsTicketOut(ticket=ticket, expires_in=WS_TICKET_TTL_S)
