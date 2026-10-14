# 故障演练

本文记录针对完整 compose 栈做的故障演练：每项演练的目的、操作命令、预期与观察点、实测结果，以及系统为什么这样表现。设计层面的说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 说明

- **环境**：`docker compose up -d --build` 启动的完整栈，全部使用默认配置（`.env` 复制自 `.env.example`），入口为 http://localhost:8080。
- **数字的性质**：实测结果来自两轮演练，第一轮在 2026-10-13，第二轮在 2026-10-14，都是在一台本地机器上的单次运行。数字用来说明行为，不是基准测试；换一台机器，具体耗时会不同。大部分耗时是每 0.5 到 1 s 轮询一次接口得到的，表示首次观察到该状态的时刻，实际发生时间不晚于记录值。
- **随机断线**：模拟器默认让每台机器人平均每分钟随机断线 0.5 次，演练期间可能看到与演练无关的断线重连。想排除它，把 `.env` 里的 `CHAOS_DISCONNECT_PER_MIN` 改为 `0`，再执行 `docker compose up -d simulator`。
- **工具**：命令需要 `curl` 和 `python3`（用于解析 JSON），在仓库根目录、bash 或 zsh 中执行。

## 准备：辅助命令

先在终端里定义下面这些辅助函数，后续各项演练都会用到：

```bash
API=http://localhost:8080
TOKEN=$(curl -s -X POST "$API/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"

# 每台机器人：是否在线、连在哪个副本
robots() {
  curl -s "$API/api/robots" -H "$AUTH" | python3 -c '
import json, sys
for r in json.load(sys.stdin): print(r["id"], "online" if r["online"] else "offline", r["replica"])'
}

# 给一台机器人下发 stop，打印新命令的 id
send() {
  curl -s -X POST "$API/api/robots/$1/commands" -H "$AUTH" \
    -H 'Content-Type: application/json' -d '{"type":"stop","params":{}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'
}

# 一条命令的状态和事件时间线
show() {
  curl -s "$API/api/commands/$1" -H "$AUTH" | python3 -c '
import json, sys
c = json.load(sys.stdin)
print(c["status"], "error=%s" % c["error"], "attempts=%s" % c["attempts"])
for e in c["events"]:
    print("  ", e["at"], e["from_status"], "->", e["to_status"], e["replica"], e["detail"] or "")'
}

# 一条命令从 dispatched 到 acked 的间隔（取自事件时间线）
acklat() {
  curl -s "$API/api/commands/$1" -H "$AUTH" | python3 -c '
import json, sys
from datetime import datetime
ev = {e["to_status"]: datetime.fromisoformat(e["at"].replace("Z", "+00:00")) for e in json.load(sys.stdin)["events"]}
print("%.1f ms" % ((ev["acked"] - ev["dispatched"]).total_seconds() * 1000) if "acked" in ev and "dispatched" in ev else "n/a")'
}

# 连续 4 次健康检查：哪个副本应答（X-Replica）、Nginx 转发到的地址（X-Upstream）
lb() {
  for i in 1 2 3 4; do
    curl -s -o /dev/null -D - "$API/api/health" | grep -iE '^x-(upstream|replica)' | tr -d '\r' | tr '\n' ' '; echo
  done
}

# 两个副本都健康时返回 0（4 次请求会轮流落到两个副本上）
healthy() {
  [ "$(for i in 1 2 3 4; do curl -s -o /dev/null -w '%{http_code} ' "$API/api/health"; done)" = "200 200 200 200 " ]
}

# RabbitMQ 队列：消费者数、待投递、已投递未确认
queues() {
  docker compose exec -T rabbitmq rabbitmqctl -q list_queues name consumers messages_ready messages_unacknowledged
}

# Prometheus 里当前处于 pending / firing 的告警
alerts() {
  curl -s http://localhost:9090/api/v1/alerts | python3 -c '
import json, sys
for a in json.load(sys.stdin)["data"]["alerts"]: print(a["labels"]["alertname"], a["labels"].get("instance", ""), a["state"])'
}
```

