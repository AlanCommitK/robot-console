"""HTTP 中间件（SPEC §8）+ 结构化 JSON 日志。

中间件 vs 依赖项（Depends）
--------------------------
中间件包在整个应用外面，每个请求都会经过，而且发生在路由匹配之前——它不知道"这个接口需要
什么角色"。所以中间件只做"对所有请求一视同仁"的横切关注点：request id、访问日志、指标、CORS。
认证/鉴权是"每个接口要求不同"的，放在依赖项里（见 deps.py）：按接口声明、可以组合、
还会出现在 OpenAPI 文档里。

为什么写成"纯 ASGI 中间件"而不是继承 BaseHTTPMiddleware
--------------------------------------------------------
ASGI 应用就是一个 async 函数 app(scope, receive, send)。纯 ASGI 中间件只是再包一层这样的函数，
在调用内层 app 之前/之后做事，并可以"截获" send 来读取或修改响应头。这样写开销最小、
没有额外的 task，contextvar 也自然地对内层可见；而且能直接看清 HTTP 在 ASGI 里是怎么流动的。
WebSocket 连接的 scope["type"] 是 "websocket"，这些 HTTP 专用的中间件直接放行。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.metrics import HTTP_REQUEST_DURATION

# 当前请求的 request id。contextvar 对"同一个 asyncio task 里后续调用的所有代码"可见，
# 所以日志格式化器不需要层层传参就能拿到它。
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

logger = logging.getLogger("app.http")
access_logger = logging.getLogger("app.access")

# 只接受"看起来正常"的外部 request id，防止有人塞换行或超长字符串污染日志（日志注入）。
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# 凭证已经不走 URL 了（机器人用 Authorization 头，看板用一次性票据，见 security.py），
# 但 uvicorn 会把 WebSocket 的请求路径连同查询串写进日志，旧版或配错的客户端仍可能把
# token=... 放在 URL 里。这里统一打码，作为最后一道防线。
# 看板的 ticket=... 不打码：它只能用一次、30 秒后作废，出现在日志里没有危害，留着反而方便排查。
_TOKEN_IN_TEXT_RE = re.compile(r"(token=)[^&\s\"']+")
# 探活和抓取指标的请求非常频繁（每几秒一次），记成 DEBUG，免得淹没真正的业务请求日志。
_QUIET_PATHS = frozenset({"/api/health", "/metrics"})
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# LogRecord 自带的属性；除此之外的属性都是调用方通过 extra={...} 传进来的业务字段。
# color_message 是 uvicorn 附带的、含终端颜色控制符的重复消息，不需要。
_STANDARD_LOG_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)


# --------------------------------------------------------------------------- 日志


class JsonFormatter(logging.Formatter):
    """每条日志输出一行 JSON：便于 grep、jq，也便于日志系统按字段检索。"""

    def __init__(self, replica: str) -> None:
        super().__init__()
        self._replica = replica

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
            "replica": self._replica,
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        # logger.info("...", extra={"robot_id": ...}) 传进来的字段会变成 record 的属性，原样输出。
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _redact(text: str) -> str:
    return _TOKEN_IN_TEXT_RE.sub(r"\1***", text)


def configure_logging(level: str, replica: str) -> None:
    """让整个进程（包括 uvicorn、第三方库）都输出同一种 JSON 日志。"""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(replica))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn 启动时给自己的 logger 装了单独的 handler；清掉，让它们冒泡到 root 统一格式。
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # uvicorn 自带的访问日志与 AccessLogMiddleware 重复，关掉。
    logging.getLogger("uvicorn.access").disabled = True
    # aio-pika/aiormq 在 INFO 级别很啰嗦（每次建 channel 都记一条）。
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    # warnings.warn(...)（例如 PyJWT 的"HMAC 密钥太短"）也走 JSON 日志，而不是裸打到 stderr。
    logging.captureWarnings(True)


# --------------------------------------------------------------------------- 中间件


class RequestIdMiddleware:
    """最外层：分配 request id、给每个 HTTP 响应加 X-Request-ID / X-Replica。

    还兜底"未处理的异常"：把它变成带 request id 的 JSON 500。否则 500 会由更外层的
    Starlette ServerErrorMiddleware 生成，那个响应不经过这里，也就没有这两个响应头。
    """

    def __init__(self, app: ASGIApp, replica: str) -> None:
        self.app = app
        self.replica = replica

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Replica"] = self.replica
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if response_started:  # 响应头已经发出去了，没法再改成 500，只能交给服务器断开连接
                raise
            logger.exception("unhandled error")
            response = JSONResponse(
                {"detail": "internal server error", "request_id": request_id}, status_code=500
            )
            await response(scope, receive, send_with_headers)
        finally:
            request_id_var.reset(token)


class AccessLogMiddleware:
    """每个 HTTP 请求一行 JSON 访问日志（request_id / replica 由 JsonFormatter 自动附加）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500  # 如果内层抛异常且没发出响应，按 500 记

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            level = logging.DEBUG if scope["path"] in _QUIET_PATHS else logging.INFO
            access_logger.log(
                level,
                "http request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )


class MetricsMiddleware:
    """记录 rc_http_request_duration_seconds{method, route, status}。

    route 用"路由模板"（/api/robots/{robot_id}）而不是原始路径（/api/robots/robot-1）：
    Prometheus 里每一种标签组合都是一条独立的时间序列，占内存、拖慢查询。用原始路径的话，
    每个机器人 id、每个命令 uuid、每个扫描器乱敲的 URL 都会生成新序列（基数爆炸）。
    路由匹配成功后，FastAPI 把匹配到的路由对象放进 scope["route"]；中间件和路由共享同一个
    scope 字典，所以内层 app 返回后在这里就能读到。没匹配上的（404）统一记为 "unmatched"。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def send_capturing_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            method = scope["method"] if scope["method"] in _KNOWN_METHODS else "OTHER"
            HTTP_REQUEST_DURATION.labels(
                method=method, route=_route_label(scope), status=str(status)
            ).observe(time.perf_counter() - started)


def _route_label(scope: Scope) -> str:
    route = scope.get("route")
    if route is not None:  # FastAPI 的 APIRoute：路由模板，如 /api/robots/{robot_id}
        return str(route.path)
    if "endpoint" in scope and not scope.get("path_params"):
        # Starlette 原生路由（/docs、/openapi.json）只设置 endpoint；它们没有路径参数，
        # 原始路径就是模板，取值个数有限，不会造成基数爆炸。
        return str(scope["path"])
    return "unmatched"


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _REQUEST_ID_RE.match(candidate) else None
    return None
