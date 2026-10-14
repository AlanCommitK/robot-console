"""认证用到的密码学工具：bcrypt 口令哈希、JWT 签发与校验、机器人 token 摘要。

两种"秘密"，两种存法
--------------------
- 人的口令（admin123）：熵很低，可以被字典/暴力枚举。所以用 bcrypt —— 一种"故意很慢"的哈希
  （每次约几百毫秒，且自带随机盐）。数据库泄露后，攻击者每猜一次都要付出同样的代价。
- 机器人的 token：应该是机器生成的长随机串（生产环境用 secrets.token_urlsafe(32)），
  熵足够高，根本枚举不完，用快速的 sha256 就够了。而机器人断线重连很频繁，
  每次握手都跑一遍 bcrypt 会白白烧 CPU。
  只存 sha256 摘要而不存明文：数据库被拖库时，拿到的摘要无法反推出能用的 token。
  （本项目的 dev-token-robot-N 只是演示用的低熵 token。）

凭证不放进 URL
--------------
URL 会被写进各种日志：Nginx 的 error log 遇到上游出错时会写出完整的请求行（含查询参数），
uvicorn 也会记录 WebSocket 的请求路径。所以长期有效的凭证一律放在请求头里：
- 机器人：WebSocket 握手时带 `Authorization: Bearer <robot token>`；
- 浏览器看板：浏览器的 WebSocket API 不能自定义请求头，于是先用 JWT（在请求头里）调用
  POST /api/ws-ticket 换一张"一次性票据"，再用 /ws/dashboard?ticket=... 建立连接。
  票据只能用一次（连接时用 Redis GETDEL 原子地取出并删除）、30 秒后自动作废，
  并记着 JWT 的过期时间（WebSocket 不会比签发它的 JWT 活得更久）。
  所以票据即使出现在日志里也无害：看到它的人拿到的要么已经被用掉，要么最多 30 秒后作废。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

JWT_ALGORITHM = "HS256"
ROLES = ("admin", "viewer")
# bcrypt 算法只使用口令的前 72 个字节；bcrypt 5.x 对更长的输入直接抛 ValueError。
BCRYPT_MAX_PASSWORD_BYTES = 72
WS_TICKET_TTL_S = 30


# --------------------------------------------------------------------------- 口令（bcrypt）


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError("password is longer than 72 bytes")
    # gensalt() 每次生成新的随机盐，并把盐和代价因子一起编码进结果字符串（$2b$12$...），
    # 所以同一个口令两次哈希结果不同，校验时也不需要单独保存盐。
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        return False  # 我们从不为这么长的口令生成哈希，所以它不可能匹配
    try:
        return bcrypt.checkpw(raw, password_hash.encode("ascii"))
    except ValueError:  # 数据库里的哈希格式损坏
        return False


# --------------------------------------------------------------------------- JWT


@dataclass(frozen=True)
class TokenClaims:
    username: str
    role: str
    expires_at: datetime


def create_access_token(
    username: str,
    role: str,
    *,
    secret: str,
    expire_minutes: int,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """签发 HS256 JWT，返回 (token, 过期时间)。

    JWT 的 exp 是整数秒，这里先把过期时间截到整秒，保证返回给前端的 expires_at
    和 token 里真正生效的 exp 完全一致。
    """
    issued_at = now or datetime.now(UTC)
    expires_at = (issued_at + timedelta(minutes=expire_minutes)).replace(microsecond=0)
    payload = {"sub": username, "role": role, "exp": expires_at}
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token, expires_at


def decode_access_token(token: str, *, secret: str) -> TokenClaims:
    """校验签名和过期时间；任何问题都抛 jwt.InvalidTokenError（过期是它的子类）。"""
    payload = jwt.decode(
        token,
        secret,
        # 显式白名单算法：防止攻击者在 header 里写 alg=none 或换成别的算法绕过签名校验。
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
    role = payload.get("role")
    if role not in ROLES:
        raise jwt.InvalidTokenError("token has no valid role claim")
    return TokenClaims(
        username=payload["sub"],
        role=role,
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )


def role_allowed(role: str, allowed: Collection[str]) -> bool:
    return role in allowed


# --------------------------------------------------------------------------- 看板 WebSocket 票据


def new_ws_ticket() -> str:
    # 32 字节（256 bit）密码学安全的随机数，编码成 URL 安全的字符串：被猜中的概率可以忽略。
    return secrets.token_urlsafe(32)


def ws_ticket_payload(claims: TokenClaims) -> str:
    """票据在 Redis 里对应的值：换票时 JWT 里的身份，以及 JWT 的过期时间（epoch 秒）。"""
    return json.dumps(
        {"sub": claims.username, "role": claims.role, "exp": int(claims.expires_at.timestamp())}
    )


def parse_ws_ticket_payload(payload: str) -> TokenClaims:
    """ws_ticket_payload 的逆操作；内容不对时抛 ValueError（例如 Redis 里的值被人改过）。"""
    try:
        data = json.loads(payload)
        username, role, exp = str(data["sub"]), data["role"], int(data["exp"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("malformed ws ticket payload") from exc
    if role not in ROLES:
        raise ValueError("ws ticket has no valid role")
    return TokenClaims(username=username, role=role, expires_at=datetime.fromtimestamp(exp, UTC))


# --------------------------------------------------------------------------- 机器人 token


def hash_robot_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_robot_token(token: str, token_hash: str) -> bool:
    # compare_digest 的耗时与"第几个字符开始不同"无关，避免通过响应时间逐字符猜测（时序攻击）。
    return hmac.compare_digest(hash_robot_token(token), token_hash)
