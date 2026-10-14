# robot-console

一个小型的机器人集群云端控制台：模拟机器人通过 WebSocket 上报遥测，操作员在实时看板上下发命令，命令经 RabbitMQ 可靠投递，并配有指标与告警。

[![CI](https://github.com/AlanCommitK/robot-console/actions/workflows/ci.yml/badge.svg)](https://github.com/AlanCommitK/robot-console/actions/workflows/ci.yml)

English version: [README.en.md](README.en.md)

![控制台看板：机器人列表、选中机器人的电量/速度曲线、x-y 轨迹](docs/dashboard.png)

<table>
  <tr>
    <td width="50%"><img src="docs/grafana.png" alt="Grafana 看板：连接数、遥测速率、HTTP 与 ack 延迟、RabbitMQ 队列积压"></td>
    <td width="50%"><img src="docs/login.png" alt="登录页：admin 与 viewer 两个默认账号"></td>
  </tr>
</table>

## 它是什么

robot-console 是一个缩小版的"机器人车队云平台"，完整地跑通了这类系统的三条主链路：

- **遥测上行**：4 台模拟机器人（`AGV-01` 到 `AGV-04`）各自保持一条 WebSocket 长连接，以 5 Hz 上报位置、速度、电量和模式；服务端把最新状态写进 Redis，按 0.5 s 一条降采样后批量落入 PostgreSQL。
- **命令下行**：操作员在看板上下发 `move_to` / `set_speed` / `stop`。命令先入库，再发布到 RabbitMQ 中该机器人专属的持久队列，由机器人所在的 API 副本取出、经 WebSocket 下发，等待机器人 ack 和执行结果。整条链路是至少一次投递（at-least-once）：消息手动确认、断线自动重投、按 `command_id` 去重，并用一个服务端状态机把每一步落成可追溯的事件流水。
- **看板实时更新**：两个 API 副本通过 Redis pub/sub 互相转发事件，浏览器连到任意一个副本都能看到全部机器人；页面带 120 s 的电量/速度曲线和 x-y 轨迹。

整套系统用 Docker Compose 一键启动，包含 Nginx（托管前端 + 反向代理 + 在两个 API 副本间负载均衡）、PostgreSQL、Redis、RabbitMQ、Prometheus（含 4 条告警规则）和 Grafana（自动装载的看板）。模拟器默认会随机断线（每台机器人平均每分钟 0.5 次）并以 5% 的概率让 `move_to` 失败，用来持续演练重连与重投。

## 架构

![架构图](docs/architecture.svg)

怎么读这张图：

1. 浏览器只访问 Nginx 一个入口（宿主机 8080 端口）。静态页面由 Nginx 直接返回，`/api/` 和 `/ws/` 被反向代理到 `api-1` / `api-2`，轮询分配；WebSocket 握手通过 `Upgrade` / `Connection` 头接力给后端。
2. 模拟器也走 Nginx 的 `/ws/robot`，所以机器人会分散落在两个副本上，而且重连后可能换副本。
3. 两个 API 副本不保存需要彼此共享的状态：持久数据在 PostgreSQL，实时状态和跨副本事件在 Redis，命令消息在 RabbitMQ，所以任何副本都能处理任何 REST 请求。WebSocket 连接则落在接受握手的那个副本上，由它负责这台机器人的命令派发。
4. Prometheus 直接抓取每个副本的 `/metrics`（不经过 Nginx，否则同一条时间序列会在两个副本间来回跳变），同时抓取 RabbitMQ 自带的指标端口 15692；Grafana 从 Prometheus 读数。
5. 只有 Nginx 发布在所有网卡上。数据库和各运维界面（RabbitMQ 管理台、Prometheus、Grafana）都只绑定宿主机的 127.0.0.1，因为它们用的是公开的默认口令或没有认证。

## 关键设计

每一条都是"机制 + 为什么"，完整推导见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

- **命令投递：每台机器人一个持久队列，手动 ack，prefetch 1，发布确认。** 发布端拿到 broker 的 publisher confirm 才返回 202，消费端等机器人回 ack 之后才确认 RabbitMQ 消息，中间任何一环崩溃消息都还在队列里；prefetch 1 让同一台机器人的命令严格一条一条下发。整体语义是至少一次投递，重投由机器人按 `command_id` 去重、由服务端状态机吸收。→ [为什么用 RabbitMQ](docs/ARCHITECTURE.md#decision-broker)、[按机器人分队列](docs/ARCHITECTURE.md#decision-queue-per-robot)、[至少一次 + 幂等处理](docs/ARCHITECTURE.md#decision-delivery-semantics)
- **过期：消息 TTL + 死信交换机，而不是定时轮询。** 机器人队列带 `x-message-ttl`（默认 30 s）和 `x-dead-letter-exchange: cmd.dlx`，消息在队列里超时后由 RabbitMQ 转投到 `cmd.expired`，任一副本消费到就把命令标为 `expired`。过期因此成了一条普通消息，到期即触发，不需要扫表。→ [TTL + DLX](docs/ARCHITECTURE.md#decision-expiry)
- **命令状态机在行锁下推进。** 6 个状态 × 7 种事件的转移表是一个纯函数（42 种组合全部有单元测试），重复、迟到、乱序的事件一律返回 `None` 被忽略，终态不可再改。每次转移都在 `SELECT ... FOR UPDATE` 行锁内完成并写一条事件流水，两个副本对同一条命令的并发处理因此被串行化。→ [状态机](docs/ARCHITECTURE.md#state-machine)、[行锁 vs 乐观锁](docs/ARCHITECTURE.md#decision-row-lock)
- **幂等键由唯一约束保证。** `Idempotency-Key` 落在 `(robot_id, idempotency_key)` 唯一约束上，并发的重复请求只有一个 INSERT 成功，另一个捕获 `IntegrityError` 后返回已存在的命令（HTTP 200 而不是 202）。前端每次点击生成新 key，同一次点击内的自动重试复用同一个 key。→ [幂等键](docs/ARCHITECTURE.md#decision-idempotency)
- **跨副本扇出用 Redis pub/sub，且 Redis 不拖慢命令链路。** 每个副本只开一条到频道 `rc:events` 的订阅，再在进程内复制给本地看板（每个看板一个 1000 条的有界队列，慢客户端只丢自己的事件）。机器人连接的接收循环从不等待 Redis：遥测交给每连接一个的写入任务（只写最新一条），`command_update` 通知进入每副本一个的有界发件箱，所以 Redis 停机时命令照常派发和确认。→ [pub/sub vs 会话粘滞](docs/ARCHITECTURE.md#decision-fanout)、[发件箱与缓冲](docs/ARCHITECTURE.md#decision-redis-outbox)
- **机器人归属检查。** 机器人重连到另一个副本后，旧副本上的半开连接仍会和新连接争抢同一个队列；派发前读 Redis 键 `robot:{id}:online`（值为副本名，由最新连接写入并随遥测续期），指向别的副本就放弃派发、让消息回到队列，并以关闭码 4409 关掉旧连接。Redis 不可用时照常派发（fail open），宁可退化成没有这层保护，也不停发命令。→ [归属检查](docs/ARCHITECTURE.md#decision-ownership)
- **兜底清扫：`FOR UPDATE SKIP LOCKED`。** 进程恰好死在"命令已入库"与"消息已发布"之间，或机器人 ack 后再也不报结果，这类命令不会再有事件推动；每个副本每 30 s 把超过 TTL + 60 s 未更新的 `queued`/`dispatched` 标为 `expired`，把 ack 后超过 `RESULT_TIMEOUT_S`（默认 300 s）仍无结果的标为 `failed`。`SKIP LOCKED` 让多个副本同时清扫时从不处理同一行，也从不互相等待。→ [清扫器](docs/ARCHITECTURE.md#decision-sweeper)
- **认证与授权。** REST 用 HS256 JWT（`{sub, role, exp}`），`admin` 可下发命令、`viewer` 只读（403）；口令用 bcrypt，机器人 token 只存 sha256 摘要。长期凭证从不进 URL：机器人在 WebSocket 握手里带 `Authorization` 头，浏览器先用 JWT 换一张 30 s 有效、经 Redis `GETDEL` 单次兑换的票据再连 `/ws/dashboard`，连接寿命不超过签发票据的 JWT。→ [认证与授权](docs/ARCHITECTURE.md#decision-auth)、[一次性票据](docs/ARCHITECTURE.md#decision-ws-ticket)
- **Nginx：负载均衡、WebSocket 升级、动态解析上游。** `/api/` 与 `/ws/` 在两个副本间轮询，`/ws/` 用 HTTP/1.1 加 `Upgrade`/`Connection` 头完成协议升级；`upstream` 用 `zone` + `resolver 127.0.0.11 valid=5s` + `server ... resolve`，API 容器重建换了 IP 也能自动跟上。`proxy_next_upstream` 只对连接错误和超时切换副本，不对 502/503 切换，避免一个依赖故障把两个副本同时摘除。→ [Nginx](docs/ARCHITECTURE.md#decision-nginx-resolve)
- **指标与告警。** 自定义 `rc_*` 指标按路由模板（如 `/api/robots/{robot_id}`）而不是原始路径打标签，避免标签基数爆炸；Prometheus 分别抓取两个副本，告警规则为 `ApiReplicaDown`、`NoRobotsConnected`、`CommandAckSlow`、`CommandsExpiring`。Grafana 的数据源和看板由文件自动装载。→ [可观测性](docs/ARCHITECTURE.md#observability)

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12（本地 3.13 亦可）、FastAPI 0.141、uvicorn、SQLAlchemy 2.0 async + asyncpg、Alembic、redis-py 8、aio-pika 9、prometheus-client、PyJWT、bcrypt、pydantic-settings |
| 前端 | React 18、TypeScript 5、Vite 6、ECharts 6，纯 CSS（无 UI 组件库） |
| 基础设施 | PostgreSQL 16、Redis 7、RabbitMQ 3.13（management + prometheus 插件）、Nginx 1.27 |
| 可观测性 | Prometheus v2.54.1（含告警规则）、Grafana 11.2.2（数据源与看板由文件自动装载） |
| 工程 | Docker Compose、GitHub Actions、pytest、ruff |

## 快速开始

前置条件：安装了 Docker 且支持 Compose v2（`docker compose` 子命令），例如 Docker Desktop 或 OrbStack。所有镜像都是官方多架构镜像，x86-64 和 Apple Silicon 都可直接运行。

```bash
git clone https://github.com/AlanCommitK/robot-console.git
cd robot-console
cp .env.example .env
docker compose up -d --build
```

首次启动会构建后端、前端和模拟器镜像，并按依赖顺序启动：PostgreSQL、Redis、RabbitMQ 通过健康检查，且一次性迁移任务 `migrate`（只依赖 PostgreSQL）成功退出之后，才启动两个 API 副本；两个副本都健康后启动 Nginx，最后启动模拟器。RabbitMQ 的 Erlang 节点启动最慢。`docker compose ps` 显示 `api-1`、`api-2`、`nginx` 都是 `healthy` 后即可访问：

| 入口 | 地址 | 绑定 | 账号 |
|---|---|---|---|
| 控制台 | http://localhost:8080 | 0.0.0.0:8080 | `admin` / `admin123`（管理员），`viewer` / `viewer123`（只读） |
| API 文档（Swagger UI） | http://localhost:8080/api/docs | 经 Nginx | 同上（接口需要 Bearer JWT） |
| Grafana | http://localhost:3000 | 127.0.0.1 | 匿名可看（Viewer）；管理员 `admin` / `admin` |
| Prometheus | http://localhost:9090 | 127.0.0.1 | 无认证 |
| RabbitMQ 管理台 | http://localhost:15672 | 127.0.0.1 | `guest` / `guest` |
| PostgreSQL / Redis | 127.0.0.1:5432 / 127.0.0.1:6379 | 127.0.0.1 | `robot` / `robot` / 库 `robot`；Redis 无口令 |

只有 Nginx 的 8080 端口对外发布。其余发布到宿主机的端口全部只绑在 127.0.0.1（RabbitMQ 的 AMQP 端口 5672 和指标端口 15692 根本不发布，只在 compose 内部网络可达）：它们用的是公开的默认口令（`guest`/`guest`、`admin`/`admin`）或根本没有认证（Prometheus 连 `/-/reload` 这类管理接口都开着），绑在 0.0.0.0 上等于把它们交给同一局域网里的任何人。本机浏览器照常可以打开；需要从别的机器访问时用 SSH 端口转发。

模拟机器人的 token 是 `dev-token-robot-1` 到 `dev-token-robot-4`（数据库里只存 sha256 摘要）。所有默认口令和 `JWT_SECRET` 都是开发用值，API 启动时会对默认密钥打一条告警日志。

**在界面上下发一条命令**：用 `admin` 登录，在"机器人列表"里点一台机器人，页面下方出现"下发命令"面板：填 X、Y 后点"移动"（`move_to`，取值 −50 到 50 m）；或在"设置速度"里填 0 到 2.0 m/s 后点"设置"（`set_speed`）；或点"立即停止"（`stop`）。命令立刻出现在"命令记录"里，状态徽章随 排队中 → 已下发 → 已确认 → 成功（`queued → dispatched → acked → succeeded`）实时变化；点一行可以看到"事件时间线"（每次转移发生在哪个副本、相隔多久）。`move_to` 的目标点同时画在轨迹图上。用 `viewer` 登录时面板是禁用的。给离线机器人发的命令会在队列里等待，30 秒内机器人没有上线就变为"已过期"。

**端到端冒烟检查**：`scripts/e2e_check.py` 只用 Python 标准库（3.9+），不需要安装任何依赖，走的是和浏览器一样的 Nginx 入口：

```bash
python3 scripts/e2e_check.py            # 默认 http://localhost:8080
python3 scripts/e2e_check.py --base-url http://127.0.0.1:8080 --robot robot-2
```

它依次：以 admin 登录；等待至少 3 台机器人在线（最多 60 s）；给 `robot-1` 发一条 `move_to` 并轮询到终态、打印事件时间线；用同一个 `Idempotency-Key` 连发两次并断言返回同一条命令（202 然后 200）；调用 6 次 `/api/health` 并打印 `X-Upstream` / `X-Replica` 响应头以展示负载均衡。全部通过退出码为 0。

**停止**：

```bash
docker compose down        # 停掉并删除容器，数据卷保留
docker compose down -v     # 同时删除 PostgreSQL / RabbitMQ / Prometheus / Grafana 的数据卷
```

**国内网络**：`.env` 里的 `PIP_INDEX_URL` 和 `NPM_REGISTRY` 可以指定构建镜像时使用的 PyPI 和 npm 镜像源（例如 `https://pypi.tuna.tsinghua.edu.cn/simple` 和 `https://registry.npmmirror.com`），留空则使用官方源。改完后重新 `docker compose up -d --build`。

## 本地测试

与 [`.github/workflows/ci.yml`](.github/workflows/ci.yml) 中的四个 job 一一对应：后端（lint + 迁移 + 单元与集成测试）、前端构建、模拟器测试、镜像构建。下面每段命令都从仓库根目录开始执行。

**后端单元测试**不需要任何外部服务，覆盖：状态机的全部 42 种（状态, 事件）组合；bcrypt、JWT（过期、篡改、`alg=none`、缺少角色）、机器人 token 摘要与 WebSocket 票据；命令参数与遥测消息的校验边界；sweeper 的转移规则；派发前的归属检查（含 Redis 故障时 fail open）；Redis 写入任务与发件箱（只写最新样本、断线期间日志不丢且有界、发件箱保序、满了丢弃而不阻塞）；限流日志。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements-dev.txt
ruff check backend
(cd backend && pytest -q)        # 集成测试显示为 skipped
```

**后端集成测试**（标记 `integration`，只在 `RUN_INTEGRATION=1` 时运行）启动完整的应用（含 lifespan：连接 RabbitMQ、Redis 订阅、sweeper），覆盖：登录与角色、OpenAPI 文档路径、幂等键重放、机器人与看板的 WebSocket 认证（错误/缺失的请求头、查询参数里的 token、票据单次有效与 30 s TTL、JWT 过期时以 4401 关闭）、派发 → ack → 结果的完整往返、断线后重投（`attempt` 变为 2）、过时连接以 4409 让位且不增加 `attempts`、sweeper 的转移与 `SKIP LOCKED`，以及把 aio-pika 的重连地址指向一个不可达端口来模拟 RabbitMQ 断线，验证消费者会重新打开、且机器人在断线期间离开后不残留幽灵消费者。

它需要真实的 PostgreSQL、Redis 和 RabbitMQ。CI 用的是三个官方镜像，本地可以同样起三个容器（compose 栈里的 PostgreSQL / Redis 也占用 5432 / 6379，两者不要同时运行；compose 栈没有把 RabbitMQ 的 5672 端口发布到宿主机，不能直接复用）：

```bash
docker run -d --name rc-it-postgres -e POSTGRES_DB=robot -e POSTGRES_USER=robot -e POSTGRES_PASSWORD=robot \
  -p 127.0.0.1:5432:5432 postgres:16-alpine
docker run -d --name rc-it-redis -p 127.0.0.1:6379:6379 redis:7-alpine
docker run -d --name rc-it-rabbitmq -p 127.0.0.1:5672:5672 rabbitmq:3.13-management-alpine
# 等三个容器就绪后再继续（RabbitMQ 启动最慢，docker logs rc-it-rabbitmq 出现 "Server startup complete" 即可）

export DATABASE_URL=postgresql+asyncpg://robot:robot@localhost:5432/robot
export REDIS_URL=redis://localhost:6379/0
export RABBITMQ_URL=amqp://guest:guest@localhost:5672/
export RUN_INTEGRATION=1
(cd backend && alembic upgrade head && python -m app.seed && pytest -q)   # 单元 + 集成
```

**模拟器单元测试**是纯逻辑测试，不联网：运动与电量模型、命令执行与失败概率、去重集合、退避抖动、默认配置，以及"token 只出现在 `Authorization` 头里、不出现在 URL 里"。

```bash
pip install -r simulator/requirements.txt pytest==9.1.1
pytest -q simulator/tests
```

**前端类型检查与构建**：

```bash
cd frontend
npm ci
npm run build          # = tsc --noEmit + vite build
npm run dev            # 开发服务器 http://localhost:5173，/api 与 /ws 代理到 http://localhost:8080
```

**镜像构建**：`cp .env.example .env && docker compose config -q && docker compose build`。

截至 2026-10-14 的规模：后端 200 个用例（161 个单元 + 39 个集成），模拟器 49 个；当天在本地按 CI 配置复现时，后端完整测试连续 3 次全部通过。

## 故障演练

以下演练都针对完整的 compose 栈执行。数字来自 2026-10-13 和 2026-10-14 在一台本地机器上的单次运行，用来说明行为，不是基准测试。操作步骤、观察点和机制解释见 [docs/FAILURE_DRILLS.md](docs/FAILURE_DRILLS.md)。

| 演练 | 实测结果 |
|---|---|
| 停掉 `api-1` | 原先连在 `api-1` 的机器人在 4.5 s 内经 Nginx 重连到 `api-2`，随后 8/8 条命令成功；`api-1` 重新启动后 4.2 s 起又开始经 Nginx 应答；Prometheus 告警 `ApiReplicaDown` 触发 |
| 重建 `api-1` 使其换 IP | Nginx 通过动态解析跟到新 IP（172.18.0.11），不再向旧地址转发，无需重启 Nginx |
| 给离线机器人下发命令 | 发布后 30.5 s 经 TTL + 死信交换机（`cmd.dlx` → `cmd.expired`）过期，状态变为 `expired` |
| 重启 RabbitMQ | 重启命令返回后 9.1 s 两个副本恢复健康；重启前发布、目标机器人当时离线的命令，在发布后 17.8 s 成功执行 |
| RabbitMQ 停机期间所有机器人断线重连 | 恢复后 12/12 条命令成功；每个机器人队列恰好 1 个消费者（没有残留的幽灵消费者） |
| 停掉 Redis 约 22 s | 停机期间 10/10 条命令成功，下发到 ack 的最大延迟 9 ms；`/api/robots` 返回 503；Redis 恢复后 0.5 s 内机器人全部重新显示在线 |
| 日志里的凭证 | Nginx 全部日志中包含 `token=` 的行数为 0；API 日志中未打码的 `token=` 为 0 |

同期 `scripts/e2e_check.py` 5/5 通过。

## 目录结构

```
robot-console/
  README.en.md              英文版说明
  LICENSE                   MIT 许可证
  docker-compose.yml        全栈编排：postgres、redis、rabbitmq、migrate、api-1/api-2、nginx、simulator、prometheus、grafana
  .env.example              所有环境变量及开发默认值（复制为 .env）
  SPEC.md                   接口与设计契约：拓扑、配置、表结构、状态机、REST/WebSocket 协议、Redis 键、RabbitMQ 拓扑、指标
  backend/
    app/
      main.py               应用装配、中间件顺序、lifespan 中的后台任务
      config.py             pydantic-settings 配置
      models.py, db.py      SQLAlchemy 2.0 async 模型与引擎
      schemas.py            REST / WebSocket / 事件的 pydantic 模型
      security.py           bcrypt、JWT、机器人 token 摘要、WebSocket 票据
      deps.py               依赖项：会话、当前用户、角色检查
      middleware.py         request id、JSON 访问日志、指标中间件
      state_machine.py      命令状态机（纯函数）
      commands_service.py   命令创建（幂等）、状态转移（行锁）、派发循环、死信消费
      mq.py                 RabbitMQ 拓扑、发布确认、非 robust channel
      redis_bus.py          Redis 键、pub/sub 订阅、发件箱
      ws_hub.py             看板连接的有界队列扇出；机器人连接登记
      robot_state_writer.py 每连接一个的 Redis 状态写入任务
      telemetry_writer.py   遥测降采样 + 批量落库
      sweeper.py            卡住命令的兜底清扫（SKIP LOCKED）
      routers/              auth、robots、commands、ws、health
    alembic/                迁移（0001_initial）
    tests/                  单元测试 + 集成测试（marker: integration）
  frontend/
    src/                    React + TypeScript：登录、看板、WebSocket 客户端（换票、退避重连、心跳）
    nginx.conf              SPA 托管、反向代理、负载均衡、WebSocket 升级
    Dockerfile              多阶段构建：node:22-alpine -> nginx:1.27-alpine
  simulator/
    sim.py, robot_model.py  机器人模拟器：运动模型、命令去重、断线重连、混沌断线
  ops/
    prometheus/             prometheus.yml、alerts.yml
    grafana/                数据源与看板的 provisioning 文件、看板 JSON
    rabbitmq/               enabled_plugins
  scripts/e2e_check.py      端到端冒烟检查
  docs/                     架构说明、故障演练、已知限制、图表与截图
  .github/workflows/ci.yml  CI：后端（lint + 迁移 + 单元/集成测试）、前端构建、模拟器测试、镜像构建
```

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：组件职责、三条数据路径、状态机转移表、故障处理、设计决策（决策 / 备选方案 / 为什么 / 代价）
- [docs/FAILURE_DRILLS.md](docs/FAILURE_DRILLS.md)：故障演练的操作、观察点、实测结果与机制解释
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md)：已知限制与真实部署需要补充的东西
- [SPEC.md](SPEC.md)：接口与设计契约（代码注释中的 "SPEC §N" 指向这里）

## 许可证

[MIT](LICENSE)
