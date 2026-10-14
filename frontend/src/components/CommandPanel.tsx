import { memo, useState, type FormEvent } from 'react';
import { useAuth } from '../auth';
import { describeError, newIdempotencyKey } from '../api';
import { COMMAND_LABEL } from '../lib/format';
import type { CommandIn, CommandOut, CommandType } from '../types';

interface CommandPanelProps {
  robotId: string;
  robotOnline: boolean;
  /** 只有 admin 能下发命令；viewer 看到的是禁用状态 + 提示 */
  canCommand: boolean;
  onCreated: (command: CommandOut) => void;
}

interface Feedback {
  kind: 'ok' | 'error';
  text: string;
  idempotencyKey: string;
}

/** 解析数字输入；空串或非数字返回 NaN */
const parseNum = (s: string) => (s.trim() === '' ? Number.NaN : Number(s));
const inRange = (v: number, lo: number, hi: number) => Number.isFinite(v) && v >= lo && v <= hi;

/**
 * 表单输入值是“只有这个面板关心”的局部状态，所以就放在面板自己里面，不往上提。
 * 下发成功后通过 onCreated 把服务端返回的命令交给上层（命令列表和时间线需要它）。
 */
export const CommandPanel = memo(function CommandPanel({ robotId, robotOnline, canCommand, onCreated }: CommandPanelProps) {
  const { api } = useAuth();
  const [x, setX] = useState('12');
  const [y, setY] = useState('-6');
  const [speed, setSpeed] = useState('0.8');
  const [pending, setPending] = useState<CommandType | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  const xNum = parseNum(x);
  const yNum = parseNum(y);
  const speedNum = parseNum(speed);
  const moveValid = inRange(xNum, -50, 50) && inRange(yNum, -50, 50);
  const speedValid = inRange(speedNum, 0, 2);
  const disabled = !canCommand || pending !== null;

  async function send(body: CommandIn) {
    // 关键点：每一次点击都生成一个新的幂等键。
    // “点击”代表一个新的操作意图，必须是一条新命令；而这一次点击内部的自动重试（见 api.createCommand）
    // 复用同一个键，服务端据此去重，保证网络抖动时也不会把同一个意图执行两遍。
    const idempotencyKey = newIdempotencyKey();
    setPending(body.type);
    setFeedback(null);
    try {
      const { command, created } = await api.createCommand(robotId, body, idempotencyKey);
      onCreated(command);
      setFeedback({
        kind: 'ok',
        text: created
          ? `已提交「${COMMAND_LABEL[body.type]}」，当前状态 ${command.status}`
          : '服务端已存在相同幂等键的命令，返回了原命令（未重复创建）',
        idempotencyKey,
      });
    } catch (err) {
      setFeedback({ kind: 'error', text: describeError(err), idempotencyKey });
    } finally {
      setPending(null);
    }
  }

  const onMove = (e: FormEvent) => {
    e.preventDefault();
    if (moveValid) void send({ type: 'move_to', params: { x: xNum, y: yNum } });
  };
  const onSpeed = (e: FormEvent) => {
    e.preventDefault();
    if (speedValid) void send({ type: 'set_speed', params: { speed: speedNum } });
  };

  return (
    <section className="card command-panel">
      <header className="card-header">
        <h3 className="card-title">下发命令</h3>
        {!canCommand && <span className="tag tag--muted">只读</span>}
      </header>

      <div className="card-body stack">
        {!canCommand && (
          <p className="hint hint--info">当前账号为只读角色（viewer），不能下发命令。请使用管理员账号登录后操作。</p>
        )}
        {canCommand && !robotOnline && (
          <p className="hint hint--warn">机器人当前离线：命令会在队列中等待，30 秒内未上线将变为“已过期”。</p>
        )}

        <form className="cmd-form" onSubmit={onMove}>
          <div className="cmd-form-title">
            移动到 <code>move_to</code>
          </div>
          <div className="field-row">
            <label className="field">
              <span>X（米）</span>
              <input
                type="number"
                step="0.5"
                min={-50}
                max={50}
                value={x}
                onChange={(e) => setX(e.target.value)}
                disabled={!canCommand}
                aria-invalid={!inRange(xNum, -50, 50)}
              />
            </label>
            <label className="field">
              <span>Y（米）</span>
              <input
                type="number"
                step="0.5"
                min={-50}
                max={50}
                value={y}
                onChange={(e) => setY(e.target.value)}
                disabled={!canCommand}
                aria-invalid={!inRange(yNum, -50, 50)}
              />
            </label>
          </div>
          {canCommand && !moveValid && <p className="field-error">x、y 取值范围为 −50 ~ 50</p>}
          <button type="submit" className="btn btn--primary" disabled={disabled || !moveValid}>
            {pending === 'move_to' ? '提交中…' : '移动'}
          </button>
        </form>

        <form className="cmd-form" onSubmit={onSpeed}>
          <div className="cmd-form-title">
            设置速度 <code>set_speed</code>
          </div>
          <div className="field-row">
            <label className="field">
              <span>速度（m/s，0 ~ 2.0）</span>
              <input
                type="number"
                step="0.1"
                min={0}
                max={2}
                value={speed}
                onChange={(e) => setSpeed(e.target.value)}
                disabled={!canCommand}
                aria-invalid={!speedValid}
              />
            </label>
            <button type="submit" className="btn btn--secondary" disabled={disabled || !speedValid}>
              {pending === 'set_speed' ? '提交中…' : '设置'}
            </button>
          </div>
          {canCommand && !speedValid && <p className="field-error">速度取值范围为 0 ~ 2.0</p>}
        </form>

        <div className="cmd-form">
          <div className="cmd-form-title">
            停止 <code>stop</code>
          </div>
          <button
            type="button"
            className="btn btn--danger btn--block"
            disabled={disabled}
            onClick={() => void send({ type: 'stop', params: {} })}
          >
            {pending === 'stop' ? '提交中…' : '立即停止'}
          </button>
        </div>

        {feedback && (
          <div className={`feedback feedback--${feedback.kind}`} role="status">
            <div>{feedback.text}</div>
            <div className="feedback-key mono" title={feedback.idempotencyKey}>
              Idempotency-Key: {feedback.idempotencyKey}
            </div>
          </div>
        )}
      </div>
    </section>
  );
});
