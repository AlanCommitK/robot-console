import { ApiError } from './api';
import type { DashboardEvent } from './types';

/**
 * 看板 WebSocket 客户端：/ws/dashboard?ticket=<一次性票据>
 *
 * 为什么走“同源 + Nginx 反代”，而不是直连某个 API 副本（比如 ws://api-1:8000）：
 * 1. 浏览器只认识页面所在的地址。api-1/api-2 是 Docker 内网主机名，浏览器根本解析不到；
 *    统一经过 Nginx 的 /ws/ 入口，再由 Nginx 负载均衡到某个副本。
 * 2. 任何副本都能服务任何看板：每个副本都订阅了 Redis 的 rc:events 频道，所以连到哪个副本都能收到全部事件，
 *    副本挂了，重连时 Nginx 自然会把我们分到另一个副本。
 * 3. 协议跟着页面走：页面是 https 时必须用 wss（否则浏览器按“混合内容”拦截），所以从 location 推导，不写死。
 * 4. 开发环境（Vite 代理）和生产环境（Nginx）用同一个相对路径，代码不用区分环境。
 *
 * 为什么用“一次性票据”而不是把 JWT 放进 URL：
 * 浏览器的 WebSocket API 不允许自定义请求头（没法带 Authorization），凭证只能放进 URL；
 * 而 URL 会被写进日志（Nginx 的 error log 在上游出错时会写出完整的请求行）。JWT 有效期长达数小时，
 * 落进日志就等于泄露。所以每次连接前先用 JWT（在请求头里）调用 POST /api/ws-ticket 换一张票据：
 * 它只能用一次、30 秒后作废，日志里即使出现也没人能再用它。
 * 每次（重新）连接都要换新票：旧票在上一次连接时已经被服务端用掉了。
 */

export type ConnectionState =
  /** 正在建立连接；attempt = 0 表示首次连接，> 0 表示第几次重连 */
  | { status: 'connecting'; attempt: number }
  | { status: 'open' }
  /** 连接断开，正在等待退避计时器；retryAt 是下次尝试的时间（epoch 毫秒） */
  | { status: 'reconnecting'; attempt: number; retryAt: number }
  /** 已停止（登出，或换票时 JWT 被判无效） */
  | { status: 'closed' };

export interface DashboardSocketOptions {
  /** 每次（重新）连接前调用：用 JWT 换一张新的一次性票据（POST /api/ws-ticket）。stop() 时 signal 会被 abort */
  fetchTicket: (signal: AbortSignal) => Promise<string>;
  onEvent: (event: DashboardEvent) => void;
  onStateChange: (state: ConnectionState) => void;
  /** 每次连接成功后调用；reconnect = true 表示这是断线后的重连（调用方应该用 REST 重新拉一遍数据） */
  onOpen?: (info: { reconnect: boolean }) => void;
  /** 换票接口返回 401：JWT 无效或已过期，重连也没用，应该登出 */
  onAuthFailed: () => void;
}

const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
/** 连接保持这么久才算“稳定”，之后断开才把退避次数清零（防止“连上就被踢”时退化成每秒狂连） */
const STABLE_AFTER_MS = 5_000;
const PING_INTERVAL_MS = 20_000;
/** 超过这么久一条消息都没收到（正常情况下至少每 20 秒有一个 pong），就认定连接已死 */
const LIVENESS_TIMEOUT_MS = 45_000;

