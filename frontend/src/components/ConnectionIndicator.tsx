import { useNow } from '../hooks/useNow';
import type { ConnectionState } from '../ws';

/** 顶部栏的实时连接指示灯：绿 = 已连接，琥珀 = 连接中/重连中，灰 = 未连接 */
export function ConnectionIndicator({ state }: { state: ConnectionState }) {
  const now = useNow(500);

  let tone: 'ok' | 'warn' | 'off';
  let text: string;
  switch (state.status) {
    case 'open':
      tone = 'ok';
      text = '实时连接正常';
      break;
    case 'connecting':
      tone = 'warn';
      text = state.attempt === 0 ? '正在连接…' : `正在重连（第 ${state.attempt} 次）…`;
      break;
    case 'reconnecting': {
      tone = 'warn';
      const seconds = Math.max(1, Math.ceil((state.retryAt - now) / 1000));
      text = `连接已断开 · ${seconds} 秒后重连`;
      break;
    }
    default:
      tone = 'off';
      text = '未连接';
  }

  return (
    <span className={`conn conn--${tone}`} role="status" aria-live="polite">
      <span className="conn-dot" aria-hidden="true" />
      {text}
    </span>
  );
}
