import { memo } from 'react';
import { formatClock, formatDateTime, LEVEL_LABEL, toEpochMs } from '../lib/format';
import type { LoadStatus } from '../pages/dashboardState';
import type { LogOut } from '../types';

interface LogListProps {
  logs: LogOut[];
  status: LoadStatus;
  error: string | null;
}

/** 机器人日志：先用 GET /logs 回填最近 100 条，之后 WS 的 log 事件实时插到最前面 */
export const LogList = memo(function LogList({ logs, status, error }: LogListProps) {
  return (
    <section className="card log-card">
      <header className="card-header">
        <h3 className="card-title">最近日志</h3>
        {logs.length > 0 && <span className="card-meta">{logs.length} 条 · 最新在前</span>}
      </header>
      {status === 'loading' && logs.length === 0 ? (
        <div className="state">正在加载日志…</div>
      ) : status === 'error' && logs.length === 0 ? (
        <div className="state state--error">日志加载失败：{error}</div>
      ) : logs.length === 0 ? (
        <div className="state">暂无日志</div>
      ) : (
        <ul className="log-list">
          {logs.map((l) => {
            const t = toEpochMs(l.ts);
            // 合并时已按 (时间, 级别, 内容) 去重，所以它可以当稳定的 key；不要用数组下标——
            // 新日志插在最前面时下标全部错位，React 会以为每一行都变了
            return (
              <li key={`${l.ts}|${l.level}|${l.msg}`} className={`log log--${l.level}`}>
                <span className="log-time num" title={formatDateTime(t)}>
                  {formatClock(t)}
                </span>
                <span className={`level level--${l.level}`}>{LEVEL_LABEL[l.level] ?? l.level}</span>
                <span className="log-msg">{l.msg}</span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
});