/**
 * 计算第 attempt 次重连前要等多久（attempt 从 0 开始）。
 *
 * 指数退避：基础等待 1 s、2 s、4 s、8 s……封顶 30 s。
 *   服务端长时间宕机时，客户端不会每秒敲一次门，把刚恢复的服务端再压垮；同时最长等待有上限，恢复后最多 30 s 就能连上。
 * 抖动（jitter）：实际等待时间在 [基础值/2, 基础值] 之间随机。
 *   假设 API 副本重启，所有看板和机器人会在同一瞬间断线；如果大家都严格按 1 s、2 s、4 s 重连，
 *   就会一波一波地“同时”冲向服务端（惊群效应，thundering herd）。加上随机量，重连请求就被摊开了。
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt);
  return Math.round(base / 2 + random() * (base / 2));
}

export function dashboardSocketUrl(ticket: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws/dashboard?ticket=${encodeURIComponent(ticket)}`;
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isStr = (v: unknown): v is string => typeof v === 'string';

/** 运行时校验：网络上来的数据不可信，字段缺失的消息直接丢弃，而不是让后面的代码崩掉 */
export function parseDashboardEvent(raw: unknown): DashboardEvent | null {
  if (!isStr(raw)) return null;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  const o = data as Record<string, unknown>;
  switch (o.type) {
    case 'telemetry':
      // SPEC 约定 ts 是 epoch 秒（数字）；也容忍 iso 字符串，统一换算在 lib/fleetStore.ts 里做
      return isStr(o.robot_id) && (isNum(o.ts) || isStr(o.ts)) && isNum(o.x) && isNum(o.y) && isNum(o.speed) &&
        isNum(o.battery)
        ? (o as unknown as DashboardEvent)
        : null;
    case 'robot_status':
      return isStr(o.robot_id) && typeof o.online === 'boolean' ? (o as unknown as DashboardEvent) : null;
    case 'command_update': {
      const c = o.command as Record<string, unknown> | undefined;
      return c && isStr(c.id) && isStr(c.robot_id) && isStr(c.status) ? (o as unknown as DashboardEvent) : null;
    }
    case 'log':
      return isStr(o.robot_id) && isStr(o.msg) ? (o as unknown as DashboardEvent) : null;
    case 'pong':
      return { type: 'pong' };
    default:
      return null;
  }
}

export class DashboardSocket {
  private readonly opts: DashboardSocketOptions;
  private ws: WebSocket | null = null;
  /** 连续失败次数，决定下一次退避多久 */
  private attempt = 0;
  private stopped = true;
  private everOpened = false;
  private openedAt = 0;
  private lastMessageAt = 0;
  private reconnectTimer: number | undefined;
  private heartbeatTimer: number | undefined;
  /** 正在进行中的换票请求；stop() 时用它取消请求 */
  private ticketRequest: AbortController | null = null;

