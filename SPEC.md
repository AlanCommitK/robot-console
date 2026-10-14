# robot-console — Design Spec

This document is the contract for the interfaces between the components of robot-console: runtime
topology, configuration, database schema, the command state machine, REST and WebSocket protocols, Redis
keys, RabbitMQ topology, metrics, Docker and CI. Code comments refer to its sections as "SPEC §N".

## 1. Purpose and scope

robot-console is a miniature "robot fleet cloud platform".
Simulated robots stream telemetry over WebSocket; an operator watches a live dashboard and sends
commands; commands travel through RabbitMQ with at-least-once delivery, a server-side state machine,
idempotency keys and duplicate-ACK dedupe (the same class of problems as any order pipeline: retries,
duplicates and out-of-order events).

Goals:
- Commands are never silently lost: every command reaches a terminal status (succeeded, failed or
  expired), and every status change is recorded as an event.
- Duplicate, late and out-of-order events are harmless (idempotent, out-of-order safe handling).
- Two stateless API replicas behind one entry point; any replica can serve any dashboard.
- A dependency outage degrades the system in a bounded, observable way (metrics, alerts, health checks).

Technologies in scope:
FastAPI (REST + WebSocket), middleware, JWT auth + role-based access, SQLAlchemy 2.0 async + PostgreSQL
+ Alembic migrations, Redis (cache + pub/sub), RabbitMQ (durable per-robot queues, manual ack, prefetch,
message TTL, dead-letter exchange), React + TypeScript + ECharts, Nginx (SPA hosting, reverse proxy,
WebSocket upgrade, load balancing across two API replicas), Docker / Docker Compose, Prometheus + Grafana
(+ alert rules), GitHub Actions CI, pytest.

Non-goals: real robot hardware or vendor protocols (robots are simulated); high availability of
PostgreSQL / Redis / RabbitMQ (single instances); TLS termination and secret management (development
defaults only); multi-tenancy, per-robot authorization and user management; exactly-once delivery
(at-least-once plus idempotent handling is the contract); long-term telemetry retention.
Known limitations are listed in docs/LIMITATIONS.md.

Engineering conventions:
- Prefer standard, boring, widely documented approaches over clever ones. Small files, clear names.
- Comment the "why" in Chinese at mechanism level (e.g. why manual ack, why FOR UPDATE, why Upgrade
  headers). No line-by-line noise comments. Identifiers, logs and API fields stay in English.
- Pin dependency versions. Python 3.12 in containers (code must also run on 3.13 locally).
- Images must be multi-arch official images (the stack runs on x86-64 and arm64 hosts, e.g. Apple Silicon).

## 2. Repository layout

```
robot-console/
  SPEC.md                    (this file)
  README.md                  (Chinese, primary)
  README.en.md               (English)
  LICENSE                    (MIT)
  docs/
    ARCHITECTURE.md, FAILURE_DRILLS.md, LIMITATIONS.md (Chinese)
    architecture.svg, state-machine.svg
    dashboard.png, grafana.png, login.png
  docker-compose.yml
  .env.example
  .gitignore
  .github/workflows/ci.yml
  scripts/e2e_check.py
  ops/
    rabbitmq/enabled_plugins
    prometheus/prometheus.yml
    prometheus/alerts.yml
    grafana/provisioning/datasources/datasource.yml
    grafana/provisioning/dashboards/dashboards.yml
    grafana/dashboards/robot-console.json
  backend/
    Dockerfile, requirements.txt, requirements-dev.txt, pyproject.toml (ruff + pytest config)
    alembic.ini, alembic/env.py, alembic/versions/0001_initial.py
    app/__init__.py, main.py, config.py, db.py, models.py, schemas.py, security.py, deps.py,
        middleware.py, metrics.py, redis_bus.py, mq.py, state_machine.py, commands_service.py,
        ws_hub.py, telemetry_writer.py, seed.py,
        sweeper.py (stuck-command sweeper, §7), robot_state_writer.py (per-connection Redis writer, §9),
        log_throttle.py (rate-limited warning logs),
        routers/__init__.py, auth.py, robots.py, commands.py, ws.py, health.py
    tests/ (conftest.py, test_state_machine.py, test_security.py, test_schemas.py, test_transitions.py,
            test_dispatcher.py, test_robot_state_writer.py, test_redis_outbox.py, test_log_throttle.py,
            test_api_integration.py [marker: integration])
  frontend/
    Dockerfile, nginx.conf, package.json, package-lock.json, tsconfig.json, vite.config.ts,
    index.html, src/...
  simulator/
    Dockerfile, requirements.txt, sim.py, robot_model.py, tests/test_robot_model.py, tests/test_sim_config.py
```

