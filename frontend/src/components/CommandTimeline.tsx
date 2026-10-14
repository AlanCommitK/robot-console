import { memo, useEffect, useState } from 'react';
import { useAuth } from '../auth';
import { describeError, isAbortError } from '../api';
import { StatusBadge } from './StatusBadge';
import {
  COMMAND_LABEL,
  describeParams,
  formatClock,
  formatDateTime,
  formatDuration,
  STATUS_LABEL,
  toEpochMs,
} from '../lib/format';
import type { CommandDetail, CommandStatus } from '../types';

interface CommandTimelineProps {
  commandId: string | null;
  /**
   * 列表里这条命令的“版本”（状态 + 尝试次数 + 更新时间）。它一变化（收到 WS 的 command_update），
   * 就重新拉一次 GET /api/commands/{id}，时间线跟着实时长出新的一行。
   */
  refreshKey: string;
}

type ViewStatus = 'idle' | 'loading' | 'ready' | 'error';

/**
 * 父组件用 key={commandId} 渲染本组件：切换到另一条命令时整个组件重新挂载，state 自动回到初始值，
 * 不用手写“如果 id 变了就清空”的逻辑。同一条命令的后续刷新则保留旧内容，避免闪烁。
 */
export const CommandTimeline = memo(function CommandTimeline({ commandId, refreshKey }: CommandTimelineProps) {
  const { api } = useAuth();
  const [detail, setDetail] = useState<CommandDetail | null>(null);
  const [status, setStatus] = useState<ViewStatus>(commandId ? 'loading' : 'idle');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!commandId) return;
    const ctrl = new AbortController();
    api
      .command(commandId, ctrl.signal)
      .then((d) => {
        setDetail(d);
        setStatus('ready');
        setError(null);
      })
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        setError(describeError(err));
        setStatus((s) => (s === 'ready' ? 'ready' : 'error'));
      });
    // 状态连续快速变化时，上一个还没回来的请求直接取消，只保留最新的那次
    return () => ctrl.abort();
  }, [api, commandId, refreshKey]);

  // 保持服务端返回的顺序（按事件自增 id 排序 = 行锁串行化后的真实先后），不要按时间戳重排：
  // 事件可能由不同副本写入，副本之间时钟有偏差时，按 at 排序会把因果顺序打乱（见 commands_service.py）。
  const events = detail ? detail.events : [];
  const t0 = events.length > 0 ? toEpochMs(events[0].at) : detail ? toEpochMs(detail.created_at) : 0;
  const ackLatency =
    detail?.dispatched_at && detail.acked_at ? toEpochMs(detail.acked_at) - toEpochMs(detail.dispatched_at) : null;
  const total = detail?.finished_at ? toEpochMs(detail.finished_at) - toEpochMs(detail.created_at) : null;

  return (
    <section className="card timeline-card">
      <header className="card-header">
        <h3 className="card-title">事件时间线</h3>
        {detail && <StatusBadge status={detail.status} />}
      </header>

      {status === 'idle' && (
        <div className="state">
          <p>点击左侧任意一条命令</p>
          <p className="muted">查看它在状态机里的每一次流转：由哪个副本处理、何时下发、何时确认</p>
        </div>
      )}
      {status === 'loading' && <div className="state">正在加载事件…</div>}
      {status === 'error' && <div className="state state--error">加载失败：{error}</div>}

      {status === 'ready' && detail && (
        <div className="card-body timeline-body">
          <dl className="kv">
            <div>
              <dt>命令</dt>
              <dd>
                <b>{COMMAND_LABEL[detail.type] ?? detail.type}</b> {describeParams(detail.type, detail.params)}{' '}
                <code>{detail.type}</code>
              </dd>
            </div>
            <div>
              <dt>命令 ID</dt>
              <dd className="mono small">{detail.id}</dd>
            </div>
            {detail.error && (
              <div>
                <dt>错误</dt>
                <dd className="text-danger">{detail.error}</dd>
              </div>
            )}
          </dl>

          <div className="metrics">
            <div>
              <span>创建者</span>
              <b>{detail.created_by}</b>
            </div>
            <div>
              <span>投递次数</span>
              <b className="num">{detail.attempts}</b>
            </div>
            <div title="dispatched_at → acked_at">
              <span>下发 → 确认</span>
              <b className="num">{ackLatency !== null ? formatDuration(ackLatency) : '—'}</b>
            </div>
            <div title="created_at → finished_at">
              <span>总耗时</span>
              <b className="num">{total !== null ? formatDuration(total) : '—'}</b>
            </div>
          </div>

          {events.length === 0 ? (
            <div className="state">暂无事件</div>
          ) : (
            <ol className="timeline">
              {events.map((ev, i) => {
                const at = toEpochMs(ev.at);
                const to = ev.to_status as CommandStatus;
                return (
                  <li key={`${ev.at}-${i}`} className={`tl-item tl-item--${to}`}>
                    <span className="tl-dot" aria-hidden="true" />
                    <div className="tl-main">
                      <div className="tl-title">
                        <b>{STATUS_LABEL[to] ?? to}</b>
                        <code className="tl-transition">
                          {ev.from_status ?? '∅'} → {ev.to_status}
                        </code>
                      </div>
                      <div className="tl-meta">
                        <span className="num" title={formatDateTime(at)}>
                          {formatClock(at, true)}
                        </span>
                        <span className="num muted">+{formatDuration(at - t0)}</span>
                        {ev.replica && <span className="pill mono">{ev.replica}</span>}
                      </div>
                      {ev.detail && <div className="tl-detail">{ev.detail}</div>}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </section>
  );
});
