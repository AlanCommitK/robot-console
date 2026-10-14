// 图表的颜色与字体。界面样式用 CSS 变量（styles.css），但 ECharts 画在 <canvas> 上，读不到 CSS 变量和页面字体，
// 所以这里单独维护一份，取值与 styles.css 保持一致。

export const COLOR_ACCENT = '#2f5bea';
export const COLOR_BATTERY = '#13a07a';
export const COLOR_SPEED = '#2f5bea';
export const COLOR_TARGET = '#e8590c';
export const COLOR_ROBOT = '#1e3a8a';

export const CHART_AXIS = '#cfd6e1';
export const CHART_GRIDLINE = '#eef1f6';
export const CHART_LABEL = '#8592a6';

/** 与 styles.css 的 --font 相同：先列各平台的西文字体，再列中文字体（浏览器按“字”逐个回退） */
export const CHART_FONT =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif";

/** '#2f5bea' + 0.2 → 'rgba(47, 91, 234, 0.2)' */
export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3 ? h.replace(/(.)/g, '$1$1') : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}
