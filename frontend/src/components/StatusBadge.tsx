import { isTerminal, STATUS_LABEL } from '../lib/format';
import type { CommandStatus } from '../types';

/**
 * 命令状态徽章。key={status} 让状态一变化 React 就重新创建这个 <span>，
 * 于是 CSS 里的入场动画会重新播放一次——状态跳变时徽章“闪一下”，实时感更强。
 * 未到终态的状态带一个呼吸的小圆点，表示“还在进行中”。
 */
export function StatusBadge({ status }: { status: CommandStatus }) {
  const known = status in STATUS_LABEL;
  return (
    <span key={status} className={`badge badge--${known ? status : 'unknown'}`} title={status}>
      {known && !isTerminal(status) && <span className="badge-pulse" aria-hidden="true" />}
      {known ? STATUS_LABEL[status] : status}
    </span>
  );
}
