import { useMemo, useRef } from 'react';
import { EChart, type ChartOption } from './EChart';
import { useElementSize } from '../hooks/useElementSize';
import type { Sample } from '../lib/fleetStore';
import { formatNumber, headingDegrees } from '../lib/format';
import {
  CHART_FONT,
  CHART_GRIDLINE,
  CHART_LABEL,
  COLOR_ACCENT,
  COLOR_ROBOT,
  COLOR_TARGET,
  withAlpha,
} from '../lib/chartTheme';

/** 轨迹末尾这一段用深色粗线画，前面的用浅色细线，一眼看出运动方向和新旧 */
const RECENT_MS = 15_000;
const GRID = { left: 40, right: 14, top: 14, bottom: 28 };

interface Point {
  x: number;
  y: number;
}

interface View {
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
}

/**
 * 计算坐标范围，保证“等比例”：x 方向 1 米和 y 方向 1 米在屏幕上一样长，否则圆形轨迹会被画成椭圆。
 * 做法是算出“每像素多少米”，两个方向取同一个值。
 * 这个比例和中心点都做了取整（量化），机器人每走一小步视图不会跟着抖动，只在需要时成档跳变。
 */
function computeView(points: Point[], width: number, height: number): View {
  let minX = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const p of points) {
    minX = Math.min(minX, p.x);
    maxX = Math.max(maxX, p.x);
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }
  if (!Number.isFinite(minX)) {
    minX = -5;
    maxX = 5;
    minY = -5;
    maxY = 5;
  }
  const plotW = Math.max(60, width - GRID.left - GRID.right);
  const plotH = Math.max(60, height - GRID.top - GRID.bottom);
  // 至少显示 10 m 见方，数据四周再留 20% 的边
  const needX = Math.max(10, (maxX - minX) * 1.4);
  const needY = Math.max(10, (maxY - minY) * 1.4);
  const raw = Math.max(needX / plotW, needY / plotH);
  // 每像素米数按 1.25 倍为一档向上取整
  const metersPerPx = 0.005 * 1.25 ** Math.ceil(Math.log(raw / 0.005) / Math.log(1.25));
  const halfW = (plotW * metersPerPx) / 2;
  const halfH = (plotH * metersPerPx) / 2;
  const quantum = Math.min(halfW, halfH) / 8;
  const cx = Math.round((minX + maxX) / 2 / quantum) * quantum;
  const cy = Math.round((minY + maxY) / 2 / quantum) * quantum;
  return { xMin: cx - halfW, xMax: cx + halfW, yMin: cy - halfH, yMax: cy + halfH };
}

const axisLabel = (v: number) => (Math.abs(v - Math.round(v)) < 1e-6 ? String(Math.round(v)) : v.toFixed(1));

interface TrajectoryChartProps {
  samples: Sample[];
  /** 正在执行的 move_to 目标点（没有则为 null） */
  target: Point | null;
  emptyText: string | null;
}

export function TrajectoryChart({ samples, target, emptyText }: TrajectoryChartProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const { width, height } = useElementSize(boxRef);

  const option = useMemo<ChartOption>(() => {
    const current = samples.length > 0 ? samples[samples.length - 1] : null;
    const viewPoints: Point[] = target ? [...samples, target] : samples;
    const view = computeView(viewPoints, width, height);
    const recentFrom = current ? current.t - RECENT_MS : 0;
    const recentStart = samples.findIndex((s) => s.t >= recentFrom);
    const trail = samples.map((s) => [s.x, s.y]);
    const recent = recentStart >= 0 ? trail.slice(recentStart) : [];

    const axisCommon = {
      type: 'value' as const,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: CHART_LABEL, formatter: axisLabel, showMinLabel: false, showMaxLabel: false },
      splitLine: { lineStyle: { color: CHART_GRIDLINE } },
    };

    return {
      animation: false,
      textStyle: { fontFamily: CHART_FONT },
      grid: GRID,
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const item = p as { seriesId?: string; value?: [number, number] };
          if (!item.value) return '';
          const [x, y] = item.value;
          const label = item.seriesId === 'target' ? '目标点' : item.seriesId === 'robot' ? '当前位置' : '轨迹点';
          return `${label}<br/>(${formatNumber(x, 2)}, ${formatNumber(y, 2)}) m`;
        },
      },
      xAxis: { ...axisCommon, min: view.xMin, max: view.xMax },
      yAxis: { ...axisCommon, min: view.yMin, max: view.yMax },
      series: [
        {
          id: 'trail',
          type: 'line',
          data: trail,
          showSymbol: false,
          silent: true,
          lineStyle: { width: 1.5, color: withAlpha(COLOR_ACCENT, 0.3) },
        },
        {
          id: 'recent',
          type: 'line',
          data: recent,
          showSymbol: false,
          silent: true,
          lineStyle: { width: 2.5, color: COLOR_ACCENT, cap: 'round', join: 'round' },
        },
        {
          id: 'target-path',
          type: 'line',
          data: current && target ? [[current.x, current.y], [target.x, target.y]] : [],
          showSymbol: false,
          silent: true,
          lineStyle: { width: 1.5, type: 'dashed', color: withAlpha(COLOR_TARGET, 0.7) },
        },
        {
          id: 'target',
          type: 'scatter',
          data: target ? [[target.x, target.y]] : [],
          symbol: 'circle',
          symbolSize: 12,
          itemStyle: { color: '#fff', borderColor: COLOR_TARGET, borderWidth: 3 },
          label: { show: true, position: 'top', formatter: '目标', color: COLOR_TARGET, fontSize: 11, fontWeight: 600 },
        },
        {
          id: 'robot',
          type: 'scatter',
          data: current ? [[current.x, current.y]] : [],
          // 'arrow' 符号默认朝上（+y）；theta 按数学约定 0 = 朝 +x、逆时针为正，ECharts 的 symbolRotate 也是逆时针为正，
          // 所以要先减去 90°
          symbol: 'arrow',
          symbolSize: [16, 21],
          symbolRotate: current ? headingDegrees(current.theta) - 90 : 0,
          itemStyle: { color: COLOR_ROBOT, borderColor: '#fff', borderWidth: 1.5 },
          z: 10,
        },
      ],
    };
  }, [samples, target, width, height]);

  return (
    <div ref={boxRef} className="chart-box chart-box--trajectory">
      <EChart option={option} className="chart" ariaLabel="机器人 x-y 轨迹图" />
      {emptyText && <div className="chart-empty">{emptyText}</div>}
    </div>
  );
}
