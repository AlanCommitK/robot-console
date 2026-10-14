import { memo, type KeyboardEvent, type ReactNode } from 'react';
import { StatusBadge } from './StatusBadge';
import { COMMAND_LABEL, describeParams, formatClock, formatDateTime, formatDuration, isTerminal, toEpochMs } from '../lib/format';
import type { LoadStatus } from '../pages/dashboardState';
import type { CommandOut } from '../types';

interface CommandListProps {
  commands: CommandOut[];
  status: LoadStatus;
  error: string | null;
  selectedId: string | null;
  canCommand: boolean;
  onSelect: (commandId: string) => void;
}

function elapsed(c: CommandOut): string {
  if (!isTerminal(c.status) || !c.finished_at) return '—';
  return formatDuration(toEpochMs(c.finished_at) - toEpochMs(c.created_at));
}

export const CommandList = memo(function CommandList({ commands, status, error, selectedId, canCommand, onSelect }: CommandListProps) {
  const onKey = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(id);
    }
  };

  let body: ReactNode;
  if (status === 'loading' && commands.length === 0) {
    body = <div className="state">正在加载命令…</div>;
  } else if (status === 'error' && commands.length === 0) {
    body = <div className="state state--error">命令列表加载失败：{error}</div>;
  } else if (commands.length === 0) {
    body = (
      <div className="state">
        <p>这台机器人还没有命令记录</p>
        {canCommand && <p className="muted">在左侧面板下发一条命令，状态会在这里实时更新</p>}
      </div>
    );
  } else {
    body = (
      <div className="table-scroll table-scroll--y">
        <table className="table command-table">
          <thead>
            <tr>
              <th>创建时间</th>
              <th>命令</th>
              <th>状态</th>
              <th className="t-right">尝试</th>
              <th className="t-right">耗时</th>
            </tr>
          </thead>
          <tbody>
            {commands.map((c) => (
              <tr
                key={c.id}
                className={`clickable${c.id === selectedId ? ' is-selected' : ''}`}
                onClick={() => onSelect(c.id)}
                onKeyDown={(e) => onKey(e, c.id)}
                tabIndex={0}
                aria-current={c.id === selectedId ? 'true' : undefined}
                title={`${c.id}\n创建者：${c.created_by}`}
              >
                <td className="num" title={formatDateTime(toEpochMs(c.created_at))}>
                  {formatClock(toEpochMs(c.created_at))}
                </td>
                <td>
                  <span className="cmd-type">{COMMAND_LABEL[c.type] ?? c.type}</span>{' '}
                  <span className="cmd-params num">{describeParams(c.type, c.params)}</span>
                </td>
                <td>
                  <StatusBadge status={c.status} />
                </td>
                <td className="t-right num">{c.attempts}</td>
                <td className="t-right num">{elapsed(c)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <section className="card command-list">
      <header className="card-header">
        <h3 className="card-title">命令记录</h3>
        <span className="card-meta">点击一行查看事件时间线</span>
      </header>
      {body}
    </section>
  );
});
