"""Robot fleet simulator (SPEC §9, §17).

每个机器人 = 一个 RobotAgent，内部两个并发任务：
- _tick_loop：按 TELEMETRY_HZ 推进物理模型、发遥测/心跳日志。断网时照样推进——
  真实机器人不会因为网络断了就停在半路，正在执行的 move_to 会继续走完。
- _connection_loop：连接 -> 收消息 -> 断开 -> 指数退避+抖动 -> 重连，循环到收到 SIGTERM。
本文件只做 I/O（网络、计时、日志）；“机器人怎么动、命令怎么判结果”都在 robot_model.py。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

import robot_model as rm

DEFAULT_WS_URL = "ws://nginx/ws/robot"
DEFAULT_ROBOTS = ",".join(f"robot-{i}:dev-token-robot-{i}" for i in range(1, 5))
HEARTBEAT_INTERVAL_S = 10.0
OUTBOX_LIMIT = 1000
LOG_MSG_LIMIT = 500
MAX_DT_S = 1.0

logger = logging.getLogger("simulator")


# ---------------------------------------------------------------------------
# Structured logging: one JSON object per line on stdout (`docker compose logs` friendly)
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("websockets").setLevel(logging.WARNING)  # frame-level debug logs are noise


def log(level: int, msg: str, **fields: object) -> None:
    logger.log(level, msg, extra={"fields": fields})


# ---------------------------------------------------------------------------
# Configuration (environment variables of SPEC §17)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    ws_url: str
    robots: list[tuple[str, str]]
    telemetry_hz: float
    chaos_per_min: float
    fail_rate: float
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        cfg = cls(
            ws_url=os.environ.get("WS_URL", DEFAULT_WS_URL),
            robots=parse_robots(os.environ.get("ROBOTS", DEFAULT_ROBOTS)),
            telemetry_hz=_env_float("TELEMETRY_HZ", 5.0),
            chaos_per_min=_env_float("CHAOS_DISCONNECT_PER_MIN", 0.5),
            fail_rate=_env_float("FAIL_RATE", 0.05),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )
        if urlsplit(cfg.ws_url).scheme not in ("ws", "wss"):
            raise ValueError(f"WS_URL must start with ws:// or wss://, got {cfg.ws_url!r}")
        if not 0 < cfg.telemetry_hz <= 50:
            raise ValueError("TELEMETRY_HZ must be in (0, 50]")
        if cfg.chaos_per_min < 0:
            raise ValueError("CHAOS_DISCONNECT_PER_MIN must be >= 0 (0 disables chaos)")
        if not 0 <= cfg.fail_rate <= 1:
            raise ValueError("FAIL_RATE must be in [0, 1]")
        if cfg.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
        return cfg


def parse_robots(raw: str) -> list[tuple[str, str]]:
    """Parse ROBOTS, e.g. 'robot-1:token-1,robot-2:token-2' -> [('robot-1', 'token-1'), ...]."""
    robots = []
    for item in raw.split(","):
        robot_id, sep, token = item.strip().partition(":")
        if not item.strip():
            continue
        if not sep or not robot_id.strip() or not token.strip():
            raise ValueError(f"bad ROBOTS entry {item.strip()!r}; expected robot_id:token")
        robots.append((robot_id.strip(), token.strip()))
    if not robots:
        raise ValueError("ROBOTS is empty")
    return robots


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def result_message(result: rm.CommandResult) -> dict:
    return {
        "type": "result",
        "command_id": result.command_id,
        "status": "succeeded" if result.succeeded else "failed",
        "detail": result.detail,
    }


# ---------------------------------------------------------------------------
# One simulated robot
# ---------------------------------------------------------------------------
class RobotAgent:
    def __init__(
        self,
        robot_id: str,
        token: str,
        index: int,
        total: int,
        cfg: Config,
        stopping: asyncio.Event,
    ) -> None:
        self.robot_id = robot_id
        self.token = token
        self.cfg = cfg
        self.stopping = stopping
        self.rng = random.Random()
        self.model = rm.RobotModel.spawn(index, total)
        self.seen = rm.LruSet(rm.DEDUPE_CAPACITY)
        # result 是“必须送达”的消息：断线期间产生的结果先存这里，重连收到 hello 后按顺序补发。
        # telemetry 不存——它只有“最新值”有意义，补发旧位置没有价值。
        self.outbox: deque[dict] = deque(maxlen=OUTBOX_LIMIT)
        self.ws: ClientConnection | None = None
        self.replica: str | None = None  # set by `hello`; None = not greeted on this connection
        self._flush_lock = asyncio.Lock()

    def _log(self, level: int, msg: str, **fields: object) -> None:
        log(level, msg, robot_id=self.robot_id, **fields)

    @property
    def url(self) -> str:
        # 只有 robot_id 进 URL；token 走 Authorization 头（见 auth_headers）。
        sep = "&" if "?" in self.cfg.ws_url else "?"
        return f"{self.cfg.ws_url}{sep}{urlencode({'robot_id': self.robot_id})}"

    @property
    def auth_headers(self) -> dict[str, str]:
        # 凭证放请求头而不是 URL：URL 会被写进 Nginx/uvicorn 的日志（Nginx 的 error log 遇到上游出错时
        # 会写出完整的请求行），请求头不会。机器人不是浏览器，可以自由设置握手请求头。
        return {"Authorization": f"Bearer {self.token}"}

    async def run(self) -> None:
        ticker = asyncio.create_task(self._tick_loop(), name=f"tick-{self.robot_id}")
        try:
            await self._connection_loop()
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def close(self) -> None:
        """Graceful shutdown: send a proper close frame so the server sees a clean disconnect."""
        ws = self.ws
        if ws is not None:
            await ws.close(code=1001, reason="simulator shutting down")

    # ---- connection lifecycle ----
    async def _connection_loop(self) -> None:
        attempt = 0
        while not self.stopping.is_set():
            greeted = False
            try:
                # proxy=None：websockets 15+ 默认会读取 HTTP(S)_PROXY 环境变量走代理。
                # 如果 Docker/宿主机配置了代理（国内很常见），连 ws://nginx 也会被发去代理而失败。
                # 机器人只连内网地址，所以显式关闭代理。
                async with connect(
                    self.url,
                    additional_headers=self.auth_headers,
                    proxy=None,
                    open_timeout=10,
                    close_timeout=3,
                ) as ws:
                    greeted = await self._session(ws)
            except (OSError, TimeoutError, WebSocketException) as exc:
                self._log(
                    logging.WARNING,
                    "connect failed",
                    ws_url=self.cfg.ws_url,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                # 兜底：任何意外异常都只让这一次连接失败，不能让整个机器人任务死掉
                self._log(logging.ERROR, "unexpected error in session", exc_info=True)
            if self.stopping.is_set():
                break
            # 只有“握手成功且收到 hello”的健康会话才把退避计数清零；
            # token 错误（accept 后立刻 4401 关闭）会一直退避到 15 s，不会每秒狂连。
            if greeted:
                attempt = 0
            delay = rm.backoff_delay(attempt, self.rng)
            attempt += 1
            self._log(
                logging.INFO, "reconnecting with backoff", delay_s=round(delay, 2), attempt=attempt
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)

    async def _session(self, ws: ClientConnection) -> bool:
        """Serve one connection until it closes. Returns True if the server greeted us."""
        self.ws, self.replica = ws, None
        self._log(logging.INFO, "connected", ws_url=self.cfg.ws_url)
        if self.stopping.is_set():  # SIGTERM arrived while we were still handshaking
            await ws.close(code=1001, reason="simulator shutting down")
        chaos = None
        if self.cfg.chaos_per_min > 0:
            chaos = asyncio.create_task(self._chaos_monkey(ws), name=f"chaos-{self.robot_id}")
        try:
            async for raw in ws:
                await self._on_message(raw)
        except ConnectionClosed:
            pass  # abnormal close (e.g. network error); logged below with its close code
        finally:
            self.ws = None
            if chaos is not None:
                chaos.cancel()
        greeted, replica, self.replica = self.replica is not None, self.replica, None
        self._log_close(ws, replica)
        return greeted

    def _log_close(self, ws: ClientConnection, replica: str | None) -> None:
        code, reason = ws.close_code, ws.close_reason or None
        if code == 4401:
            self._log(
                logging.ERROR,
                "disconnected: token rejected (check ROBOTS)",
                close_code=code,
                close_reason=reason,
            )
        elif code == 4409:
            self._log(
                logging.WARNING,
                "disconnected: replaced by a newer connection",
                close_code=code,
                replica=replica,
            )
        else:
            self._log(
                logging.WARNING,
                "disconnected",
                close_code=code,
                close_reason=reason,
                replica=replica,
            )

    async def _chaos_monkey(self, ws: ClientConnection) -> None:
        # “平均每分钟 N 次、彼此独立”的随机事件 = 泊松过程，
        # 两次事件的间隔服从指数分布（均值 60/N 秒），用 expovariate 抽样即可。
        delay = self.rng.expovariate(self.cfg.chaos_per_min / 60.0)
        await asyncio.sleep(delay)
        self._log(
            logging.WARNING,
            "chaos: closing the connection on purpose",
            after_s=round(delay, 1),
            replica=self.replica,
        )
        await ws.close(code=1001, reason="chaos disconnect")

    # ---- inbound messages ----
    async def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            self._log(logging.WARNING, "ignored non-JSON message", raw=str(raw)[:200])
            return
        if not isinstance(msg, dict):
            self._log(logging.WARNING, "ignored non-object message", raw=str(raw)[:200])
            return
        kind = msg.get("type")
        if kind == "hello":
            await self._on_hello(msg)
        elif kind == "command":
            await self._on_command(msg)
        else:
            self._log(logging.WARNING, "ignored unknown message type", type=kind)

    async def _on_hello(self, msg: dict) -> None:
        self.replica = str(msg.get("replica"))
        server_time = msg.get("server_time")
        skew = round(time.time() - server_time, 3) if isinstance(server_time, int | float) else None
        self._log(
            logging.INFO,
            "hello received",
            replica=self.replica,
            clock_skew_s=skew,
            pending_results=len(self.outbox),
        )
        await self._send_log("info", f"connected via {self.replica}")
        await self._flush_outbox()  # results produced while we were offline

    async def _on_command(self, msg: dict) -> None:
        command_id, command = msg.get("command_id"), msg.get("command")
        params, attempt = msg.get("params") or {}, msg.get("attempt")
        if not isinstance(command_id, str) or not command_id:
            self._log(logging.WARNING, "ignored command without command_id", message=msg)
            return
        is_new = self.seen.add(command_id)
        # 新命令和重复命令都要立刻回 ack：服务端只有收到 ack 才会对 RabbitMQ 消息 basic.ack；
        # 重复投递往往正是因为上一次的 ack 丢了，所以重复时也必须再回一次。
        ack_sent = await self._send({"type": "ack", "command_id": command_id})
        if not is_new:
            self._log(
                logging.INFO,
                "duplicate command re-acked, not executed",
                command_id=command_id,
                command=command,
                attempt=attempt,
                ack_sent=ack_sent,
            )
            return
        ack_note = "ack sent" if ack_sent else "ack NOT sent (socket closed)"
        received = f"command received, {ack_note}"
        self._log(
            logging.INFO,
            received,
            command_id=command_id,
            command=command,
            params=params,
            attempt=attempt,
        )
        # 即使 ack 没发出去也照常执行：命令 id 已记入去重集合，重投时只会补 ack、不会再执行，
        # 执行结果进 outbox，重连后补发——命令不会丢，也不会被执行两次。
        for result in self.model.apply_command(
            command_id, command, params, self.rng, self.cfg.fail_rate
        ):
            await self._send_result(result)

    # ---- outbound messages ----
    async def _send(self, message: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(message))
            return True
        except ConnectionClosed:
            return False

    async def _send_log(self, level: str, msg: str) -> None:
        await self._send({"type": "log", "level": level, "msg": msg[:LOG_MSG_LIMIT]})

    async def _send_result(self, result: rm.CommandResult) -> None:
        self.outbox.append(result_message(result))
        if not result.succeeded:
            await self._send_log("warn", f"command {result.command_id} failed: {result.detail}")
        if self.replica is None:
            self._log(
                logging.INFO,
                "result queued until reconnect",
                command_id=result.command_id,
                status="succeeded" if result.succeeded else "failed",
                detail=result.detail,
            )
        await self._flush_outbox()

    async def _flush_outbox(self) -> None:
        # 只在收到 hello 之后发：hello 说明服务端已校验过 token，这时发出的结果不会被丢弃。
        # 锁保证 tick 循环和收消息循环不会同时 flush、把同一条结果发两遍。
        async with self._flush_lock:
            while self.outbox and self.replica is not None:
                message = self.outbox[0]
                if not await self._send(message):
                    return  # connection dropped mid-flush; keep it for the next connection
                self.outbox.popleft()
                self._log(
                    logging.INFO,
                    "result sent",
                    command_id=message["command_id"],
                    status=message["status"],
                    detail=message["detail"],
                    replica=self.replica,
                )

    # ---- simulation clock ----
    async def _tick_loop(self) -> None:
        loop = asyncio.get_running_loop()
        period = 1.0 / self.cfg.telemetry_hz
        last = loop.time()
        next_heartbeat = last + self._heartbeat_gap()
        while True:
            await asyncio.sleep(period)
            now = loop.time()
            # 用单调时钟测真实间隔；上限 1 s 防止笔记本休眠后一步“瞬移”几十米
            dt, last = min(now - last, MAX_DT_S), now
            try:
                for event in self.model.step(dt, self.rng):
                    await self._on_model_event(event)
                if self.ws is None:
                    continue
                await self._send(
                    {"type": "telemetry", "ts": round(time.time(), 3), **self.model.snapshot()}
                )
                if now >= next_heartbeat:
                    await self._send_log("info", f"heartbeat ok, battery {self.model.battery:.0f}%")
                    next_heartbeat = now + self._heartbeat_gap()
            except Exception:  # a bug in one tick must not stop this robot's clock for good
                self._log(logging.ERROR, "tick failed", exc_info=True)

    def _heartbeat_gap(self) -> float:
        return HEARTBEAT_INTERVAL_S + self.rng.uniform(-1.0, 1.0)  # "~10 s", robots not in lockstep

    async def _on_model_event(self, event: rm.Event) -> None:
        if isinstance(event, rm.CommandResult):
            await self._send_result(event)
        elif isinstance(event, rm.BatteryLow):
            await self._send_log("warn", f"battery low: {event.battery:.0f}%")
        elif isinstance(event, rm.BatteryDepleted):
            await self._send_log("error", "battery depleted, robot halted")
        elif isinstance(event, rm.WanderStarted):
            self._log(logging.DEBUG, "wandering to waypoint", x=event.x, y=event.y)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main() -> int:
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(f"simulator: configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    # 容器里 python 是 PID 1：Linux 内核不会对 PID 1 执行信号的默认动作，
    # 不自己注册 SIGTERM 处理器的话 `docker stop` 会干等 10 s 然后 SIGKILL（连接也不会正常关闭）。
    # (Windows 的事件循环不支持 add_signal_handler，那里靠下面的 KeyboardInterrupt 兜底。)
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    agents = [
        RobotAgent(robot_id, token, index, len(cfg.robots), cfg, stopping)
        for index, (robot_id, token) in enumerate(cfg.robots)
    ]
    log(
        logging.INFO,
        "simulator starting",
        ws_url=cfg.ws_url,
        robots=[a.robot_id for a in agents],
        telemetry_hz=cfg.telemetry_hz,
        chaos_disconnect_per_min=cfg.chaos_per_min,
        fail_rate=cfg.fail_rate,
    )
    tasks = [asyncio.create_task(agent.run(), name=f"robot-{agent.robot_id}") for agent in agents]

    await stopping.wait()
    log(logging.INFO, "shutdown requested, closing connections")
    await asyncio.gather(*(agent.close() for agent in agents), return_exceptions=True)
    _, pending = await asyncio.wait(tasks, timeout=5)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    log(logging.INFO, "simulator stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:  # Ctrl+C where signal handlers are unavailable (Windows)
        sys.exit(130)