## 3. Runtime topology (docker compose, project name "robot-console")

| service    | image / build                         | internal port | host port     | notes |
|------------|---------------------------------------|---------------|---------------|-------|
| postgres   | postgres:16-alpine                    | 5432          | 127.0.0.1:5432 | db/user/password = robot/robot/robot, named volume pgdata |
| redis      | redis:7-alpine                        | 6379          | 127.0.0.1:6379 | |
| rabbitmq   | rabbitmq:3.13-management-alpine       | 5672, 15672, 15692 | 127.0.0.1:15672 (mgmt UI) | mount ops/rabbitmq/enabled_plugins (management + prometheus) |
| migrate    | build ./backend                       | -             | -             | one-shot: `alembic upgrade head && python -m app.seed` |
| api-1      | build ./backend                       | 8000          | -             | REPLICA_ID=api-1 |
| api-2      | build ./backend                       | 8000          | -             | REPLICA_ID=api-2 |
| nginx      | build ./frontend                      | 80            | 8080          | SPA + reverse proxy + LB |
| simulator  | build ./simulator                     | -             | -             | connects to ws://nginx/ws/robot |
| prometheus | prom/prometheus:v2.54.1               | 9090          | 127.0.0.1:9090 | |
| grafana    | grafana/grafana:11.2.2                | 3000          | 127.0.0.1:3000 | anonymous Viewer + admin/admin |

Only nginx (the product entry point) is published on all interfaces. Every other host port is bound to
127.0.0.1: the databases and the ops UIs use public default credentials (guest/guest, admin/admin) or no
authentication at all (Prometheus, incl. its lifecycle API).

Start order (depends_on): migrate waits for postgres healthy; api-1/api-2 wait for postgres, redis and
rabbitmq healthy plus migrate completed successfully; nginx waits for api-1/api-2 healthy; simulator waits
for nginx started. Prometheus/grafana independent.

## 4. Configuration (backend reads env via pydantic-settings; defaults in brackets)

