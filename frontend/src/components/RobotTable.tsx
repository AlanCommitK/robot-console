import type { KeyboardEvent } from 'react';
import { useNow } from '../hooks/useNow';
import { isLive, type RobotView } from '../lib/fleetStore';
import { formatDateTime, formatNumber, formatRelative, MODE_LABEL } from '../lib/format';
import type { LoadStatus } from '../pages/dashboardState';

interface RobotTableProps {
  robots: RobotView[];
  status: LoadStatus;
  error: string | null;
  selectedId: string | null;
  socketOpen: boolean;
  onSelect: (robotId: string) => void;
  onRetry: () => void;
}

function batteryTone(pct: number): 'good' | 'mid' | 'low' {
  if (pct >= 50) return 'good';
  if (pct >= 20) return 'mid';
  return 'low';
}

function BatteryCell({ value }: { value: number | undefined }) {
  if (value === undefined || !Number.isFinite(value)) return <span className="muted">—</span>;
  const pct = Math.max(0, Math.min(100, value));
  return (
    <span className="battery">
      <span className={`battery-bar battery-bar--${batteryTone(pct)}`}>
        <span style={{ width: `${pct}%` }} />
      </span>
      <span className="num">{pct.toFixed(0)}%</span>
    </span>
  );
}

export function RobotTable({ robots, status, error, selectedId, socketOpen, onSelect, onRetry }: RobotTableProps) {
  const now = useNow(1000);
  const onlineCount = robots.filter((r) => isLive(r, now, socketOpen)).length;

  const onKey = (e: KeyboardEvent<HTMLTableRowElement>, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(id);
    }
  };

  return (
    <section className="card">
      <header className="card-header">
        <h2 className="card-title">机器人列表</h2>
        {status === 'ready' && robots.length > 0 && (
          <span className="card-meta">
            在线 <b>{onlineCount}</b> / {robots.length}
          </span>
        )}
      </header>

      {status === 'error' && robots.length === 0 ? (
        <div className="state state--error">
          <p>机器人列表加载失败：{error}</p>
          <button type="button" className="btn btn--secondary btn--sm" onClick={onRetry}>
            重试
          </button>
        </div>
      ) : (
        <div className="table-scroll">
          <table className="table robot-table">
            <thead>
              <tr>
                <th>机器人</th>
                <th>状态</th>
                <th>接入副本</th>
                <th>电量</th>
                <th className="t-right">速度</th>
                <th>模式</th>
                <th>最后上报</th>
              </tr>
            </thead>
            <tbody>
              {status === 'loading' && robots.length === 0
                ? Array.from({ length: 4 }, (_, i) => (
                    <tr key={i} className="skeleton-row">
                      {Array.from({ length: 7 }, (__, j) => (
                        <td key={j}>
                          <span className="skeleton" />
                        </td>
                      ))}
                    </tr>
                  ))
                : robots.map((r) => {
                    const live = isLive(r, now, socketOpen);
                    const selected = r.id === selectedId;
                    const mode = r.latest?.mode;
                    return (
                      <tr
                        key={r.id}
                        className={`clickable${selected ? ' is-selected' : ''}${live ? '' : ' is-offline'}`}
                        onClick={() => onSelect(r.id)}
                        onKeyDown={(e) => onKey(e, r.id)}
                        tabIndex={0}
                        aria-current={selected ? 'true' : undefined}
                      >
                        <td>
                          <div className="robot-name">{r.name}</div>
                          <div className="robot-id mono">{r.id}</div>
                        </td>
                        <td>
                          <span className={`online ${live ? 'online--yes' : 'online--no'}`}>
                            <span className="online-dot" aria-hidden="true" />
                            {live ? '在线' : '离线'}
                          </span>
                        </td>
                        <td>
                          {r.replica ? (
                            <span className={`pill mono${live ? '' : ' pill--muted'}`} title={live ? '当前连接的 API 副本' : '最后一次连接的 API 副本'}>
                              {r.replica}
                            </span>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                        <td>
                          <BatteryCell value={r.latest?.battery} />
                        </td>
                        <td className="t-right num">
                          {r.latest ? (
                            <>
                              {formatNumber(r.latest.speed, 2)} <span className="unit">m/s</span>
                            </>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                        <td>
                          {mode ? (
                            <span className={`mode mode--${mode}`}>{MODE_LABEL[mode] ?? mode}</span>
                          ) : (
                            <span className="muted">—</span>
                          )}
                        </td>
                        <td title={r.lastSeenMs ? formatDateTime(r.lastSeenMs) : undefined} className="num">
                          {formatRelative(r.lastSeenMs, now)}
                        </td>
                      </tr>
                    );
                  })}
              {status !== 'loading' && robots.length === 0 && (
                <tr>
                  <td colSpan={7} className="state">
                    还没有注册的机器人
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
