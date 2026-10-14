import { useMemo } from 'react';
import { TelemetryCharts } from './TelemetryCharts';
import { CommandPanel } from './CommandPanel';
import { CommandList } from './CommandList';
import { CommandTimeline } from './CommandTimeline';
import { LogList } from './LogList';
import { isLive, type FleetStore, type RobotView } from '../lib/fleetStore';
import { formatNumber, formatRelative, headingDegrees, isTerminal, MODE_LABEL } from '../lib/format';
import { useNow } from '../hooks/useNow';
import type { DashboardState, LoadStatus } from '../pages/dashboardState';
import type { CommandOut } from '../types';

interface RobotDetailProps {
  robot: RobotView;
  /** 实时连接是否正常（用于判断机器人在线是否“过期”，见 isLive） */
  socketOpen: boolean;
  store: FleetStore;
  backfill: LoadStatus;
  data: DashboardState;
  isAdmin: boolean;
  onCommandCreated: (command: CommandOut) => void;
  onSelectCommand: (commandId: string) => void;
}

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value num">
        {value}
        {unit && <span className="stat-unit">{unit === '°' ? unit : ` ${unit}`}</span>}
      </div>
    </div>
  );
}

/** 选中机器人的详情区：头部概览 + 三张图表 + 命令面板/命令列表/时间线 + 日志 */
export function RobotDetail({
  robot,
  socketOpen,
  store,
  backfill,
  data,
  isAdmin,
  onCommandCreated,
  onSelectCommand,
}: RobotDetailProps) {
  const now = useNow(1000);
  const live = isLive(robot, now, socketOpen);
  const s = robot.latest;

  // 正在执行的 move_to 的目标点，画在轨迹图上（命令列表最新的在前，find 找到的就是最近一条）
  const target = useMemo(() => {
    const active = data.commands.find((c) => c.type === 'move_to' && !isTerminal(c.status));
    const x = active?.params.x;
    const y = active?.params.y;
    return typeof x === 'number' && typeof y === 'number' ? { x, y } : null;
  }, [data.commands]);

  const selectedCommand = data.commands.find((c) => c.id === data.selectedCommandId) ?? null;
  const timelineRefreshKey = selectedCommand
    ? `${selectedCommand.status}|${selectedCommand.attempts}|${selectedCommand.updated_at}`
    : '';

  return (
    <div className="detail">
      <section className="card detail-header">
        <div className="detail-title">
          <div>
            <h2>{robot.name}</h2>
            <div className="detail-sub">
              <span className="mono">{robot.id}</span>
              <span className={`online ${live ? 'online--yes' : 'online--no'}`}>
                <span className="online-dot" aria-hidden="true" />
                {live ? '在线' : '离线'}
              </span>
              {s?.mode && <span className={`mode mode--${s.mode}`}>{MODE_LABEL[s.mode] ?? s.mode}</span>}
              {robot.replica && (
                <span className="detail-replica">
                  经由 <span className="pill mono">{robot.replica}</span>
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="stats">
          <Stat label="电量" value={formatNumber(s?.battery, 1)} unit="%" />
          <Stat label="速度" value={formatNumber(s?.speed, 2)} unit="m/s" />
          <Stat label="位置 (x, y)" value={s ? `${formatNumber(s.x, 1)}, ${formatNumber(s.y, 1)}` : '—'} unit="m" />
          <Stat label="朝向" value={s ? String(headingDegrees(s.theta)) : '—'} unit="°" />
          <Stat label="最后上报" value={formatRelative(robot.lastSeenMs, now)} />
        </div>
      </section>

      <TelemetryCharts store={store} robotId={robot.id} backfill={backfill} target={target} latest={s} />

      <div className="ops-grid">
        <CommandPanel robotId={robot.id} robotOnline={live} canCommand={isAdmin} onCreated={onCommandCreated} />
        <CommandList
          commands={data.commands}
          status={data.commandsStatus}
          error={data.commandsError}
          selectedId={data.selectedCommandId}
          canCommand={isAdmin}
          onSelect={onSelectCommand}
        />
        <CommandTimeline
          key={data.selectedCommandId ?? 'none'}
          commandId={data.selectedCommandId}
          refreshKey={timelineRefreshKey}
        />
      </div>

      <LogList logs={data.logs} status={data.logsStatus} error={data.logsError} />
    </div>
  );
}
