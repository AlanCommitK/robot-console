import { memo, useEffect, useState } from 'react';
import { LineChart } from './LineChart';
import { TrajectoryChart } from './TrajectoryChart';
import type { FleetStore, Sample } from '../lib/fleetStore';
import type { LoadStatus } from '../pages/dashboardState';
import { formatNumber } from '../lib/format';
import { COLOR_BATTERY, COLOR_SPEED } from '../lib/chartTheme';

/** 图表刷新节奏：4 Hz。比遥测（5 Hz × N 台）慢，但人眼看起来已经是“实时”的 */
const RENDER_INTERVAL_MS = 250;

interface TelemetryChartsProps {
  store: FleetStore;
  robotId: string;
  /** 历史数据回填（GET telemetry?seconds=120）的状态 */
  backfill: LoadStatus;
  target: { x: number; y: number } | null;
  /**
   * 卡片标题上显示的最新数值，取自和详情头部同一份快照（robot.latest），保证两处显示的数字完全一致；
   * 曲线本身用本组件自己的 4 Hz 快照，两者最多相差 250 ms，肉眼看不出来
   */
  latest: Sample | null;
}

/**
 * 三张实时图表。注意“高频状态”放在这里，而不是提升到 Dashboard：
 * 4 Hz 的快照 state 只会让这个组件和它的子图表重新渲染；如果放在 Dashboard，
 * 表格、命令列表、日志等整棵树都会被每秒 4 次地带着重算。
 * —— 状态应该“提升到需要它的最近公共祖先”，但也只提升到那里为止。
 */
export const TelemetryCharts = memo(function TelemetryCharts({
  store,
  robotId,
  backfill,
  target,
  latest,
}: TelemetryChartsProps) {
  const [snap, setSnap] = useState(() => store.series(robotId));

  useEffect(() => {
    setSnap(store.series(robotId));
    let lastVersion = store.getRobotVersion(robotId);
    let lastRender = Date.now();
    const timer = window.setInterval(() => {
      const version = store.getRobotVersion(robotId);
      const now = Date.now();
      // 有新数据就刷新；没有新数据也每秒刷新一次，让时间窗口继续往左滑（掉线机器人的曲线会逐渐移出窗口）
      if (version !== lastVersion || now - lastRender >= 1000) {
        lastVersion = version;
        lastRender = now;
        setSnap(store.series(robotId, now));
      }
    }, RENDER_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [store, robotId]);

  const emptyText =
    snap.samples.length > 0
      ? null
      : backfill === 'loading'
        ? '正在加载最近 120 秒的数据…'
        : backfill === 'error'
          ? '历史数据加载失败，等待实时数据…'
          : '最近 120 秒内没有遥测数据';

  return (
    <div className="charts-grid">
      <div className="charts-col">
        <section className="card chart-card">
          <header className="card-header">
            <h3 className="card-title">
              电量 <span className="card-title-unit">%</span>
            </h3>
            <span className="chart-value" style={{ color: COLOR_BATTERY }}>
              {formatNumber(latest?.battery, 1)}
              <small> %</small>
            </span>
          </header>
          <LineChart
            samples={snap.samples}
            windowEnd={snap.end}
            field="battery"
            color={COLOR_BATTERY}
            unit="%"
            digits={1}
            emptyText={emptyText}
            ariaLabel="最近 120 秒电量曲线"
          />
        </section>
        <section className="card chart-card">
          <header className="card-header">
            <h3 className="card-title">
              速度 <span className="card-title-unit">m/s</span>
            </h3>
            <span className="chart-value" style={{ color: COLOR_SPEED }}>
              {formatNumber(latest?.speed, 2)}
              <small> m/s</small>
            </span>
          </header>
          <LineChart
            samples={snap.samples}
            windowEnd={snap.end}
            field="speed"
            color={COLOR_SPEED}
            unit="m/s"
            digits={2}
            emptyText={emptyText}
            ariaLabel="最近 120 秒速度曲线"
          />
        </section>
      </div>
      <section className="card chart-card chart-card--trajectory">
        <header className="card-header">
          <h3 className="card-title">
            x-y 轨迹 <span className="card-title-unit">最近 120 秒</span>
          </h3>
          {latest && (
            <span className="chart-meta">
              当前 ({formatNumber(latest.x, 1)}, {formatNumber(latest.y, 1)}) m
            </span>
          )}
        </header>
        <TrajectoryChart samples={snap.samples} target={target} emptyText={emptyText} />
      </section>
    </div>
  );
});
