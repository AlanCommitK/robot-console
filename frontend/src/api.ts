import type {
  CommandDetail,
  CommandIn,
  CommandOut,
  ErrorBody,
  LoginResponse,
  LogOut,
  Me,
  RobotOut,
  TelemetryOut,
  WsTicket,
} from './types';

/** 所有接口错误都包装成 ApiError；status = 0 表示根本没拿到 HTTP 响应（网络断了、服务没起来）。 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** 组件卸载或切换机器人时会主动取消请求（AbortController），这种“错误”不应该展示给用户。 */
export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/** 把错误翻译成给用户看的中文提示（服务端的 detail 原样附在后面，方便排查）。 */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 0:
        return '无法连接服务器，请检查网络或后端服务是否已启动';
      case 401:
        return '登录已失效，请重新登录';
      case 403:
        return '权限不足：该操作需要管理员角色';
      case 404:
        return `资源不存在（${err.detail}）`;
      case 422:
        return `参数不合法：${err.detail}`;
      default:
        return err.status >= 500 ? `服务器错误 ${err.status}：${err.detail}` : err.detail;
    }
  }
  return err instanceof Error ? err.message : String(err);
}

function extractDetail(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as ErrorBody).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      // FastAPI 的 422：detail = [{loc: [...], msg: "...", type: "..."}]
      const msgs = detail.map((d) => d.msg ?? '').filter(Boolean);
      if (msgs.length > 0) return msgs.join('；');
    }
  }
  return `HTTP ${status}`;
}

/**
 * 生成幂等键（Idempotency-Key）。
 * crypto.randomUUID() 只在“安全上下文”（https 或 localhost）里可用；
 * 如果有人用 http://192.168.x.x:8080 访问，它是 undefined。
 * 所以退回到 crypto.getRandomValues（不受此限制）手工拼一个 UUID v4。
 */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40; // version 4
  b[8] = (b[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export interface ApiClientOptions {
  /** 每次请求时现取 token（而不是创建时传死），这样登录/登出后同一个 client 对象依然可用 */
  getToken: () => string | null;
  /** 任何需要登录的接口返回 401 时调用（token 过期或被篡改）→ 登出 */
  onUnauthorized: () => void;
}

interface RequestOptions {
  method?: 'GET' | 'POST';
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** 默认 true：带上 Bearer token，并在 401 时触发登出 */
  auth?: boolean;
}

export function createApiClient({ getToken, onUnauthorized }: ApiClientOptions) {
  async function request<T>(path: string, opts: RequestOptions = {}): Promise<{ data: T; status: number }> {
    const auth = opts.auth ?? true;
    const headers: Record<string, string> = { Accept: 'application/json', ...opts.headers };
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    if (auth) {
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
    }

    let res: Response;
    try {
      // 路径都是相对的（/api/...）：生产环境由同源的 Nginx 反代，开发环境由 Vite 代理，前端无需知道后端地址
      res = await fetch(path, {
        method: opts.method ?? 'GET',
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        signal: opts.signal,
      });
    } catch (err) {
      if (isAbortError(err)) throw err;
      throw new ApiError(0, err instanceof Error ? err.message : 'network error');
    }

    const text = await res.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        // 比如 Nginx 在后端全挂时返回的 502 HTML 页面
        body = null;
      }
    }

    if (!res.ok) {
      if (res.status === 401 && auth) onUnauthorized();
      throw new ApiError(res.status, extractDetail(body, res.status));
    }
    return { data: body as T, status: res.status };
  }

  const enc = encodeURIComponent;

  return {
    async login(username: string, password: string): Promise<LoginResponse> {
      // 登录接口的 401 表示“用户名或密码错误”，不是“token 失效”，所以 auth: false，不触发登出
      const { data } = await request<LoginResponse>('/api/auth/login', {
        method: 'POST',
        body: { username, password },
        auth: false,
      });
      return data;
    },

    async me(signal?: AbortSignal): Promise<Me> {
      return (await request<Me>('/api/me', { signal })).data;
    },

    /**
     * 换一张看板 WebSocket 的一次性票据。JWT 只在这个请求的 Authorization 头里出现，从不进 URL；
     * 票据只能用一次、30 秒后作废，所以 /ws/dashboard?ticket=... 即使被写进日志也无害。
     * 401 和其他接口一样会触发登出（token 失效了，重连也没用）。
     */
    async wsTicket(signal?: AbortSignal): Promise<WsTicket> {
      return (await request<WsTicket>('/api/ws-ticket', { method: 'POST', signal })).data;
    },

    async robots(signal?: AbortSignal): Promise<RobotOut[]> {
      return (await request<RobotOut[]>('/api/robots', { signal })).data;
    },

    async telemetry(robotId: string, seconds = 120, signal?: AbortSignal): Promise<TelemetryOut[]> {
      return (await request<TelemetryOut[]>(`/api/robots/${enc(robotId)}/telemetry?seconds=${seconds}`, { signal }))
        .data;
    },

    async logs(robotId: string, limit = 100, signal?: AbortSignal): Promise<LogOut[]> {
      return (await request<LogOut[]>(`/api/robots/${enc(robotId)}/logs?limit=${limit}`, { signal })).data;
    },

    async commands(robotId: string, limit = 50, signal?: AbortSignal): Promise<CommandOut[]> {
      return (await request<CommandOut[]>(`/api/commands?robot_id=${enc(robotId)}&limit=${limit}`, { signal })).data;
    },

    async command(commandId: string, signal?: AbortSignal): Promise<CommandDetail> {
      return (await request<CommandDetail>(`/api/commands/${enc(commandId)}`, { signal })).data;
    },

    /**
     * 下发命令。幂等键的用法是理解这个函数的关键：
     *
     * - 一次“点击”= 一个操作意图 = 一个新的 Idempotency-Key（由调用方生成后传进来）。
     * - 如果请求发出去了但响应丢了（网络抖动、Nginx 返回 502/504），我们并不知道服务端到底建没建这条命令。
     *   这时用“同一个 key”重试是安全的：服务端靠 (robot_id, idempotency_key) 唯一约束去重，
     *   已经建过就直接返回原来那条（HTTP 200），而不是再建一条——机器人不会被重复指挥。
     * - 反过来，如果每次重试都换新 key，重试就可能变成“发了两条 move_to”。
     *
     * Nginx 默认不会替我们重试 POST（POST 不是幂等方法，见 nginx.conf），所以重试放在客户端做，并且必须带同一个 key。
     * 返回值里的 created 区分 202（新建）和 200（命中已有命令）。
     */
    async createCommand(
      robotId: string,
      body: CommandIn,
      idempotencyKey: string,
      maxRetries = 2,
    ): Promise<{ command: CommandOut; created: boolean }> {
      for (let attempt = 0; ; attempt++) {
        try {
          const { data, status } = await request<CommandOut>(`/api/robots/${enc(robotId)}/commands`, {
            method: 'POST',
            body,
            headers: { 'Idempotency-Key': idempotencyKey },
          });
          return { command: data, created: status === 202 };
        } catch (err) {
          // 只对“结果未知”的失败重试：网络错误（status 0）和网关错误（502/504）。
          // 422/403/404/503 是服务端给出的明确答复，重试也不会变，直接抛给界面。
          const unknownOutcome = err instanceof ApiError && [0, 502, 504].includes(err.status);
          if (!unknownOutcome || attempt >= maxRetries) throw err;
          await sleep(400 * (attempt + 1));
        }
      }
    },
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
