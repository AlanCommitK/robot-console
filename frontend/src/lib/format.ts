import type { CommandStatus, CommandType, LogLevel, RobotMode, Role } from '../types';

/** 不带时区的 iso8601 日期时间，例如 "2026-10-13T07:12:00.123" */
const ISO_WITHOUT_ZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

/**
 * 把各种时间表示统一换算成 epoch 毫秒。
 * SPEC 里 REST 用 iso8601 字符串，看板 WebSocket 的遥测用 epoch 秒（浮点）。这里两种都接受；
 * 数字小于 1e11 视为“秒”（现在的 epoch 秒约 1.8e9，epoch 毫秒约 1.8e12）。无法解析时返回 NaN。
 *
 * 字符串先做两步规整再交给 Date.parse：
 * - Python 输出的是 6 位小数（微秒），标准只规定了 3 位（毫秒），部分浏览器（老版本 Safari）不认，截成 3 位；
 * - 没有时区后缀的时间按 UTC 处理。否则 Date.parse 会把它当成浏览器本地时间，在北京（UTC+8）就差了 8 小时。
 */
export function toEpochMs(ts: number | string | null | undefined): number {
  if (typeof ts === 'number') return ts < 1e11 ? ts * 1000 : ts;
  if (typeof ts !== 'string' || !ts) return Number.NaN;
  let s = ts.trim().replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
  if (ISO_WITHOUT_ZONE.test(s)) s += 'Z';
  return Date.parse(s);
}

const pad = (n: number, width = 2) => String(n).padStart(width, '0');

/** HH:mm:ss（本地时区），withMs 时带毫秒 */
export function formatClock(ms: number, withMs = false): string {
  if (!Number.isFinite(ms)) return '—';
  const d = new Date(ms);
  const base = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return withMs ? `${base}.${pad(d.getMilliseconds(), 3)}` : base;
}

/** 完整的本地日期时间，用在 title 提示里 */
export function formatDateTime(ms: number): string {
  if (!Number.isFinite(ms)) return '—';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${formatClock(ms, true)}`;
}

/** “刚刚 / 12 秒前 / 3 分钟前 / 2 小时前” */
export function formatRelative(ms: number | null, now: number): string {
  if (ms === null || !Number.isFinite(ms)) return '—';
  const sec = Math.max(0, Math.round((now - ms) / 1000));
  if (sec < 3) return '刚刚';
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** 两个时间点之间的耗时：“850 ms / 2.4 s / 1 分 05 秒” */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const sec = Math.round(ms / 1000);
  return `${Math.floor(sec / 60)} 分 ${pad(sec % 60)} 秒`;
}

export function formatNumber(v: number | null | undefined, digits: number): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—';
}

/** 弧度 → 0~359 度（数学约定：0° 指向 +x，逆时针为正） */
export function headingDegrees(theta: number): number {
  const deg = (theta * 180) / Math.PI;
  return ((Math.round(deg) % 360) + 360) % 360;
}

export const ROLE_LABEL: Record<Role, string> = {
  admin: '管理员',
  viewer: '只读',
};

export const MODE_LABEL: Record<RobotMode, string> = {
  idle: '空闲',
  moving: '移动中',
  charging: '充电中',
  error: '故障',
};

export const STATUS_LABEL: Record<CommandStatus, string> = {
  queued: '排队中',
  dispatched: '已下发',
  acked: '已确认',
  succeeded: '成功',
  failed: '失败',
  expired: '已过期',
};

export const LEVEL_LABEL: Record<LogLevel, string> = {
  info: 'INFO',
  warn: 'WARN',
  error: 'ERROR',
};

export const COMMAND_LABEL: Record<CommandType, string> = {
  move_to: '移动',
  set_speed: '设速',
  stop: '停止',
};

const TERMINAL: ReadonlySet<CommandStatus> = new Set(['succeeded', 'failed', 'expired']);
export const isTerminal = (s: CommandStatus) => TERMINAL.has(s);

/** 命令参数的简短描述，例如 “(12.0, -8.0)”、“0.8 m/s” */
export function describeParams(type: CommandType, params: Record<string, unknown>): string {
  const num = (k: string) => (typeof params[k] === 'number' ? (params[k] as number) : Number.NaN);
  switch (type) {
    case 'move_to':
      return `(${formatNumber(num('x'), 1)}, ${formatNumber(num('y'), 1)})`;
    case 'set_speed':
      return `${formatNumber(num('speed'), 2)} m/s`;
    default:
      return '';
  }
}