JWT 默认 8 小时过期，过期后重新执行开头的 `TOKEN=...` 一行即可。

## 演练一览

| # | 演练 | 日期 | 实测结果 |
|---|---|---|---|
| 1 | [停掉一个 API 副本](#drill-replica-down) | 10-13、10-14 | 4.5 s 内全部机器人连到 `api-2`；8/8 条命令成功；`ApiReplicaDown` 触发 |
| 2 | [重建 API 副本使其换 IP](#drill-new-ip) | 10-13 | Nginx 跟到新地址 172.18.0.11，没有重启 Nginx |
| 3 | [给离线机器人下发命令](#drill-ttl) | 10-13 | 发布后 30.5 s 经死信变为 `expired` |
| 4 | [重启 RabbitMQ](#drill-rabbitmq-restart) | 10-13、10-14 | 重启前入队的命令在发布后 17.8 s 成功执行 |
| 5 | [RabbitMQ 停机期间机器人断线重连](#drill-ghost-consumer) | 10-14 | 每个机器人队列恰好 1 个消费者；12/12 条命令成功 |
| 6 | [停掉 Redis](#drill-redis-down) | 10-14 | 约 22 s 停机期间 10/10 条命令成功，最大 ack 延迟 9 ms |
| 7 | [日志中不出现长期凭证](#drill-credentials) | 10-14 | Nginx 日志中含 `token=` 的行数为 0 |

两轮演练中，端到端冒烟检查 `python3 scripts/e2e_check.py` 均为 5/5 通过。

<a id="drill-replica-down"></a>

## 1. 停掉一个 API 副本

### 目的

验证一个副本宕机时：机器人经 Nginx 重连到另一个副本，命令照常执行，监控能发现宕机；副本恢复后重新承接流量。

### 操作

```bash
robots                                   # 记下每台机器人连在哪个副本
docker compose stop api-1
sleep 10 && robots                       # 全部机器人应已连到 api-2
for r in robot-1 robot-2 robot-3 robot-4; do send $r; send $r; done > /tmp/rc-ids.txt
sleep 5
while read id; do show $id | head -1; done < /tmp/rc-ids.txt
alerts                                   # ApiReplicaDown：先 pending，15 s 后 firing
docker compose start api-1
until healthy; do sleep 1; done
lb                                       # api-1 重新出现在应答者里
```

### 预期与观察点

- `docker compose logs simulator` 里，原先连在 api-1 的机器人出现 `disconnected`，随后是 `hello received` 和 `"replica": "api-2"`；
- `robots` 显示 4 台机器人都连在 `api-2`；停机期间下发的命令全部 `succeeded`，事件流水里的 `replica` 都是 `api-2`；
- Prometheus 的告警页 http://localhost:9090/alerts（或 `alerts`）里 `ApiReplicaDown{instance="api-1:8000"}` 先 pending，持续 15 s 后变为 firing；Grafana 的 "API replicas up" 从 2 变为 1；
- `docker compose start api-1` 之后，`lb` 的输出里重新出现 `X-Replica: api-1`。已经连在 api-2 上的机器人不会自动迁回，要等它们下次断线重连时才会重新分布。

### 实测结果

- 2026-10-14：停机前 3 台机器人连在 api-1、1 台连在 api-2；执行 `docker compose stop api-1` 后约 4.5 s，4 台全部连在 api-2。随后下发的 8 条命令（每台 2 条）全部成功。执行 `docker compose start api-1` 后约 4.2 s，两个副本都重新经 Nginx 应答，之后又下发的 4 条命令全部成功；此时 4 台机器人仍都连在 api-2。
- 2026-10-13：同样的操作下命令持续成功，`ApiReplicaDown` 进入 firing。

### 机制解释

- **机器人重连**：副本退出时关闭 WebSocket，模拟器按指数退避加随机抖动重连（第一次等待 0.5 到 1 s）。Nginx 转发握手时，连 api-1 失败（容器已停止）就按 `proxy_next_upstream error timeout` 换到 api-2；10 s 内失败 2 次后，api-1 在接下来的 10 s 内被跳过（`max_fails=2 fail_timeout=10s`），建连超时只有 2 s，故障转移不会卡住。
- **命令不丢**：api-1 退出前关闭了它为各台机器人打开的 channel，未确认的消息回到各自的 `cmd.robot.{id}` 队列。机器人连上 api-2 后，api-2 为它打开新的消费者，继续消费同一个队列；新命令发布到同一个队列，由 api-2 派发。
- **在线标记**：api-1 退出时对它的连接执行"比较后删除"：如果机器人已经在 api-2 上写入了新的在线标记，删除会被跳过，也不会广播"离线"。即使 api-1 来不及清理，标记也会在 5 s 内过期。
- **告警**：Prometheus 每 5 s 抓取 `api-1:8000/metrics` 失败，`up{job="api",instance="api-1:8000"}` 变为 0；规则每 5 s 计算一次，条件持续 15 s 后进入 firing。

<a id="drill-new-ip"></a>

## 2. 重建 API 副本使其换 IP

### 目的

验证 Nginx 能跟随容器 IP 的变化，而不是继续向启动时解析到的旧地址转发。为了让结论更有说服力，旧地址被另一个容器占用，这样发往旧地址的请求不可能碰巧成功。

### 操作

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' robot-console-api-1-1   # 旧地址
docker compose stop api-1
# 占位容器：挂到同一个网络上，占住 api-1 刚释放的地址（它不监听 8000 端口）
docker run -d --rm --name ip-placeholder --network robot-console_default --entrypoint sleep redis:7-alpine 600
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ip-placeholder         # 应等于旧地址
docker compose up -d --force-recreate api-1
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' robot-console-api-1-1   # 新地址
sleep 6 && lb
docker stop ip-placeholder
```

### 预期与观察点

- 占位容器拿到的正是 api-1 的旧地址，重建后的 api-1 是一个新地址；
- `lb` 的输出里，`X-Replica: api-1` 对应的 `X-Upstream` 是新地址，期间没有重启或重载 Nginx；
- 作为对照：如果 Nginx 仍然使用旧地址，请求会连到不监听 8000 端口的占位容器，连接被拒绝后转给 api-2，`lb` 里将只看到 `api-2`，Nginx 的 error log 会出现 `connect() failed (111: Connection refused) while connecting to upstream`。

### 实测结果

2026-10-13：在旧地址被另一个容器占用的情况下重建 api-1，新地址为 172.18.0.11；Nginx 随即把请求发往新地址，不再向旧地址转发，没有重启 Nginx。

### 机制解释

`frontend/nginx.conf` 的 upstream 块：

```nginx
upstream api_upstream {
    zone api_upstream 64k;
    resolver 127.0.0.11 valid=5s ipv6=off;
    server api-1:8000 max_fails=2 fail_timeout=10s resolve;
    server api-2:8000 max_fails=2 fail_timeout=10s resolve;
}
```

`resolve` 让 Nginx 每 5 s（`valid=5s`）向 Docker 内置 DNS（`127.0.0.11`）重新查询 `api-1`，地址变化后更新共享内存区 `zone` 里的服务器列表，轮询和失败计数照常工作。不加 `resolve` 时，Nginx 只在启动时解析一次，之后一直使用旧地址，直到自己被重启。开源版 Nginx 从 1.27.3 起支持这个参数。

<a id="drill-ttl"></a>

## 3. 给离线机器人下发命令（TTL + 死信过期）

### 目的

验证命令在目标机器人离线超过 TTL（默认 30 s）后被自动标记为 `expired`，不依赖任何扫描任务。

### 操作

```bash
docker compose stop simulator
sleep 3 && robots                        # 4 台都显示 offline
CID=$(send robot-1); show $CID           # queued
queues                                   # cmd.robot.robot-1：consumers 0，messages_ready 1
sleep 35
show $CID                                # expired
queues                                   # messages_ready 回到 0
alerts                                   # CommandsExpiring firing；NoRobotsConnected 也会在 30 s 后 firing
docker compose start simulator
```

### 预期与观察点

- 机器人离线时它的队列没有消费者，消息停在 `messages_ready`；
- 约 30 s 后命令变为 `expired`：事件流水是 `queued → expired`，detail 为 `TTL elapsed while waiting in the queue (dead-lettered)`，`error` 为 `expired`；
- `rc_command_transitions_total{to_status="expired"}` 增加，告警 `CommandsExpiring` 在下一次规则计算时触发（这条规则没有 `for`，过期一条也要让人知道）；
- 看板上，给离线机器人下发命令时面板会提示"30 秒内未上线将变为已过期"。

### 实测结果

2026-10-13：命令在发布后 30.5 s 变为 `expired`，路径为 `cmd.robot.robot-1` → `cmd.dlx` → `cmd.expired`。

### 机制解释

机器人队列声明了 `x-message-ttl=30000` 和 `x-dead-letter-exchange=cmd.dlx`。消息在队列里待满 30 s 仍没被取走，RabbitMQ 把它转投到 fanout 交换机 `cmd.dlx`，进入 `cmd.expired`。两个副本竞争消费 `cmd.expired`，拿到的那个按消息的 `message_id` 找到命令，在行锁下应用 `expire`。测得的时间包括 TTL 本身，以及死信转投、消费和写库。

如果死信到达时命令已经是终态（例如机器人恰好刚完成），状态机返回 `None`，死信被确认后丢弃，终态不会被改写。

<a id="drill-rabbitmq-restart"></a>

## 4. 重启 RabbitMQ

### 目的

验证 broker 重启不会丢失已经入队的命令，API 副本能自动恢复消费和发布。

### 操作

```bash
docker compose stop simulator            # 让命令留在队列里等待
CID=$(send robot-1); show $CID           # queued；30 s 的 TTL 从此刻开始
docker compose restart rabbitmq
until healthy; do sleep 1; done          # 两个副本都恢复健康
docker compose start simulator           # 需要在 TTL 内让机器人重新上线
sleep 10 && show $CID                    # succeeded
queues                                   # 每个机器人队列 1 个消费者，cmd.expired 2 个
```

### 预期与观察点

- 重启期间 `/api/health` 返回 503，`checks.rabbitmq` 为 `ConnectionError: not connected to rabbitmq`；
- API 日志出现 `consumer stopped (channel closed); reopening`，随后消费者重新打开；
- 恢复后 `queues` 显示每个机器人队列恰好 1 个消费者，`cmd.expired` 有 2 个（每个副本一个）；
- 命令走完 `queued → dispatched → acked → succeeded`；
- 如果在 RabbitMQ 停机期间下发新命令：发布在 5 s 预算内失败，接口返回 503，命令被标记为 `failed`（`publish_failed`）。

### 实测结果

- 2026-10-14：`docker compose restart rabbitmq` 返回后约 9.1 s，两个副本的健康检查都恢复为 200；重启前发布的命令在发布后约 17.8 s 观察到 `succeeded`，时间线为 `queued`（api-2 创建）→ `dispatched` → `acked` → `succeeded`（均在 api-1）。
- 2026-10-13：重启前入队的命令同样在 broker 恢复后执行成功。

### 机制解释

- **消息持久化**：命令以持久消息（`delivery_mode=2`）发布到持久队列，且要等到 publisher confirm 才向客户端返回 202，所以已返回 202 的命令已被 broker 可靠接收。RabbitMQ 数据在卷 `rabbitmq-data` 里，容器主机名固定为 `rabbitmq`，重启后节点名不变，能找回原来的队列。
- **自动恢复**：连接由 `connect_robust` 自动重连。每个消费循环（`CommandDispatcher._keep_consuming`）发现 channel 已关闭，就先暂停 1 s，再等待连接就绪（`_open_channel` 开头的 `connection.ready()`），然后打开新 channel、重新声明拓扑（机器人队列；死信消费者还会重新声明 `cmd.dlx`、`cmd.expired` 和它们之间的绑定）并继续消费；发布 channel 在下一次发布时重新打开。
- **在途消息**：重启时尚未确认的消息会回到队列，重新投递时 `attempts` 加 1，机器人按 `command_id` 去重。

<a id="drill-ghost-consumer"></a>

## 5. RabbitMQ 停机期间机器人断线重连

### 目的

验证"机器人恰好在 broker 断线期间离开"不会留下幽灵消费者（ghost consumer：已经没有代码在读、却仍在 broker 上占着消息的消费者）。

### 操作

```bash
queues                                   # 每个机器人队列 1 个消费者
docker compose stop rabbitmq
docker compose restart simulator         # 所有机器人在 broker 停机期间断开并重连
docker compose start rabbitmq
until healthy; do sleep 1; done
queues                                   # 每个 cmd.robot.* 仍然恰好 1 个消费者
for r in robot-1 robot-2 robot-3 robot-4; do send $r; send $r; send $r; done > /tmp/rc-ids.txt
sleep 6
while read id; do show $id | head -1; done < /tmp/rc-ids.txt | sort | uniq -c
```

### 预期与观察点

- 恢复后每个 `cmd.robot.*` 队列的 consumers 为 1，`messages_unacknowledged` 为 0；
- 12 条命令全部 `succeeded`；
- 如果存在幽灵消费者，对应队列的 consumers 会是 2，并且有消息一直停在 `messages_unacknowledged`：那台机器人的命令既不会下发，也不会过期（已投递未确认的消息不受 TTL 约束）。

### 实测结果

2026-10-14：执行 `docker compose start rabbitmq` 后约 12.2 s，两个副本恢复健康；之后每个机器人队列恰好 1 个消费者，没有残留的消费者；12/12 条命令成功。

### 机制解释

所有 channel 都是普通的 `aio_pika.Channel`，不是 robust channel（见 [ARCHITECTURE.md 第 6.14 节](ARCHITECTURE.md#decision-plain-channels)）。机器人在 broker 停机期间断开时，它的派发任务被取消，channel 早已随连接断开而关闭；普通 channel 不会在重连后被 aio-pika 自动恢复，所以不会出现"没人读、却占着消息"的消费者。机器人重新连上后，新的派发任务打开 channel 时会先等连接就绪，broker 恢复后才真正注册消费者，每个队列始终只有当前连接对应的那一个。集成测试 `test_no_ghost_consumer_when_the_robot_leaves_during_an_outage` 在测试环境里覆盖同一场景。

<a id="drill-redis-down"></a>

## 6. 停掉 Redis

### 目的

验证 Redis 停机不影响命令链路，只影响实时视图，并在 Redis 恢复后自动复原。

### 操作

```bash
docker compose stop redis
for r in robot-1 robot-2 robot-3 robot-4 robot-1; do send $r; sleep 2; done > /tmp/rc-ids.txt
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/robots" -H "$AUTH"    # 503
curl -s "$API/api/health"; echo                                          # 503，checks.redis 报错
sleep 3
while read id; do echo "$(show $id | head -1) | ack $(acklat $id)"; done < /tmp/rc-ids.txt
docker compose start redis
sleep 2 && robots                                                         # 重新显示在线
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/robots/robot-1/logs" -H "$AUTH"   # 200
```

### 预期与观察点

- 停机期间下发的命令全部 `succeeded`，`dispatched → acked` 的间隔保持在毫秒级；
- `GET /api/robots`、`GET /api/robots/{id}/logs`、`POST /api/ws-ticket` 和 `/api/health` 返回 503；
- 已经打开的看板连接仍然保持（`pong` 由副本直接回复，不经过 Redis），但收不到推送；新打开的看板换票失败，前端退避重试；
- API 日志里出现限流的警告，每 10 s 最多一条并带 `suppressed` 计数：`failed to write robot state to redis`、`redis publish failed; event dropped`、`redis subscriber failed; reconnecting`、`robot ownership check failed; dispatching anyway`；
- Redis 恢复后：机器人很快重新显示在线；几秒内日志出现 `subscribed to redis channel`，看板重新收到推送；日志接口恢复 200。

### 实测结果

2026-10-14：Redis 停机约 22 s，期间每 2 s 下发一条命令，10/10 成功，下发到 ack 的最大延迟为 9 ms（同日的另一次运行为 10/10 成功、最大 10 ms）；`/api/robots` 在停机期间返回 503；Redis 恢复后约 0.5 s，4 台机器人全部重新显示在线，日志接口恢复 200；两个副本在 Redis 就绪后约 3 s 重新订阅 `rc:events`。

### 机制解释

- **接收循环不等待 Redis**（[ARCHITECTURE.md 第 6.13 节](ARCHITECTURE.md#decision-redis-outbox)）：ack 和结果的处理只涉及内存里的 Future 和 PostgreSQL；遥测和日志交给每条连接的状态写入任务，`command_update` 进入发件箱，二者都不阻塞接收循环。
- **归属检查 fail open**：派发前读不到在线标记就照常派发，最多等 0.5 s。
- **状态写入任务**：失败后 1 s 重试，期间只保留最新的样本；Redis 回来后的第一次写入就重建 `robot:{id}:online` 和 `robot:{id}:state`，所以机器人很快重新显示在线。
- **订阅任务**：按 1 s、2 s、4 s 退避重连，上限 5 s，所以 Redis 恢复后几秒内看板就能重新收到推送。
- **REST 返回 503**：`/api/robots` 需要读 Redis 里的在线标记和最新状态，读不到时明确返回 503，而不是把所有机器人显示为离线。

<a id="drill-credentials"></a>

## 7. 日志中不出现长期凭证

### 目的

验证长期有效的凭证（机器人 token、JWT）不会出现在 Nginx 或 API 的日志里，即使在上游故障、Nginx 写出完整请求行的时候。

### 操作

在完成上面几项演练之后（期间有机器人反复重连，副本宕机也让 Nginx 记录了上游错误）：

```bash
docker compose logs nginx | grep -c 'token='                       # 0
docker compose logs api-1 api-2 | grep -cE 'token=[^*]'             # 0：没有未打码的 token=
docker compose logs nginx | grep '\[error\]' | grep '/ws/' | head -3
```

### 预期与观察点

- 两个计数都是 0；
- Nginx 的 error log 在上游失败时会写出完整的请求行，例如 `request: "GET /ws/robot?robot_id=robot-4 HTTP/1.1"`，其中只有 `robot_id`，没有凭证。

### 实测结果

2026-10-14：Nginx 全部日志中包含 `token=` 的行数为 0；API 日志中未打码的 `token=` 为 0 处。

### 机制解释

- 机器人在 WebSocket 握手的 `Authorization: Bearer` 请求头里携带 token，Nginx 的两种日志格式都不记录请求头；查询参数里的 `token` 不被接受，连接以 4401 关闭，服务端日志用 `token_in_query` 字段标出这种情况。
- 浏览器看板用一次性票据连接 `/ws/dashboard?ticket=...`。`/ws/` 的访问日志格式只记录 `$uri`（不含查询参数）；error log 会写出完整请求行，但其中最多出现已经用过或 30 s 内失效的票据。
- 后端的 JSON 日志对消息里的 `token=...` 统一打码为 `token=***`，作为客户端配置错误时的最后一道防线。
