#!/usr/bin/env python3
"""End-to-end smoke test for a running robot-console stack (SPEC §21).

    docker compose up -d --build
    python3 scripts/e2e_check.py                       # default: http://localhost:8080
    python3 scripts/e2e_check.py --base-url http://127.0.0.1:8080

只用 Python 标准库（urllib + json），任何装了 Python 3.9+ 的机器
（包括 macOS 自带的 python3）都能直接跑，不用 pip install。
它走的是和浏览器一样的入口（nginx :8080），所以一次跑通 = nginx 反代 + 两个 API 副本 +
PostgreSQL + Redis + RabbitMQ + 模拟器 整条链路都是好的。
退出码：0 = 全部通过，1 = 有检查失败（可以直接接到 CI 或部署脚本里）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from email.message import Message

TERMINAL = {"succeeded", "failed", "expired"}
WORLD_LIMIT_M = 45.0  # stay inside the |x|,|y| <= 50 bound that move_to accepts


class CheckFailed(Exception):
    pass


def expect(condition: object, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def info(message: str) -> None:
    print(f"       {message}", flush=True)


class Api:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        # ProxyHandler({})：忽略 HTTP(S)_PROXY 环境变量。开发机上常开着 Clash 之类的代理，
        # 不关掉的话，发往 localhost 的请求也可能被送进代理，得到莫名其妙的 502 或超时。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(
        self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Message, object]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status, response_headers, raw = response.status, response.headers, response.read()
        except (
            urllib.error.HTTPError
        ) as exc:  # 4xx / 5xx: urllib raises, but the body is still useful
            status, response_headers, raw = exc.code, exc.headers, exc.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CheckFailed(
                f"{method} {path}: cannot reach {self.base_url} ({reason}). Is the stack up?"
            ) from None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode(errors="replace")[:300]
        return status, response_headers, payload


# ---------------------------------------------------------------------------- checks
def check_login(api: Api, args: argparse.Namespace) -> str:
    status, _, body = api.call(
        "POST", "/api/auth/login", {"username": args.username, "password": args.password}
    )
    expect(
        status == 200 and isinstance(body, dict) and body.get("access_token"),
        f"login: HTTP {status} {body}",
    )
    api.token = body["access_token"]
    return f"logged in as {body.get('username')} (role={body.get('role')})"


def check_robots_online(api: Api, args: argparse.Namespace) -> str:
    deadline = time.monotonic() + args.timeout
    last_report = 0.0
    while True:
        status, _, robots = api.call("GET", "/api/robots")
        expect(
            status == 200 and isinstance(robots, list), f"GET /api/robots: HTTP {status} {robots}"
        )
        online = sorted(r["id"] for r in robots if r.get("online"))
        if len(online) >= args.min_online:
            replicas = {r["id"]: r.get("replica") for r in robots if r.get("online")}
            return f"{len(online)}/{len(robots)} robots online: {replicas}"
        if time.monotonic() > deadline:
            raise CheckFailed(
                f"only {len(online)} robot(s) online after {args.timeout}s, "
                f"need {args.min_online}: {online}"
            )
        if time.monotonic() - last_report > 10:
            info(f"waiting... {len(online)} online so far {online}")
            last_report = time.monotonic()
        time.sleep(2)


def post_command(api: Api, robot_id: str, body: dict, key: str) -> tuple[int, object]:
    status, _, payload = api.call(
        "POST", f"/api/robots/{robot_id}/commands", body, headers={"Idempotency-Key": key}
    )
    return status, payload


def pick_target(robot: dict) -> tuple[float, float]:
    """A point ~3 m from where the robot is now: ~6 s of driving at the default 0.5 m/s."""
    state = robot.get("state") or {}
    x, y = float(state.get("x") or 0.0), float(state.get("y") or 0.0)
    tx = x + 3.0 if x + 3.0 <= WORLD_LIMIT_M else x - 3.0
    ty = max(-WORLD_LIMIT_M, min(WORLD_LIMIT_M, y))
    return round(tx, 2), round(ty, 2)


def print_timeline(command: dict) -> None:
    info(f"events of command {command['id']} (attempts={command.get('attempts')}):")
    start = None
    for event in command.get("events", []):
        offset = ""
        try:
            at = datetime.fromisoformat(str(event["at"]).replace("Z", "+00:00"))
            start = start or at
            offset = f"+{(at - start).total_seconds():6.3f}s"
        except ValueError:
            pass
        transition = f"{event.get('from_status') or '-'} -> {event.get('to_status')}"
        detail = f"  ({event['detail']})" if event.get("detail") else ""
        info(f"  {offset:>9}  {transition:<24} replica={event.get('replica') or '-':<6}{detail}")


def check_move_to(api: Api, args: argparse.Namespace) -> str:
    status, _, robot = api.call("GET", f"/api/robots/{args.robot}")
    expect(
        status == 200 and isinstance(robot, dict),
        f"GET /api/robots/{args.robot}: HTTP {status} {robot}",
    )
    x, y = pick_target(robot)
    status, command = post_command(
        api, args.robot, {"type": "move_to", "params": {"x": x, "y": y}}, str(uuid.uuid4())
    )
    expect(
        status == 202 and isinstance(command, dict),
        f"POST move_to: expected 202, got {status} {command}",
    )
    info(f"move_to(x={x}, y={y}) accepted as command {command['id']}, polling until it finishes...")

    deadline = time.monotonic() + args.timeout
    while True:
        status, _, command = api.call("GET", f"/api/commands/{command['id']}")
        expect(
            status == 200 and isinstance(command, dict),
            f"GET /api/commands/<id>: HTTP {status} {command}",
        )
        if command["status"] in TERMINAL:
            break
        if time.monotonic() > deadline:
            print_timeline(command)
            raise CheckFailed(f"command still '{command['status']}' after {args.timeout}s")
        time.sleep(1)

    print_timeline(command)
    if command["status"] == "succeeded":
        return f"move_to succeeded after {command.get('attempts')} dispatch attempt(s)"
    details = [command.get("error") or ""] + [
        e.get("detail") or "" for e in command.get("events", [])
    ]
    if command["status"] == "failed" and any("obstacle" in d for d in details):
        # 模拟器按 FAIL_RATE（默认 5%）随机“撞障碍”——这也是一条走完全程的合法结果
        return (
            "move_to ended 'failed: obstacle detected' (simulated via FAIL_RATE) - pipeline works"
        )
    raise CheckFailed(f"move_to ended as '{command['status']}' (error={command.get('error')})")


def check_idempotency(api: Api, args: argparse.Namespace) -> str:
    key = f"e2e-{uuid.uuid4()}"
    body = {"type": "set_speed", "params": {"speed": 0.5}}
    first_status, first = post_command(api, args.robot, body, key)
    second_status, second = post_command(api, args.robot, body, key)
    expect(
        first_status == 202 and isinstance(first, dict),
        f"first POST: expected 202, got {first_status} {first}",
    )
    expect(
        second_status == 200 and isinstance(second, dict),
        f"second POST with the same Idempotency-Key: expected 200, got {second_status} {second}",
    )
    expect(
        first["id"] == second["id"],
        f"same Idempotency-Key produced two commands: {first['id']} != {second['id']}",
    )
    return (
        f"same Idempotency-Key twice -> HTTP {first_status} then {second_status}, "
        f"one command {first['id']}"
    )


def check_load_balancing(api: Api, args: argparse.Namespace) -> str:
    replicas = []
    for i in range(1, 7):
        status, headers, body = api.call("GET", "/api/health")
        # X-Upstream is added by nginx (which upstream server answered),
        # X-Replica by the API itself (its REPLICA_ID).
        upstream = headers.get("X-Upstream") or "-"
        replica = headers.get("X-Replica") or "-"
        info(f"#{i}  HTTP {status}  X-Upstream={upstream:<20} X-Replica={replica}")
        expect(status == 200, f"/api/health returned HTTP {status}: {body}")
        replicas.append(replica)
    distinct = sorted(set(replicas) - {"-"})
    if len(distinct) < 2:
        who = distinct or "no replica header"
        info(f"WARNING: only {who} answered; is the other API replica down?")
    return f"/api/health 6x -> 200, answered by {distinct}"


CHECKS = [
    # (name, function, names of checks it depends on)
    ("login as admin", check_login, []),
    ("robots online", check_robots_online, ["login as admin"]),
    ("move_to round trip", check_move_to, ["login as admin", "robots online"]),
    ("idempotency key", check_idempotency, ["login as admin"]),
    ("load balancing", check_load_balancing, []),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url", default=os.environ.get("E2E_BASE_URL", "http://localhost:8080")
    )
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--robot", default="robot-1", help="robot that receives the test commands")
    parser.add_argument("--min-online", type=int, default=3)
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="seconds to wait for robots / for the command"
    )
    args = parser.parse_args()

    api = Api(args.base_url)
    print(f"robot-console e2e check against {args.base_url}\n", flush=True)
    results: dict[str, str] = {}
    for name, check, needs in CHECKS:
        print(f"== {name}", flush=True)
        blocked = [n for n in needs if results.get(n) != "PASS"]
        if blocked:
            results[name] = "SKIP"
            print(f"[SKIP] needs: {', '.join(blocked)}\n", flush=True)
            continue
        try:
            message = check(api, args)
            results[name] = "PASS"
            print(f"[PASS] {message}\n", flush=True)
        except CheckFailed as exc:
            results[name] = "FAIL"
            print(f"[FAIL] {exc}\n", flush=True)

    failed = [n for n, r in results.items() if r != "PASS"]
    if failed:
        passed = len(results) - len(failed)
        print(f"E2E RESULT: FAIL ({passed}/{len(results)} passed; not passed: {', '.join(failed)})")
        return 1
    print(f"E2E RESULT: PASS ({len(results)}/{len(results)} checks)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