  constructor(opts: DashboardSocketOptions) {
    this.opts = opts;
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  /** 主动停止（组件卸载 / 登出）：取消换票请求、清理所有计时器，正常关闭（1000），不再重连 */
  stop(): void {
    this.stopped = true;
    window.clearTimeout(this.reconnectTimer);
    this.ticketRequest?.abort();
    this.ticketRequest = null;
    this.stopHeartbeat();
    if (this.ws) {
      this.detach(this.ws);
      this.ws.close(1000, 'client stop');
      this.ws = null;
    }
    this.opts.onStateChange({ status: 'closed' });
  }

  /** 一次连接尝试 = 先换票，再用票据建立 WebSocket。任何一步失败都走同一套退避重连 */
  private connect(): void {
    if (this.stopped) return;
    this.opts.onStateChange({ status: 'connecting', attempt: this.attempt });

    const request = new AbortController();
    this.ticketRequest = request;
    this.opts.fetchTicket(request.signal).then(
      (ticket) => {
        // 换票期间被 stop() 了：这张票就不用了（它 30 秒后自己作废）
        if (request.signal.aborted) return;
        this.ticketRequest = null;
        this.open(ticket);
      },
      (err: unknown) => {
        if (request.signal.aborted) return;
        this.ticketRequest = null;
        this.handleTicketError(err);
      },
    );
  }

  private handleTicketError(err: unknown): void {
    if (err instanceof ApiError && err.status === 401) {
      // JWT 无效或过期：API 客户端已经触发了登出，这里只负责停止重连
      this.stopped = true;
      this.opts.onStateChange({ status: 'closed' });
      this.opts.onAuthFailed();
      return;
    }
    // 网络断了、Nginx 返回 502、Redis 不可用时的 503……都是暂时的，和连接失败一样退避重试
    this.scheduleReconnect();
  }

  private open(ticket: string): void {
    let ws: WebSocket;
    try {
      ws = new WebSocket(dashboardSocketUrl(ticket));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.openedAt = Date.now();
      this.lastMessageAt = Date.now();
      const reconnect = this.everOpened;
      this.everOpened = true;
      this.opts.onStateChange({ status: 'open' });
      this.startHeartbeat();
      this.opts.onOpen?.({ reconnect });
    };

    ws.onmessage = (msg: MessageEvent) => {
      this.lastMessageAt = Date.now();
      const event = parseDashboardEvent(msg.data);
      if (event) this.opts.onEvent(event);
    };

    // onerror 之后浏览器一定会再触发 onclose，统一在 onclose 里处理，避免重复调度重连
    ws.onerror = () => undefined;

    ws.onclose = () => {
      this.ws = null;
      this.handleDisconnect();
    };
  }

  /**
   * 连接断开一律按退避重连处理，包括服务端用 4401 关闭的情况。
   *
   * 4401 = 票据无效或 JWT 已过期。这里不直接登出：票据只能用一次，偶尔会在我们不知情时作废
   * （例如某个副本兑换了票据后崩溃，Nginx 把同一次握手转给了另一个副本）。重连时会换一张新票，
   * “JWT 是否还有效”交给换票接口判断：JWT 真的过期了，换票返回 401（→ handleTicketError → 登出）；
   * 只是票据作废，就能无感恢复。服务端如果一连上就反复拒绝，下面的退避规则会让重连间隔越拉越长。
   */
  private handleDisconnect(): void {
    this.stopHeartbeat();
    if (this.stopped) return;
    // 只有“稳定连接过一段时间”之后的断开才清零退避；刚连上就断（服务端在重启/崩溃循环）则继续加大等待
    if (this.openedAt > 0 && Date.now() - this.openedAt >= STABLE_AFTER_MS) this.attempt = 0;
    this.openedAt = 0;
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    const delay = backoffDelay(this.attempt);
    this.attempt += 1;
    this.opts.onStateChange({ status: 'reconnecting', attempt: this.attempt, retryAt: Date.now() + delay });
    window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = window.setTimeout(() => this.connect(), delay);
  }

  /**
   * 应用层心跳：每 20 s 发 {"type":"ping"}，服务端回 {"type":"pong"}。
   *
   * 为什么不用 WebSocket 协议自带的 ping/pong 帧：浏览器 JS 根本没有发送 ping 帧的 API
   * （浏览器只会被动回复服务端发来的 ping），所以只能在应用层用普通消息模拟。它解决两个问题：
   * 1. 保活：链路上的代理/NAT/负载均衡会掐掉“长时间没有数据”的连接（Nginx 的 proxy_read_timeout 默认只有 60 s，
   *    云厂商负载均衡常见 60~350 s）。没有机器人在线时看板连接可能完全安静，定时 ping 让它始终“有流量”。
   * 2. 探活：网线拔了、Wi-Fi 切换、对端机器宕机时，TCP 可能变成“半开”状态，浏览器很久都不会触发 onclose。
   *    如果超过 LIVENESS_TIMEOUT_MS 一条消息都没收到（连 pong 都没有），就主动判死并立刻重连。
   */
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = window.setInterval(() => {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - this.lastMessageAt > LIVENESS_TIMEOUT_MS) {
        // 半开连接上调用 close() 可能要等很久才触发 onclose，所以先摘掉回调，直接按“已断开”处理
        this.detach(ws);
        ws.close(4000, 'liveness timeout');
        this.ws = null;
        this.handleDisconnect();
        return;
      }
      ws.send(JSON.stringify({ type: 'ping' }));
    }, PING_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    window.clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = undefined;
  }

  private detach(ws: WebSocket): void {
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
  }
}
