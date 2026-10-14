# robot-console

A small robot-fleet cloud console: simulated robots report telemetry over WebSocket, operators send commands from a live dashboard, commands travel through RabbitMQ with delivery guarantees, and the whole stack ships with metrics and alerts.

[![CI](https://github.com/AlanCommitK/robot-console/actions/workflows/ci.yml/badge.svg)](https://github.com/AlanCommitK/robot-console/actions/workflows/ci.yml)

中文版本（主文档）：[README.md](README.md)

![Dashboard: robot table, battery and speed charts and x-y trajectory of the selected robot](docs/dashboard.png)

<table>
  <tr>
    <td width="50%"><img src="docs/grafana.png" alt="Grafana dashboard: connections, telemetry rate, HTTP and ack latency, RabbitMQ queue depth"></td>
    <td width="50%"><img src="docs/login.png" alt="Login page with the admin and viewer default accounts"></td>
  </tr>
</table>

The UI labels are in Chinese. The detailed design documents under `docs/` are written in Chinese; this README covers the same facts in English.

## What it is

robot-console is a scaled-down "robot fleet cloud platform" that runs the three main paths such a system needs end to end:

- **Telemetry (up)**: four simulated robots (`AGV-01` to `AGV-04`) each keep one WebSocket connection open and report position, speed, battery and mode at 5 Hz. The server keeps the latest state in Redis and writes a downsampled stream (at most one sample per robot per 0.5 s) to PostgreSQL in batches.
- **Commands (down)**: an operator sends `move_to`, `set_speed` or `stop`. The command is stored first, then published to that robot's own durable RabbitMQ queue; the API replica the robot is connected to consumes it, sends it over the WebSocket and waits for the robot's ack and result. Delivery is at-least-once (manual acks, redelivery after reconnects, dedupe by `command_id`), and a server-side state machine records every step as an auditable event.
- **Live dashboard**: the two API replicas forward events to each other through Redis pub/sub, so a browser connected to either replica sees every robot. The page shows 120 s battery and speed charts and an x-y trajectory.

One `docker compose up` starts Nginx (static SPA, reverse proxy and load balancer across two API replicas), PostgreSQL, Redis, RabbitMQ, Prometheus (with four alert rules) and Grafana (with a provisioned dashboard). By default the simulator drops each robot's connection at random (0.5 times per minute on average) and fails 5% of `move_to` commands, so reconnects and redelivery are exercised continuously.

## Architecture

![Architecture diagram](docs/architecture.svg)

How to read it:

1. The browser talks to a single entry point, Nginx on host port 8080. Nginx serves the static files itself and reverse-proxies `/api/` and `/ws/` to `api-1` / `api-2` round robin; WebSocket handshakes are relayed with the `Upgrade` / `Connection` headers.
2. The simulator also connects through Nginx (`/ws/robot`), so robots spread across both replicas and may land on the other replica after a reconnect.
3. The replicas keep no state that has to be shared: durable data lives in PostgreSQL, live state and cross-replica events in Redis, command messages in RabbitMQ, so any replica can serve any REST request. A WebSocket stays on the replica that accepted the handshake, and that replica dispatches the robot's commands.
4. Prometheus scrapes each replica's `/metrics` directly (not through Nginx, where one series would jump between replicas) plus RabbitMQ's built-in metrics port 15692; Grafana reads from Prometheus.
5. Only Nginx is published on all interfaces. The databases and the admin UIs (RabbitMQ management, Prometheus, Grafana) bind to 127.0.0.1 on the host because they use well-known default credentials or no authentication.

## Key design points

Each point is mechanism plus reason; the full reasoning is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (Chinese).

- **Command delivery: one durable queue per robot, manual ack, prefetch 1, publisher confirms.** The API returns 202 only after the broker confirms the publish, and the consumer acks the RabbitMQ message only after the robot has acked the command, so a crash anywhere in between leaves the message in the queue; prefetch 1 keeps each robot's commands strictly one at a time. Delivery is at-least-once: robots dedupe redeliveries by `command_id` and the server-side state machine absorbs duplicates. → [why RabbitMQ](docs/ARCHITECTURE.md#decision-broker), [queue per robot](docs/ARCHITECTURE.md#decision-queue-per-robot), [at-least-once plus idempotent handling](docs/ARCHITECTURE.md#decision-delivery-semantics)
- **Expiry: message TTL plus a dead-letter exchange instead of polling.** Robot queues are declared with `x-message-ttl` (30 s by default) and `x-dead-letter-exchange: cmd.dlx`; a message that times out in the queue is re-routed by RabbitMQ to `cmd.expired`, and whichever replica consumes it marks the command `expired`. Expiry becomes an ordinary message, triggered when it is due, with no table scans. → [TTL + DLX](docs/ARCHITECTURE.md#decision-expiry)
- **A command state machine advanced under row locks.** The 6-status by 7-event transition table is a pure function with all 42 pairs unit-tested; duplicate, late or out-of-order events return `None` and are ignored, and terminal statuses never change. Every transition runs inside a `SELECT ... FOR UPDATE` row lock and appends an event row, which serializes concurrent handling of the same command on different replicas. → [state machine](docs/ARCHITECTURE.md#state-machine), [row lock vs optimistic locking](docs/ARCHITECTURE.md#decision-row-lock)
- **Idempotency keys backed by a unique constraint.** The `Idempotency-Key` header is stored under a `(robot_id, idempotency_key)` unique constraint: of two concurrent duplicates only one INSERT succeeds, the other catches `IntegrityError` and returns the existing command (HTTP 200 instead of 202). The UI creates a new key per click and reuses it for automatic retries of that click. → [idempotency keys](docs/ARCHITECTURE.md#decision-idempotency)
- **Cross-replica fan-out through Redis pub/sub, without letting Redis slow down commands.** Each replica holds one subscription to the `rc:events` channel and copies events to its local dashboards in-process (one bounded queue of 1000 events per dashboard, so a slow client only drops its own events). The robot receive loop never waits on Redis: telemetry goes to a per-connection writer that only writes the latest sample, and `command_update` notifications go to a bounded per-replica outbox, so commands keep flowing while Redis is down. → [pub/sub vs sticky sessions](docs/ARCHITECTURE.md#decision-fanout), [outbox and buffering](docs/ARCHITECTURE.md#decision-redis-outbox)
- **Robot ownership check.** After a robot reconnects to the other replica, a half-open connection on the old replica would still compete for the same queue; before dispatching, the consumer reads `robot:{id}:online` (the replica name, written by the newest connection and renewed by telemetry), and if it names another replica it backs off, lets the message return to the queue and closes the stale socket with code 4409. If Redis is unavailable it dispatches anyway (fail open). → [ownership check](docs/ARCHITECTURE.md#decision-ownership)
- **Safety-net sweeper with `FOR UPDATE SKIP LOCKED`.** A process killed between "command committed" and "message published", or a robot that acks and never reports a result, leaves a command no event will ever move; every 30 s each replica expires `queued`/`dispatched` commands with no update for TTL + 60 s and fails `acked` commands older than `RESULT_TIMEOUT_S` (300 s by default). `SKIP LOCKED` means concurrent sweepers never process the same row and never wait on each other. → [sweeper](docs/ARCHITECTURE.md#decision-sweeper)
- **Authentication and authorization.** REST uses HS256 JWTs (`{sub, role, exp}`): `admin` may send commands, `viewer` is read-only (403); passwords are bcrypt-hashed and robot tokens are stored as sha256 digests only. Long-lived credentials never go into URLs: robots send an `Authorization` header on the WebSocket handshake, and the browser exchanges its JWT for a single-use ticket (30 s, redeemed with Redis `GETDEL`) before opening `/ws/dashboard`; the socket never outlives the JWT that minted the ticket. → [auth](docs/ARCHITECTURE.md#decision-auth), [one-time ticket](docs/ARCHITECTURE.md#decision-ws-ticket)
- **Nginx: load balancing, WebSocket upgrade, dynamic upstream resolution.** `/api/` and `/ws/` are balanced round robin across the replicas, and `/ws/` upgrades over HTTP/1.1 with `Upgrade`/`Connection` headers; the upstream uses `zone` + `resolver 127.0.0.11 valid=5s` + `server ... resolve`, so a recreated API container with a new IP is picked up automatically. `proxy_next_upstream` fails over only on connection errors and timeouts, not on 502/503, so one broken dependency cannot get both replicas ejected. → [Nginx](docs/ARCHITECTURE.md#decision-nginx-resolve)
- **Metrics and alerts.** Custom `rc_*` metrics are labelled by route template (for example `/api/robots/{robot_id}`) rather than raw path, which keeps label cardinality bounded; Prometheus scrapes both replicas, and the alert rules are `ApiReplicaDown`, `NoRobotsConnected`, `CommandAckSlow` and `CommandsExpiring`. Grafana's data source and dashboard are provisioned from files. → [observability](docs/ARCHITECTURE.md#observability)

## Tech stack

| Layer | Technologies |
|---|---|
| Backend | Python 3.12 (3.13 works locally), FastAPI 0.141, uvicorn, SQLAlchemy 2.0 async + asyncpg, Alembic, redis-py 8, aio-pika 9, prometheus-client, PyJWT, bcrypt, pydantic-settings |
| Frontend | React 18, TypeScript 5, Vite 6, ECharts 6, plain CSS (no UI kit) |
| Infrastructure | PostgreSQL 16, Redis 7, RabbitMQ 3.13 (management + prometheus plugins), Nginx 1.27 |
| Observability | Prometheus v2.54.1 (with alert rules), Grafana 11.2.2 (data source and dashboard provisioned from files) |
| Engineering | Docker Compose, GitHub Actions, pytest, ruff |

## Quick start

Prerequisite: Docker with Compose v2 (the `docker compose` subcommand), for example Docker Desktop or OrbStack. All images are official multi-arch images, so x86-64 and Apple Silicon both work.

```bash
git clone https://github.com/AlanCommitK/robot-console.git
cd robot-console
cp .env.example .env
docker compose up -d --build
```

The first start builds the backend, frontend and simulator images and starts services in dependency order: the two API replicas start only after PostgreSQL, Redis and RabbitMQ pass their health checks and the one-shot `migrate` job (which needs only PostgreSQL) has exited successfully; Nginx starts once both replicas are healthy, and the simulator last. RabbitMQ's Erlang node is the slowest to start. Once `docker compose ps` shows `api-1`, `api-2` and `nginx` as `healthy`:

| Entry point | URL | Bound to | Credentials |
|---|---|---|---|
| Console | http://localhost:8080 | 0.0.0.0:8080 | `admin` / `admin123` (admin), `viewer` / `viewer123` (read-only) |
| API docs (Swagger UI) | http://localhost:8080/api/docs | through Nginx | same accounts (endpoints need a Bearer JWT) |
| Grafana | http://localhost:3000 | 127.0.0.1 | anonymous Viewer; admin `admin` / `admin` |
| Prometheus | http://localhost:9090 | 127.0.0.1 | no authentication |
| RabbitMQ management | http://localhost:15672 | 127.0.0.1 | `guest` / `guest` |
| PostgreSQL / Redis | 127.0.0.1:5432 / 127.0.0.1:6379 | 127.0.0.1 | `robot` / `robot`, database `robot`; Redis has no password |

Only Nginx's port 8080 is published on all interfaces. Every other published port binds to 127.0.0.1 (RabbitMQ's AMQP port 5672 and metrics port 15692 are not published at all and are reachable only inside the compose network) because it uses well-known default credentials (`guest`/`guest`, `admin`/`admin`) or no authentication at all (Prometheus even exposes its `/-/reload` lifecycle endpoint); binding those to 0.0.0.0 would hand them to anyone on the same network. A browser on the same machine reaches them normally; use SSH port forwarding to reach them from elsewhere.

The simulated robots authenticate with `dev-token-robot-1` to `dev-token-robot-4` (the database stores only their sha256 digests). All default passwords and the default `JWT_SECRET` are development values; the API logs a warning at startup when the default secret is in use.

**Sending a command from the UI**: log in as `admin` and click a robot in the robot table (机器人列表). The command panel (下发命令) appears below it: enter X and Y and click 移动 for `move_to` (each coordinate between -50 and 50 m), enter a speed between 0 and 2.0 m/s and click 设置 for `set_speed`, or click 立即停止 for `stop`. The command shows up in the command list (命令记录), and its status badge moves through `queued → dispatched → acked → succeeded` live; click a row to see its event timeline (which replica made each transition and how far apart they were). The `move_to` target is also drawn on the trajectory chart. The panel is disabled for `viewer`. A command sent to an offline robot waits in the queue and turns `expired` if the robot does not come back within 30 s.

**End-to-end smoke check**: `scripts/e2e_check.py` uses only the Python standard library (3.9+) and goes through the same Nginx entry point as the browser:

```bash
python3 scripts/e2e_check.py            # default http://localhost:8080
python3 scripts/e2e_check.py --base-url http://127.0.0.1:8080 --robot robot-2
```

It logs in as admin; waits until at least 3 robots are online (up to 60 s); sends a `move_to` to `robot-1`, polls it to a terminal status and prints the event timeline; posts the same `Idempotency-Key` twice and asserts the same command comes back (202, then 200); and calls `/api/health` six times, printing the `X-Upstream` / `X-Replica` headers to show load balancing. Exit code 0 means every check passed.

**Stopping**:

```bash
docker compose down        # remove containers, keep data volumes
docker compose down -v     # also delete the PostgreSQL / RabbitMQ / Prometheus / Grafana volumes
```

**Package mirrors**: `PIP_INDEX_URL` and `NPM_REGISTRY` in `.env` select the PyPI and npm mirrors used while building the images (for example `https://pypi.tuna.tsinghua.edu.cn/simple` and `https://registry.npmmirror.com`); leave them empty for the official registries, then run `docker compose up -d --build` again.

## Running the tests locally

These mirror the four jobs in [`.github/workflows/ci.yml`](.github/workflows/ci.yml): backend (lint, migrations, unit and integration tests), frontend build, simulator tests and image build. Every block below starts from the repository root.

**Backend unit tests** need no external services. They cover all 42 (status, event) pairs of the state machine; bcrypt, JWT (expired, tampered, `alg=none`, missing role), robot-token digests and WebSocket tickets; validation bounds for command parameters and telemetry; sweeper transition rules; the pre-dispatch ownership check (including fail-open on Redis errors); the Redis writer and outbox (latest sample only, logs kept and bounded through an outage, outbox ordering, drop instead of block when full); and the rate-limited logger.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements-dev.txt
ruff check backend
(cd backend && pytest -q)        # integration tests show as skipped
```

**Backend integration tests** (marker `integration`, run only when `RUN_INTEGRATION=1`) start the full application including its lifespan (RabbitMQ connection, Redis subscriber, sweeper). They cover login and roles, the OpenAPI paths, idempotency-key replay, WebSocket authentication for robots and dashboards (wrong or missing header, token in the query string, single-use 30 s tickets, 4401 when the JWT expires), the dispatch → ack → result round trip, redelivery after a reconnect (`attempt` becomes 2), a stale connection yielding with 4409 without incrementing `attempts`, sweeper transitions and `SKIP LOCKED`, and a simulated RabbitMQ outage (aio-pika's reconnect URL pointed at an unreachable port) that checks consumers reopen and that no ghost consumer is left for a robot that left during the outage.

They need real PostgreSQL, Redis and RabbitMQ. CI uses the three official images; locally you can start the same three containers (the compose stack also uses host ports 5432 and 6379, so do not run both at once; the compose stack does not publish RabbitMQ's 5672 to the host, so it cannot be reused):

```bash
docker run -d --name rc-it-postgres -e POSTGRES_DB=robot -e POSTGRES_USER=robot -e POSTGRES_PASSWORD=robot \
  -p 127.0.0.1:5432:5432 postgres:16-alpine
docker run -d --name rc-it-redis -p 127.0.0.1:6379:6379 redis:7-alpine
docker run -d --name rc-it-rabbitmq -p 127.0.0.1:5672:5672 rabbitmq:3.13-management-alpine
# wait until all three are ready (RabbitMQ is the slowest: look for "Server startup complete" in docker logs rc-it-rabbitmq)

export DATABASE_URL=postgresql+asyncpg://robot:robot@localhost:5432/robot
export REDIS_URL=redis://localhost:6379/0
export RABBITMQ_URL=amqp://guest:guest@localhost:5672/
export RUN_INTEGRATION=1
(cd backend && alembic upgrade head && python -m app.seed && pytest -q)   # unit + integration
```

**Simulator unit tests** are pure logic with no network: motion and battery model, command execution and failure probability, the dedupe set, backoff jitter, configuration defaults, and a check that the token travels only in the `Authorization` header, never in the URL.

```bash
pip install -r simulator/requirements.txt pytest==9.1.1
pytest -q simulator/tests
```

**Frontend type check and build**:

```bash
cd frontend
npm ci
npm run build          # = tsc --noEmit + vite build
npm run dev            # dev server on http://localhost:5173, proxies /api and /ws to http://localhost:8080
```

**Image build**: `cp .env.example .env && docker compose config -q && docker compose build`.

Size as of 2026-10-14: 200 backend tests (161 unit, 39 integration) and 49 simulator tests; reproducing the CI setup locally that day, the full backend suite passed three runs in a row.

## Failure drills

All drills ran against the full compose stack. The numbers come from single runs on one local machine on 2026-10-13 and 2026-10-14; they illustrate behaviour and are not benchmarks. Steps, what to watch and the mechanism behind each result are in [docs/FAILURE_DRILLS.md](docs/FAILURE_DRILLS.md) (Chinese).

| Drill | Observed result |
|---|---|
| Stop `api-1` | The robots that were on `api-1` reconnected through Nginx to `api-2` within 4.5 s, and 8/8 commands sent afterwards succeeded; 4.2 s after `api-1` was started again it was answering through Nginx; the Prometheus alert `ApiReplicaDown` fired |
| Recreate `api-1` with a new IP | Nginx re-resolved the name and followed the new IP (172.18.0.11) instead of the old address, without an Nginx restart |
| Command to an offline robot | Expired 30.5 s after publish via TTL plus dead-letter exchange (`cmd.dlx` → `cmd.expired`); status `expired` |
| Restart RabbitMQ | Both replicas healthy again 9.1 s after the restart command returned; a command published before the restart, for a robot that was offline at the time, succeeded 17.8 s after publish |
| All robots reconnect while RabbitMQ is down | Afterwards 12/12 commands succeeded and every robot queue had exactly one consumer (no ghost consumers) |
| Stop Redis for about 22 s | 10/10 commands sent during the outage succeeded, with a maximum dispatch-to-ack latency of 9 ms; `/api/robots` returned 503; within 0.5 s of Redis returning all robots showed as online again |
| Credentials in logs | 0 lines containing `token=` in the whole Nginx log; 0 unredacted `token=` in the API logs |

`scripts/e2e_check.py` passed 5/5 in the same period.

## Repository layout

```
robot-console/
  README.md                 Chinese README (primary)
  LICENSE                   MIT license
  docker-compose.yml        full stack: postgres, redis, rabbitmq, migrate, api-1/api-2, nginx, simulator, prometheus, grafana
  .env.example              every variable with its development default (copy to .env)
  SPEC.md                   interface and design contract: topology, config, schema, state machine, REST/WebSocket protocol, Redis keys, RabbitMQ topology, metrics
  backend/
    app/
      main.py               app assembly, middleware order, background tasks started in the lifespan
      config.py             pydantic-settings configuration
      models.py, db.py      SQLAlchemy 2.0 async models and engine
      schemas.py            pydantic models for REST, WebSocket messages and events
      security.py           bcrypt, JWT, robot-token digests, WebSocket tickets
      deps.py               dependencies: session, current user, role check
      middleware.py         request id, JSON access log, metrics middleware
      state_machine.py      command state machine (pure function)
      commands_service.py   idempotent command creation, locked transitions, dispatch loop, dead-letter consumer
      mq.py                 RabbitMQ topology, publisher confirms, non-robust channels
      redis_bus.py          Redis keys, pub/sub subscriber, outbox
      ws_hub.py             dashboard fan-out with bounded queues; robot connection registry
      robot_state_writer.py per-connection Redis state writer
      telemetry_writer.py   telemetry downsampling and batched inserts
      sweeper.py            safety-net sweeper for stuck commands (SKIP LOCKED)
      routers/              auth, robots, commands, ws, health
    alembic/                migrations (0001_initial)
    tests/                  unit and integration tests (marker: integration)
  frontend/
    src/                    React + TypeScript: login, dashboard, WebSocket client (tickets, backoff, heartbeat)
    nginx.conf              SPA hosting, reverse proxy, load balancing, WebSocket upgrade
    Dockerfile              multi-stage build: node:22-alpine -> nginx:1.27-alpine
  simulator/
    sim.py, robot_model.py  robot simulator: motion model, command dedupe, reconnects, chaos disconnects
  ops/
    prometheus/             prometheus.yml, alerts.yml
    grafana/                provisioning for data source and dashboards, dashboard JSON
    rabbitmq/               enabled_plugins
  scripts/e2e_check.py      end-to-end smoke check
  docs/                     architecture, failure drills, known limitations, diagrams and screenshots
  .github/workflows/ci.yml  CI: backend (lint, migrations, unit and integration tests), frontend build, simulator tests, image build
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, the three data paths, the state-machine transition table, failure handling, design decisions (decision / alternatives / why / cost)
- [docs/FAILURE_DRILLS.md](docs/FAILURE_DRILLS.md): failure drills with steps, observation points, results and mechanisms
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): known limitations and what a real deployment would add
- [SPEC.md](SPEC.md): interface and design contract (the "SPEC §N" references in code comments point here)

## License

[MIT](LICENSE)