- DATABASE_URL [postgresql+asyncpg://robot:robot@postgres:5432/robot]
- REDIS_URL [redis://redis:6379/0]
- RABBITMQ_URL [amqp://guest:guest@rabbitmq:5672/]
- JWT_SECRET [dev-secret-change-me-at-least-32-bytes-long]   JWT_EXPIRE_MINUTES [480]
- REPLICA_ID [socket.gethostname()]
- COMMAND_TTL_MS [30000]   ACK_TIMEOUT_S [10]   RESULT_TIMEOUT_S [300]   ROBOT_ONLINE_TTL_S [5]
  (RESULT_TIMEOUT_S: how long an acked command may wait for its result before the sweeper fails it, §7;
  must exceed the longest legitimate command execution time)
- TELEMETRY_FLUSH_INTERVAL_S [1.0]   LOG_LEVEL [INFO]
- CORS_ORIGINS [http://localhost:5173]  (comma separated; only for vite dev server)

Alembic reads DATABASE_URL from the environment (through app.config.Settings, the same source as the
application) and runs migrations with the async engine (asyncpg) via `connection.run_sync`, so no
synchronous driver is needed.

## 5. Seed data (app/seed.py, idempotent, run as `python -m app.seed`)

- users: admin / admin123 (role admin), viewer / viewer123 (role viewer)
- robots: robot-1 .. robot-4, names AGV-01 .. AGV-04, plaintext tokens dev-token-robot-1 .. dev-token-robot-4
  (store sha256 hex digest in robots.token_hash; never store plaintext)
- passwords hashed with bcrypt (use the `bcrypt` package directly; avoid passlib)

## 6. Database schema (PostgreSQL 16, Alembic revision 0001_initial)

```
users(id serial pk, username varchar(64) unique not null, password_hash text not null,
      role varchar(16) not null check (role in ('admin','viewer')), created_at timestamptz not null default now())
robots(id varchar(64) pk, name varchar(128) not null, token_hash char(64) not null,
       created_at timestamptz not null default now())
commands(id uuid pk, robot_id varchar(64) not null references robots(id),
         type varchar(32) not null check (type in ('move_to','stop','set_speed')),
         params jsonb not null default '{}', status varchar(16) not null default 'queued',
         idempotency_key varchar(128) null, attempts int not null default 0, error text null,
         created_by varchar(64) not null, created_at timestamptz not null default now(),
         updated_at timestamptz not null default now(), dispatched_at timestamptz null,
         acked_at timestamptz null, finished_at timestamptz null,
         unique (robot_id, idempotency_key))          -- NULLs are distinct in PG, so keyless commands never collide
index commands(robot_id, created_at desc)
command_events(id bigserial pk, command_id uuid not null references commands(id),
               from_status varchar(16) null, to_status varchar(16) not null, detail text null,
               replica varchar(64) null, at timestamptz not null default now())
telemetry(id bigserial pk, robot_id varchar(64) not null references robots(id), ts timestamptz not null,
          x double precision not null, y double precision not null, theta double precision not null,
          speed double precision not null, battery double precision not null, mode varchar(16) not null)
index telemetry(robot_id, ts desc)
```

## 7. Command state machine (app/state_machine.py — pure function, exhaustively unit-tested)

Statuses: `queued, dispatched, acked, succeeded, failed, expired`. Terminal: `succeeded, failed, expired`.
Events: `dispatch, ack, result_succeeded, result_failed, expire, ack_timeout, result_timeout`.

```
queued      --dispatch-->          dispatched
dispatched  --dispatch-->          dispatched   (redelivery after reconnect; caller increments attempts)
dispatched  --ack-->               acked
dispatched  --result_succeeded-->  succeeded    (result without/before ack implies ack)
dispatched  --result_failed-->     failed
acked       --result_succeeded-->  succeeded
acked       --result_failed-->     failed
queued      --result_failed-->     failed       (publish to RabbitMQ failed: detail/error 'publish_failed', §8)
queued      --expire-->            expired      (DLX notification, or the sweeper)
dispatched  --expire-->            expired      (DLX notification, or the sweeper)
dispatched  --ack_timeout-->       failed       (error = 'ack_timeout')
acked       --result_timeout-->    failed       (sweeper only; error = 'result_timeout')
everything else (incl. any event on a terminal status, duplicate ack on acked) -> None = ignore
```
`def next_status(current: str, event: str) -> str | None`. The unit test asserts all 6 x 7 = 42 pairs.

Applying an event (app/commands_service.py, `apply_event(session, command_id, event, detail=None, replica=...)`):
in ONE transaction `SELECT ... FOR UPDATE` the command row, compute next_status; if None -> return
(idempotent, out-of-order safe); else update status + the matching timestamp (dispatched_at on first
dispatch, acked_at, finished_at for terminal) + attempts (+1 on every dispatch) + error, insert a
command_events row, commit; after the commit increment `rc_command_transitions_total{to_status}` and put a
`command_update` event into the replica's Redis outbox (never awaited, see §11). Ack latency histogram observed
on dispatched->acked (acked_at - dispatched_at of the latest dispatch; keeping the last dispatch time in memory is
fine). The per-row logic lives in `transition_locked()` (caller holds the row lock, no commit) +
`finish_transition()` (post-commit side effects); `apply_event` and the sweeper both use it.

Sweeper (app/sweeper.py): every replica runs it at startup and then every 30 s. It claims up to 100 rows per
transaction with `SELECT ... FOR UPDATE SKIP LOCKED` (rows locked by another replica are skipped, never waited on,
so two replicas never process the same row), repeating while a batch is full, and applies events through
`transition_locked`:
- `queued`/`dispatched` with `updated_at` older than COMMAND_TTL_MS + 60 s -> `expire`,
  detail `swept: no update for over <N>s while <status>` (error `expired`). Covers commands whose message never
  reached RabbitMQ (process killed between commit and publish) or whose ack/result could not be applied.
- `acked` with `acked_at` older than RESULT_TIMEOUT_S -> `result_timeout` -> failed,
  detail `swept: no result within <N>s after ack`.

## 8. REST API (prefix /api, JSON, errors as {"detail": "..."})

Auth header: `Authorization: Bearer <JWT>`; JWT HS256 claims `{sub: username, role, exp}`.
401 = missing/invalid/expired token; 403 = wrong role.

- `POST /api/auth/login` body `{username, password}` -> 200 `{access_token, token_type: "bearer", username, role, expires_at}` | 401
- `GET /api/me` [auth] -> `{username, role}`
- `POST /api/ws-ticket` [auth] -> 200 `{ticket, expires_in: 30}` | 401 | 503 (Redis down).
  One-time ticket for the dashboard WebSocket (§10): `secrets.token_urlsafe(32)` stored as Redis key
  `ws-ticket:{ticket}` = JSON `{sub, role, exp}` (exp = the JWT's exp) with `EX 30`.
- OpenAPI docs: `/api/docs` (Swagger UI), `/api/redoc`, `/api/openapi.json` (under /api so they are reachable
  through Nginx; `/docs` is the SPA fallback)
- `GET /api/robots` [auth] -> `RobotOut[]`
  - `RobotOut = {id, name, online: bool, replica: str|null, last_seen: iso8601|null, state: TelemetryOut|null}`
  - online = Redis key `robot:{id}:online` exists; state/last_seen from hash `robot:{id}:state`
- `GET /api/robots/{id}` [auth] -> `RobotOut` | 404
- `GET /api/robots/{id}/telemetry?seconds=120` (1..3600) [auth] -> `TelemetryOut[]` ascending by ts (from PostgreSQL)
  - `TelemetryOut = {ts: iso8601, x, y, theta, speed, battery, mode}`
- `GET /api/robots/{id}/logs?limit=100` (1..200) [auth] -> `LogOut[]` newest first; `LogOut = {ts: iso8601, level, msg}`
- `POST /api/robots/{id}/commands` [auth, role admin], optional header `Idempotency-Key`
  - body `CommandIn = {type: "move_to"|"stop"|"set_speed", params: object}`
    - move_to: `{x: float, y: float}` with |x|,|y| <= 50; set_speed: `{speed: float}` 0..2.0; stop: `{}`
  - 202 `CommandOut` when newly created and published; 200 `CommandOut` when a command with the same
    (robot_id, Idempotency-Key) already exists (race-safe: rely on the unique constraint, catch IntegrityError)
  - 404 unknown robot, 403 viewer, 422 invalid params
  - Order: insert row (status queued) + event row -> commit -> put command_update(queued) into the Redis outbox
    -> publish to RabbitMQ with publisher confirms (5 s budget). If publishing to RabbitMQ fails, apply event
    `result_failed` with detail 'publish_failed' and return 503. Neither path waits for Redis.
- `GET /api/commands?robot_id=&limit=50` (1..200) [auth] -> `CommandOut[]` newest first
- `GET /api/commands/{id}` [auth] -> `CommandOut + {events: [{from_status, to_status, detail, replica, at}]}`
  - `CommandOut = {id, robot_id, type, params, status, attempts, error, created_by, created_at, updated_at, dispatched_at, acked_at, finished_at}` (datetimes iso8601)
- `GET /api/health` (no auth) -> 200 `{status: "ok", replica, checks: {db: "ok", redis: "ok", rabbitmq: "ok"}}`;
  if any check fails -> 503 with status "degraded" and the failing check's error message
- `GET /metrics` (no auth, not under /api, Prometheus text format)

Middleware (app/middleware.py), applied in this order (outermost first):
1. RequestIdMiddleware: read `X-Request-ID` or generate uuid4; store in a contextvar; echo in response.
2. AccessLogMiddleware: one JSON log line per HTTP request {method, path, status, ms, request_id, replica}.
3. Metrics: observe `rc_http_request_duration_seconds{method, route, status}` using the route template
   (e.g. `/api/robots/{robot_id}`), not the raw path (avoid label cardinality explosion).
Also add response header `X-Replica: <REPLICA_ID>` on every HTTP response.
Use pure ASGI middleware or Starlette BaseHTTPMiddleware; skip WebSocket scopes in HTTP-only middleware.
CORSMiddleware with CORS_ORIGINS (dev only).

## 9. WebSocket — robot <-> server: `GET /ws/robot?robot_id=...` + header `Authorization: Bearer <robot token>`

- Auth: the token travels ONLY in the handshake's `Authorization: Bearer` header (URLs end up in Nginx/uvicorn
  logs, request headers do not). sha256(token) must equal robots.token_hash, else accept then close with code 4401
  reason "unauthorized". A `token` query parameter is ignored (connection rejected, warning logs `token_in_query`).
- One connection per robot per replica: a new connection for the same robot on the same replica closes the
  old one with code 4409 "replaced".
- Across replicas: before dispatching a command the consumer reads `robot:{id}:online` (0.5 s budget). If it names
  another replica, this connection is stale (typically half-open): the consumer stops without applying `dispatch`
  (attempts unchanged, not a failure), its channel closes so the message is requeued for the live consumer, and the
  socket is closed with 4409 "replaced by a newer connection". Redis errors/timeouts fail open (dispatch anyway).
- Connection setup order: accept -> auth -> hello -> write the online key + `robot_status` (2 s budget; retried by
  the state writer on failure) -> start the dispatcher -> receive loop. The online key must be written before the
  dispatcher starts, otherwise the ownership check could see the previous replica and reject the live connection.
- Server -> robot:
  - `{"type":"hello","replica":"api-1","server_time":<epoch float>}` right after accept
  - `{"type":"command","command_id":"<uuid>","command":"move_to|stop|set_speed","params":{...},"attempt":<int>}`
- Robot -> server:
  - `{"type":"telemetry","ts":<epoch float>,"x":..,"y":..,"theta":..,"speed":..,"battery":..,"mode":"idle|moving|charging|error"}`
  - `{"type":"ack","command_id":"<uuid>"}`
  - `{"type":"result","command_id":"<uuid>","status":"succeeded|failed","detail":<str|null>}`
  - `{"type":"log","level":"info|warn|error","msg":"<=500 chars"}`
- Invalid JSON or schema: ignore the message, log a warning, increment `rc_ws_invalid_messages_total{kind="robot"}`; never crash the connection.
- The receive loop never awaits Redis (a slow/down Redis must not delay ack/result handling and cause
  `ack_timeout`). Telemetry and logs are handed to a per-connection `RobotStateWriter` (app/robot_state_writer.py,
  in-memory, returns immediately) whose background task does the Redis writes; failures are logged at most once
  per 10 s per process (`suppressed` = count) and retried after 1 s.
- On telemetry: enqueue for DB persistence; increment `rc_telemetry_messages_total`; keep it as the connection's
  latest sample (a newer sample overwrites an unwritten older one). The writer task writes the latest sample:
  HSET `robot:{id}:state` (all fields + replica + last_seen = receive time epoch); SET `robot:{id}:online <replica>
  EX ROBOT_ONLINE_TTL_S`; PUBLISH `rc:events` a `telemetry` event.
- On connect/disconnect: publish `robot_status` (online true/false). On disconnect (2 s budget) also DEL
  `robot:{id}:online` only if its value equals this replica (avoid deleting the key a newer connection on another
  replica owns); if Redis is down the key simply expires (TTL lease).
- On log: queued in the writer (bounded to 100, oldest dropped, removed only after a successful write), which does
  LPUSH `robot:{id}:logs` JSON LogOut, LTRIM 0 199, PUBLISH a `log` event.
- On ack/result: resolve the in-memory ack future, apply the event in PostgreSQL; the `command_update` notification
  goes through the outbox (§11), so ack/result handling never waits for Redis.

## 10. WebSocket — dashboard: `GET /ws/dashboard?ticket=<one-time ticket>`

- Browsers cannot set headers on a WebSocket handshake, and a JWT in the URL would end up in logs, so the
  dashboard first calls `POST /api/ws-ticket` with its JWT and connects with the returned ticket.
- On connect the server redeems the ticket atomically with Redis `GETDEL ws-ticket:{ticket}` (single use).
  Missing/unknown/expired/already-used ticket, or a JWT exp already in the past -> accept then close 4401.
  Redis unavailable -> close 1011 (transient; the client backs off and retries). A JWT in a `token` query
  parameter is not accepted.
- The socket never outlives the JWT that minted the ticket: when the remembered `exp` passes, the server closes
  it with 4401 "token expired".
- A ticket in a log line is harmless: it is either already consumed or expires within 30 s, and it only grants a
  read-only event stream (not API access for hours like a JWT).
- Server pushes JSON events (exactly the objects published on Redis channel `rc:events`):
  - `{"type":"telemetry","robot_id","replica","ts","x","y","theta","speed","battery","mode"}` (ts = epoch float)
  - `{"type":"robot_status","robot_id","online":bool,"replica"}`
  - `{"type":"command_update","command":CommandOut}`
  - `{"type":"log","robot_id","ts","level","msg"}` (ts = iso8601)
  - `{"type":"pong"}` in reply to `{"type":"ping"}`
- Browser sends `{"type":"ping"}` every 20 s (browsers cannot send WS ping frames).
- Each API replica runs ONE Redis subscriber task on `rc:events` and fans out to its local dashboard
  connections. Each dashboard connection has an `asyncio.Queue(maxsize=1000)` drained by a sender task;
  when full, drop the event and increment `rc_ws_dropped_total` (slow clients must not block others).

## 11. Redis keys and channels

- `robot:{id}:state` HASH (latest telemetry fields + replica + last_seen)
- `robot:{id}:online` STRING value = replica, EX ROBOT_ONLINE_TTL_S (also read by the dispatch ownership check, §9)
- `robot:{id}:logs` LIST of JSON LogOut (LPUSH + LTRIM 0 199)
- `ws-ticket:{ticket}` STRING JSON `{sub, role, exp}`, EX 30, consumed with GETDEL (§10)
- channel `rc:events`
- Outbox: `command_update` events are put into a per-replica bounded queue (1000; when full the event is dropped
  with a rate-limited warning) and PUBLISHed in order by one background task (`run_outbox`); a failed publish drops
  that event and pauses 1 s. Callers never await Redis for these notifications.
- The `rc:events` subscriber reconnects with exponential backoff 1 s -> 5 s (rate-limited warning), so dashboards
  receive events again within ~5 s after Redis recovers.

## 12. RabbitMQ (aio-pika)

- Per-robot durable queue `cmd.robot.{robot_id}` with arguments exactly
  `{"x-message-ttl": COMMAND_TTL_MS, "x-dead-letter-exchange": "cmd.dlx"}` (identical everywhere it is declared,
  otherwise RabbitMQ raises PRECONDITION_FAILED).
- Publish via the default exchange, routing_key = queue name, `delivery_mode=PERSISTENT`, `message_id=command_id`,
  `content_type="application/json"`, body `{"command_id","command","params"}`. Publisher confirms enabled.
  Declare the robot queue before publishing (idempotent).
- Dead-letter exchange `cmd.dlx` (fanout, durable) bound to durable queue `cmd.expired`. Every API replica
  consumes `cmd.expired` (competing consumers, prefetch 10) and applies event `expire` using message_id.
- Channels: the connection is `connect_robust` (auto-reconnect), but every channel is a plain, non-robust
  `aio_pika.Channel` that aio-pika never restores. Robust channels are re-opened after a reconnect together with
  every consumer ever registered on them, including consumers our code already abandoned (e.g. a robot that left
  while the broker was down): such a ghost consumer takes a message (prefetch) and never acks it, so the command is
  neither dispatched nor expired. Instead each consumer loop (robot queue and `cmd.expired`) opens its own channel,
  re-declares its topology, consumes until the channel closes, then reopens it: it waits for the connection to be
  ready, pauses 1 s, and backs off 1 s -> 15 s after exceptions.
- Publish channel: one plain channel with publisher confirms, opened during startup (lifespan) inside an empty
  contextvars context (aio-pika's internal tasks/callbacks must not inherit an HTTP request's request_id), reopened
  lazily when closed and discarded (closed) after a publish error.
- Dispatch: when a robot connects to a replica, open a dedicated channel with `prefetch_count=1`, declare the
  robot's queue, consume with manual ack. On message: ownership check (§9; if superseded stop the consumer, no
  ack/nack, message requeued); apply `dispatch`; if the result is None (already
  terminal, e.g. expired/failed) just ack and skip; else send the command (with `attempt`) to the robot and await
  an ack future (registry keyed by command_id) up to ACK_TIMEOUT_S. On ack -> `message.ack()`. On timeout ->
  apply `ack_timeout` and `message.ack()`. On robot disconnect: cancel the consumer and close the channel ->
  RabbitMQ requeues the unacked message (it keeps its original TTL deadline) -> redelivered on reconnect.
- Robot `ack` message: resolve the future if present on this replica AND apply `ack` (idempotent).
  Robot `result` message: apply `result_succeeded` / `result_failed` with detail.

## 13. Prometheus metrics (prometheus_client, exposed at /metrics)

```
rc_http_request_duration_seconds   Histogram  labels: method, route, status
rc_ws_connections                  Gauge      labels: kind (robot|dashboard)
rc_telemetry_messages_total        Counter
rc_ws_invalid_messages_total       Counter    labels: kind
rc_ws_dropped_total                Counter
rc_command_transitions_total       Counter    labels: to_status
rc_command_ack_latency_seconds     Histogram  buckets 0.005..10
rc_telemetry_rows_written_total    Counter
```
No replica label: Prometheus adds `instance` per scrape target.

## 14. Telemetry persistence (app/telemetry_writer.py)

In-memory buffer per replica; keep at most one sample per robot per 0.5 s (drop extras); flush every
TELEMETRY_FLUSH_INTERVAL_S with a single multi-row INSERT; flush on shutdown; increment
`rc_telemetry_rows_written_total`. DB failures are logged and the batch is dropped (do not crash).

## 15. Frontend (React 18 + TypeScript + Vite 6 + ECharts 6) — Chinese UI labels

- Login page -> stores JWT (memory + localStorage wrapped in try/catch).
- Dashboard:
  - top bar: user, role, live-connection indicator (connected / reconnecting), logout
  - robot table: name, online badge, replica, battery %, speed, mode, last seen; click selects a robot
  - selected robot panel:
    - live line charts over the last 120 s: battery (%) and speed (m/s) — backfill from
      `GET /api/robots/{id}/telemetry?seconds=120`, then append WS telemetry; cap points
    - x-y trajectory chart (last 120 s)
    - command panel (admin only; hidden or disabled for viewer): move_to (x, y), set_speed, stop.
      Each click generates a new Idempotency-Key with crypto.randomUUID()
    - command list for this robot with live status badges (queued/dispatched/acked/succeeded/failed/expired),
      updated from `command_update` events; click shows events timeline (GET /api/commands/{id})
    - recent logs list (backfill GET logs, then WS `log` events)
- API client: fetch with Bearer token; on 401 -> logout.
- WS client (src/ws.ts): before every connect/reconnect fetch a fresh ticket with `POST /api/ws-ticket`, then open
  `/ws/dashboard?ticket=...` (same origin; build the ws:// or wss:// URL from location). Ticket call 401 -> logout;
  any other failure (network, 502, 503) or any close (including 4401) -> auto-reconnect with exponential backoff
  1 s -> 30 s with jitter (the next ticket call decides whether the JWT is still valid). Ping every 20 s.
- Dev: `npm run dev` with vite proxy: `/api` and `/ws` (ws: true) -> http://localhost:8080.
- Styling: plain CSS with CSS variables, clean and readable. No UI kit.
- `npm run build` must run `tsc --noEmit` (or `tsc -b`) and `vite build` with zero errors.

## 16. Nginx (frontend/nginx.conf, served by nginx:1.27-alpine)

- `/` serves the SPA (`try_files $uri /index.html`).
- `upstream api_upstream { zone api_upstream 64k; resolver 127.0.0.11 valid=5s ipv6=off;
  server api-1:8000 max_fails=2 fail_timeout=10s resolve; server api-2:8000 max_fails=2 fail_timeout=10s resolve; }`
  (round robin; replicas are stateless because shared state lives in PostgreSQL/Redis/RabbitMQ). `zone` +
  `resolver` (Docker's embedded DNS) + `resolve` make Nginx re-resolve the replica hostnames every 5 s, so a
  re-created API container with a new IP is followed without restarting Nginx.
- `location /api/` -> `proxy_pass http://api_upstream;` with `X-Real-IP`, `X-Forwarded-For`,
  `X-Forwarded-Proto`, pass `X-Request-ID`; `proxy_next_upstream error timeout` (not 502/503: they count toward max_fails and would eject both replicas when a dependency is down).
- `location /ws/` -> same upstream, `proxy_http_version 1.1`, `Upgrade $http_upgrade`,
  `Connection $connection_upgrade` (map block), `proxy_read_timeout 3600s`, `proxy_send_timeout 3600s`.
- `add_header X-Upstream $upstream_addr always;` on proxied locations (shows which replica answered).
- `/metrics` is NOT proxied. gzip for static assets; long cache for hashed assets, no-cache for index.html.
- Logging: the `/ws/` access log format records `$uri` only, but the error log always contains the full request
  line and upstream URL (query string included) when an upstream fails. Hence no long-lived credential is ever put
  in a URL (robots: Authorization header; dashboards: single-use 30 s ticket).

## 17. Simulator (simulator/, Python 3.12 asyncio + `websockets`)

Env: `WS_URL` [ws://nginx/ws/robot], `ROBOTS` [robot-1:dev-token-robot-1,robot-2:dev-token-robot-2,robot-3:dev-token-robot-3,robot-4:dev-token-robot-4],
`TELEMETRY_HZ` [5], `CHAOS_DISCONNECT_PER_MIN` [0.5] (expected random disconnects per robot per minute; 0 disables),
`FAIL_RATE` [0.05], `LOG_LEVEL` [INFO].

Per robot: connect to `WS_URL?robot_id=<id>` with header `Authorization: Bearer <token>` (websockets
`additional_headers`; never the token in the URL) with exponential backoff + jitter (1 s -> 15 s); log the replica from `hello`; send
telemetry at TELEMETRY_HZ; motion model in robot_model.py (pure, unit-tested): move toward target at current
speed (default 0.5 m/s), theta follows heading, battery drains 0.05 %/s while moving, recharges 0.1 %/s when
idle, clamp 0..100, mode idle/moving/charging/error (error if battery hits 0); start positions spread out;
wander to a random waypoint when idle for > 20 s so charts move.
Commands: keep a bounded LRU set (size 1000) of processed command_ids. New command -> send `ack` immediately,
then execute: move_to sets target, sends `result succeeded` when within 0.2 m (or `failed` "obstacle detected"
with probability FAIL_RATE, decided at start); stop -> speed 0, result succeeded immediately; set_speed -> result
succeeded immediately. Duplicate command_id (redelivery) -> send `ack` again, do NOT execute again.
Logs: an info log every ~10 s ("heartbeat ok, battery 87%"), a warn once when battery < 20 %.
Chaos: randomly close the socket according to CHAOS_DISCONNECT_PER_MIN, then reconnect (exercises
load-balanced reconnect + command redelivery).

## 18. Observability files (ops/)

- prometheus.yml: scrape_interval 5s; jobs `api` (api-1:8000, api-2:8000, metrics_path /metrics),
  `rabbitmq` (rabbitmq:15692, metrics_path /metrics/per-object for per-queue series), `prometheus` (localhost:9090); `rule_files: [alerts.yml]`.
- alerts.yml (group robot-console):
  ApiReplicaDown `up{job="api"} == 0` for 15s;
  NoRobotsConnected `sum(rc_ws_connections{kind="robot"}) < 1` for 30s;
  CommandAckSlow `histogram_quantile(0.95, sum by (le) (rate(rc_command_ack_latency_seconds_bucket[1m]))) > 2` for 1m;
  CommandsExpiring `increase(rc_command_transitions_total{to_status="expired"}[5m]) > 0`.
- grafana: datasource uid `prometheus` (http://prometheus:9090, default); dashboard "Robot Console" (uid
  `robot-console`) with panels: robots connected (stat), dashboards connected (stat), API replicas up (stat),
  telemetry msg/s by instance, HTTP p95 latency by route, command transitions/s by to_status,
  command ack latency p95, RabbitMQ messages ready (`rabbitmq_queue_messages_ready`).
  Anonymous access enabled with Viewer role; admin/admin.

## 19. Docker

- backend/Dockerfile: python:3.12-slim; `ARG PIP_INDEX_URL` (optional mirror for China); install requirements;
  copy app; non-root user; `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers","--forwarded-allow-ips","*"]`;
  HEALTHCHECK via python urllib on http://127.0.0.1:8000/api/health.
- frontend/Dockerfile: multi-stage `node:22-alpine` (ARG NPM_REGISTRY optional) -> `nginx:1.27-alpine`.
- simulator/Dockerfile: python:3.12-slim, `ARG PIP_INDEX_URL`.
- docker-compose.yml: `x-api` YAML anchor shared by api-1/api-2; healthchecks for postgres (pg_isready),
  redis (redis-cli ping), rabbitmq (rabbitmq-diagnostics -q ping), api (/api/health); build args pass
  PIP_INDEX_URL / NPM_REGISTRY from .env (empty by default).
- .env.example lists every variable with dev defaults and commented China mirror examples
  (https://pypi.tuna.tsinghua.edu.cn/simple, https://registry.npmmirror.com).

## 20. CI (.github/workflows/ci.yml)

On push and pull_request; actions/checkout@v7, actions/setup-python@v7, actions/setup-node@v7 (Node 24 runtime). Jobs:
1. backend: services postgres:16-alpine, redis:7-alpine, rabbitmq:3.13-management-alpine with health options;
   python 3.12; `pip install -r backend/requirements-dev.txt`; `ruff check backend`; `alembic upgrade head` and
   `python -m app.seed` (working-directory backend, DATABASE_URL pointing at the service); `pytest -q`
   (unit + integration, RUN_INTEGRATION=1).
2. frontend: node 22; `npm ci`; `npm run build` (working-directory frontend).
3. simulator: python 3.12; `pip install -r simulator/requirements.txt pytest==9.1.1`; `pytest -q simulator/tests`.
4. docker: copy .env.example to .env; `docker compose config -q`; `docker compose build`.

## 21. Tests

- Backend unit (no services): state machine full table (42 pairs); security (bcrypt hash/verify, JWT create/verify,
  expired token, role check, ws ticket payload); schemas (command params bounds, telemetry validation);
  transitions (result_timeout, swept expiry, ignored events); dispatcher ownership check (fail-open, superseded ->
  no dispatch); RobotStateWriter (latest sample only, logs kept through an outage, bounded, rate-limited errors);
  Redis outbox (order, drop when full, keeps going after errors); LogThrottle.
- Backend integration (`@pytest.mark.integration`; skipped unless env `RUN_INTEGRATION=1`; uses real
  PostgreSQL/Redis/RabbitMQ from env): login ok/bad; list robots; admin creates command -> 202 and
  same Idempotency-Key -> 200 with the same id; viewer -> 403; health -> 200; docs under /api; robot header auth
  (wrong/missing header and query-string token -> 4401); ws-ticket requires JWT, 30 s TTL, works once, expired ticket
  rejected, JWT expiry closes the socket with 4401; dispatch/ack/result round trip and redelivery; stale connection
  yields with 4409 when the online key names another replica (attempts unchanged); sweeper transitions and
  SKIP LOCKED (a locked row is skipped, not waited on); RabbitMQ outage (simulated by pointing aio-pika's reconnect
  at a dead port): consumers reopen, and no ghost consumer remains for a robot that left during the outage.
- Simulator unit: robot model; config defaults (TELEMETRY_HZ = 5); token in the Authorization header, not the URL.
- scripts/e2e_check.py (stdlib + httpx or urllib; runs against http://localhost:8080 by default):
  login as admin; wait until >= 3 robots online (timeout 60 s); send move_to to robot-1; poll until
  status succeeded or failed (timeout 60 s); print the events timeline; send the same Idempotency-Key twice and
  assert same id; call /api/health 6 times and print the X-Upstream / X-Replica values to show load balancing;
  exit non-zero on failure.
