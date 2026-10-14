import { useMemo } from 'react';
import { EChart, echarts, type ChartOption } from './EChart';
import { WINDOW_MS, type Sample } from '../lib/fleetStore';
import { formatClock } from '../lib/format';
import { CHART_AXIS, CHART_FONT, CHART_GRIDLINE, CHART_LABEL, withAlpha } from '../lib/chartTheme';

/** 相邻两个样本间隔超过这个值，就把折线断开（机器人掉线期间不应该画一条“假”的直线连过去） */
const GAP_MS = 3_000;

export type LineField = 'battery' | 'speed';

interface AxisRange {
  min: number;
  max: number;
  interval: number;
}

/**
 * 纵轴范围由我们自己算，而不是交给 ECharts 自动选：自动模式下上下界和刻度间隔不一定对齐，
 * 会出现 70 / 72 / 75 / 78 / 80 这种参差不齐的刻度。这里保证 min、max 都是 interval 的整数倍。
 */
function yAxisRange(field: LineField, samples: Sample[]): AxisRange {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const s of samples) {
    const v = s[field];
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }

  if (field === 'speed') {
    // 速度从 0 开始，上界留一点余量，再向上取整到刻度间隔
    const top = Number.isFinite(hi) ? Math.max(0.5, hi * 1.15) : 1;
    const interval = [0.1, 0.2, 0.25, 0.5, 1].find((s) => top / s <= 5) ?? 1;
    return { min: 0, max: Math.ceil(top / interval - 1e-9) * interval, interval };
  }

  // 电量：在数据附近自动缩放（固定 0~100 的话，2 分钟内几个百分点的变化几乎是一条平线），
  // 但跨度至少 10 个百分点，避免把传感器的微小抖动放大成“剧烈波动”
  if (!Number.isFinite(lo)) return { min: 0, max: 100, interval: 20 };
  const span = Math.max(10, hi - lo);
  const interval = span <= 12 ? 2 : span <= 30 ? 5 : span <= 60 ? 10 : 20;
  let min = Math.max(0, Math.floor((lo - 0.3) / interval) * interval);
  let max = Math.min(100, Math.ceil((hi + 0.3) / interval) * interval);
  // 跨度不够 10 时上下交替各扩一格（在 0~100 之内），让曲线大致居中
  for (let i = 0; max - min < 10 && i < 20; i++) {
    if (i % 2 === 0 && max < 100) max += interval;
    else if (min > 0) min -= interval;
    else max = Math.min(100, max + interval);
  }
  return { min, max, interval };
}

const formatTick: Record<LineField, (v: number) => string> = {
  battery: (v) => String(Math.round(v)),
  speed: (v) => v.toFixed(1),
};

interface LineChartProps {
  samples: Sample[];
  windowEnd: number;
  field: LineField;
  color: string;
  unit: string;
  digits: number;
  emptyText: string | null;
  ariaLabel: string;
}

/** 构造 [时间, 数值] 序列；两点间隔过大时插入一个 null，让 ECharts 在这里断线 */
function toSeriesData(samples: Sample[], field: LineField): Array<[number, number | null]> {
  const out: Array<[number, number | null]> = [];
  let prevT = Number.NEGATIVE_INFINITY;
  for (const s of samples) {
    if (out.length > 0 && s.t - prevT > GAP_MS) out.push([prevT + 1, null]);
    out.push([s.t, s[field]]);
    prevT = s.t;
  }
  return out;
}

export function LineChart({ samples, windowEnd, field, color, unit, digits, emptyText, ariaLabel }: LineChartProps) {
  const option = useMemo<ChartOption>(() => {
    const range = yAxisRange(field, samples);
    return {
      // 流式数据每 250 ms 刷新一次，开着动画会让折线不停地“变形过渡”，既晃眼又费 CPU
      animation: false,
      textStyle: { fontFamily: CHART_FONT },
      grid: { left: 44, right: 16, top: 12, bottom: 26 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: CHART_AXIS } },
        formatter: (params: unknown) => {
          const first = (Array.isArray(params) ? params[0] : params) as { value?: [number, number | null] } | undefined;
          const value = first?.value;
          if (!value || value[1] === null) return '';
          return `${formatClock(value[0])}<br/><b>${value[1].toFixed(digits)}</b> ${unit}`;
        },
      },
      xAxis: {
        type: 'time',
        min: windowEnd - WINDOW_MS,
        max: windowEnd,
        splitNumber: 6,
        axisLine: { lineStyle: { color: CHART_AXIS } },
        axisTick: { show: false },
        axisLabel: { color: CHART_LABEL, formatter: '{HH}:{mm}:{ss}', hideOverlap: true },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        min: range.min,
        max: range.max,
        interval: range.interval,
        axisLabel: { color: CHART_LABEL, formatter: formatTick[field] },
        splitLine: { lineStyle: { color: CHART_GRIDLINE } },
      },
      series: [
        {
          id: field,
          type: 'line',
          data: toSeriesData(samples, field),
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 2, color },
          itemStyle: { color },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: withAlpha(color, 0.18) },
              { offset: 1, color: withAlpha(color, 0.01) },
            ]),
          },
        },
      ],
    };
  }, [samples, windowEnd, field, color, unit, digits]);

  return (
    <div className="chart-box">
      <EChart option={option} className="chart" ariaLabel={ariaLabel} />
      {emptyText && <div className="chart-empty">{emptyText}</div>}
    </div>
  );
}
