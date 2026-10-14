import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart, ScatterChart, type LineSeriesOption, type ScatterSeriesOption } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  type GridComponentOption,
  type TooltipComponentOption,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

// 按需引入：只注册用到的图表类型和组件。完整的 echarts 包压缩后约 1 MB，按需引入能小一半以上。
echarts.use([LineChart, ScatterChart, GridComponent, TooltipComponent, CanvasRenderer]);

/** 只允许使用上面注册过的组件，用错了（比如写了 legend 却没注册）编译期就会报错 */
export type ChartOption = echarts.ComposeOption<
  LineSeriesOption | ScatterSeriesOption | GridComponentOption | TooltipComponentOption
>;

export { echarts };

interface EChartProps {
  option: ChartOption;
  className?: string;
  ariaLabel?: string;
}

/**
 * ECharts 的最小 React 封装：ECharts 是“命令式”的库（init → setOption → dispose），
 * React 是“声明式”的。这里用 useRef 拿到真实 DOM 节点，用 useEffect 在合适的时机调用命令式 API：
 *   - 挂载时 init，卸载时 dispose（否则图表实例和它注册的事件会泄漏）；
 *   - option 变化时 setOption；
 *   - 容器尺寸变化时 resize（ResizeObserver 比监听 window.resize 更准：侧栏折叠、网格换行也能感知）。
 */
export function EChart({ option, className, ariaLabel }: EChartProps) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    const chart = echarts.init(el, undefined, { renderer: 'canvas' });
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    // replaceMerge: ['series'] —— 按 series.id 整体替换系列（新 option 里没有的系列会被删掉），
    // 其余组件（坐标轴、提示框）走默认的合并，这样鼠标悬停时 tooltip 不会因为数据刷新而闪烁。
    // lazyUpdate：把重绘推迟到下一帧，同一帧内多次 setOption 只画一次。
    chartRef.current?.setOption(option, { replaceMerge: ['series'], lazyUpdate: true });
  }, [option]);

  return <div ref={elRef} className={className} role="img" aria-label={ariaLabel} />;
}
