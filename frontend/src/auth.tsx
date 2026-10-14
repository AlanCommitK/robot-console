import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createApiClient, type ApiClient } from './api';
import { toEpochMs } from './lib/format';
import type { Role } from './types';

export interface Session {
  token: string;
  username: string;
  role: Role;
  /** JWT 过期时间（epoch 毫秒） */
  expiresAt: number;
}

interface AuthContextValue {
  session: Session | null;
  api: ApiClient;
  login: (username: string, password: string) => Promise<void>;
  logout: (reason?: string) => void;
  /** 最近一次被动登出的原因（例如 token 过期），显示在登录页上 */
  notice: string | null;
}

const STORAGE_KEY = 'robot-console.session';

/*
 * token 同时放在内存和 localStorage：
 * - 内存（React state + ref）是运行时唯一的真相来源，读起来快，而且一定可用；
 * - localStorage 只是为了“刷新页面不用重新登录”。
 * localStorage 的读写都包在 try/catch 里：隐私模式、禁用了站点数据、存储配额满时，访问它会直接抛异常
 * （比如 SecurityError / QuotaExceededError）。存不进去只是“刷新后要重新登录”，不能让整个应用崩掉。
 *
 * 安全上的取舍：localStorage 里的 token 能被页面上的任何 JS 读到（XSS 风险）；更严格的做法是 httpOnly Cookie。
 * 本项目按 SPEC 使用 Bearer token（放在 Authorization 头里）；WebSocket 不能带请求头，就先用它换一张
 * 一次性票据再连接（见 ws.ts），JWT 本身从不出现在 URL 里。
 */
function loadSession(): Session | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Partial<Session>;
    if (typeof s.token !== 'string' || typeof s.username !== 'string' || typeof s.expiresAt !== 'number') return null;
    if (s.role !== 'admin' && s.role !== 'viewer') return null;
    if (s.expiresAt <= Date.now()) return null; // 已过期的不恢复
    return s as Session;
  } catch {
    return null;
  }
}

function saveSession(s: Session | null): void {
  try {
    if (s) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 存储不可用：忽略，只是刷新后需要重新登录
  }
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * 登录状态放在整个应用的最顶层（Context），因为几乎所有地方都要用：
 * 路由（显示登录页还是看板）、API 客户端（带 token、401 时登出）、顶部栏（用户名/角色）、命令面板（是否 admin）。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(loadSession);
  const [notice, setNotice] = useState<string | null>(null);
  // API 客户端在每次请求时通过 ref 读取最新 token，所以 client 对象只需创建一次
  const sessionRef = useRef<Session | null>(session);

  const applySession = useCallback((s: Session | null) => {
    sessionRef.current = s;
    saveSession(s);
    setSession(s);
  }, []);

  const logout = useCallback(
    (reason?: string) => {
      if (sessionRef.current === null) return; // 已经登出（多个并发请求同时 401 时只处理一次）
      applySession(null);
      setNotice(reason ?? null);
    },
    [applySession],
  );

  const api = useMemo(
    () =>
      createApiClient({
        getToken: () => sessionRef.current?.token ?? null,
        onUnauthorized: () => logout('登录已失效或已过期，请重新登录'),
      }),
    [logout],
  );

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await api.login(username, password);
      const expiresAt = toEpochMs(res.expires_at);
      applySession({
        token: res.access_token,
        username: res.username,
        role: res.role,
        // 万一解析失败，按后端默认的 480 分钟兜底
        expiresAt: Number.isFinite(expiresAt) ? expiresAt : Date.now() + 480 * 60_000,
      });
      setNotice(null);
    },
    [api, applySession],
  );

  // token 到期时自动登出（而不是等到下一次请求 401 才发现）
  useEffect(() => {
    if (!session) return;
    // setTimeout 的最大延迟约 24.8 天（2^31-1 ms），超过会被当成 0 立即触发，所以要截断
    const delay = Math.min(Math.max(0, session.expiresAt - Date.now()), 2 ** 31 - 1);
    const timer = window.setTimeout(() => logout('登录已过期，请重新登录'), delay);
    return () => window.clearTimeout(timer);
  }, [session, logout]);

  // 从 localStorage 恢复的会话，启动时用 /api/me 校验一次：
  // token 被后端判为无效（比如 JWT_SECRET 换了）时 api 客户端会收到 401 并自动登出；角色以服务端为准。
  useEffect(() => {
    const restored = sessionRef.current;
    if (!restored) return;
    const ctrl = new AbortController();
    api
      .me(ctrl.signal)
      .then((me) => {
        const cur = sessionRef.current;
        if (cur && cur.token === restored.token && (cur.role !== me.role || cur.username !== me.username)) {
          applySession({ ...cur, role: me.role, username: me.username });
        }
      })
      .catch(() => undefined); // 网络错误时保留会话（后端可能只是暂时不可用）；401 已由客户端处理
    return () => ctrl.abort();
    // 依赖数组故意留空：只在应用启动时校验一次
  }, []);

  // 多标签页同步：在另一个标签页登出（localStorage 被删除）时，这个标签页也跟着登出
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && e.newValue === null) logout('已在其他标签页退出登录');
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [logout]);

  const value = useMemo<AuthContextValue>(
    () => ({ session, api, login, logout, notice }),
    [session, api, login, logout, notice],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
