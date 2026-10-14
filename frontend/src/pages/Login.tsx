import { useState, type FormEvent } from 'react';
import { useAuth } from '../auth';
import { ApiError, describeError } from '../api';
import { Logo } from '../components/Logo';

/** 种子数据里的演示账号（SPEC §5），仅用于本地开发/演示环境 */
const DEMO_ACCOUNTS = [
  { username: 'admin', password: 'admin123', role: '管理员' },
  { username: 'viewer', password: 'viewer123', role: '只读' },
];

export function Login() {
  const { login, notice } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) {
      setError('请输入用户名和密码');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await login(username.trim(), password);
      // 成功后 AuthProvider 的 session 变化，App 会切换到看板，本组件随之卸载，所以这里不再 setState
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? '用户名或密码错误' : describeError(err));
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <aside className="login-brand">
          <Logo size={44} inverted />
          <h1>机器人集群控制台</h1>
          <p className="login-tagline">Robot Console · 实时监控与命令下发</p>
          <ul className="login-features">
            <li>
              <b>实时遥测</b>
              <span>WebSocket 推送电量、速度与轨迹，断线自动重连</span>
            </li>
            <li>
              <b>可靠命令</b>
              <span>RabbitMQ 至少一次投递 + 幂等键 + 服务端状态机</span>
            </li>
            <li>
              <b>多副本部署</b>
              <span>Nginx 在两个 API 副本间负载均衡，Redis 广播事件</span>
            </li>
          </ul>
        </aside>

        <form className="login-form" onSubmit={onSubmit} noValidate>
          <h2>登录</h2>
          <p className="muted">请输入控制台账号</p>

          {notice && <div className="hint hint--warn">{notice}</div>}

          <label className="field">
            <span>用户名</span>
            <input
              name="username"
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="请输入用户名"
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请输入密码"
            />
          </label>

          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}

          <button type="submit" className="btn btn--primary btn--block btn--lg" disabled={submitting}>
            {submitting ? '登录中…' : '登录'}
          </button>

          <div className="login-demo">
            <div className="login-demo-title">演示账号（点击填入）</div>
            <div className="login-demo-list">
              {DEMO_ACCOUNTS.map((a) => (
                <button
                  key={a.username}
                  type="button"
                  className="demo-account"
                  onClick={() => {
                    setUsername(a.username);
                    setPassword(a.password);
                    setError(null);
                  }}
                >
                  <span className="mono">
                    {a.username} / {a.password}
                  </span>
                  <span className="tag tag--muted">{a.role}</span>
                </button>
              ))}
            </div>
          </div>
        </form>
      </div>
      <footer className="login-footer">React · TypeScript · ECharts · FastAPI · RabbitMQ · Redis · PostgreSQL · Nginx</footer>
    </div>
  );
}
