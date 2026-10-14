import { toEpochMs } from '../lib/format';
import type { CommandOut, CommandStatus, LogOut } from '../types';

/**
 * 看板里“选中机器人”相关的状态，用 useReducer 管理。
 *
 * 为什么用 reducer 而不是几个 useState：WebSocket 的回调在连接建立时就注册好了，之后一直是同一个函数。
 * 如果它通过闭包读取 selectedId 之类的 state，读到的永远是注册那一刻的旧值（“过期闭包”问题）。
 * 改成只调用 dispatch(action)：dispatch 本身永远不变，而 reducer 每次执行时拿到的都是最新 state，
 * “这条事件属于当前选中的机器人吗？”“这份响应是不是已经过时了？”这类判断全部集中在下面这个纯函数里。
 */

export type LoadStatus = 'loading' | 'ready' | 'error';

export interface DashboardState {
  selectedRobotId: string | null;
  /** 选中机器人的命令，最新的在前 */
  commands: CommandOut[];
  commandsStatus: LoadStatus;
  commandsError: string | null;
  /** 选中机器人的日志，最新的在前 */
  logs: LogOut[];
  logsStatus: LoadStatus;
  logsError: string | null;
  /** 时间线里正在查看的命令 */
  selectedCommandId: string | null;
}

export type DashboardAction =
  | { type: 'select_robot'; robotId: string }
  | { type: 'select_command'; commandId: string | null }
  | { type: 'commands_loaded'; robotId: string; commands: CommandOut[] }
  | { type: 'commands_failed'; robotId: string; error: string }
  /** 来自 WS 的 command_update，或者 POST 下发命令的响应 */
  | { type: 'command_upsert'; command: CommandOut }
  | { type: 'logs_loaded'; robotId: string; logs: LogOut[] }
  | { type: 'logs_failed'; robotId: string; error: string }
  | { type: 'log_received'; robotId: string; log: LogOut };

export const MAX_COMMANDS = 50;
export const MAX_LOGS = 100;

export const initialDashboardState: DashboardState = {
  selectedRobotId: null,
  commands: [],
  commandsStatus: 'loading',
  commandsError: null,
  logs: [],
  logsStatus: 'loading',
  logsError: null,
  selectedCommandId: null,
};

/**
 * 命令状态的“进度等级”。状态机（SPEC §7）只会往前走：queued → dispatched → acked → 终态，
 * 唯一的“原地转移”是 dispatched → dispatched（断线重连后重投，attempts + 1）。
 * 所以 (等级, attempts) 天然就是一个版本号。
 */
const STATUS_RANK: Record<CommandStatus, number> = {
  queued: 0,
  dispatched: 1,
  acked: 2,
  succeeded: 3,
  failed: 3,
  expired: 3,
};

/**
 * 同一条命令的两个版本，a 是否比 b 新？
 * 为什么需要比较：两个 API 副本各自把状态变化发布到 Redis，事件经过不同路径到达浏览器，
 * 可能乱序（比如先收到 acked 再收到 dispatched）；REST 响应也可能比 WS 推送晚到。
 * 规则：只接受“更新”的版本，旧版本直接丢弃——界面上的状态永远不会倒退。
 */
export function isNewerCommand(a: CommandOut, b: CommandOut): boolean {
  const ra = STATUS_RANK[a.status] ?? 0;
  const rb = STATUS_RANK[b.status] ?? 0;
  if (ra !== rb) return ra > rb;
  if (a.attempts !== b.attempts) return a.attempts > b.attempts;
  return toEpochMs(a.updated_at) > toEpochMs(b.updated_at);
}

const byCreatedDesc = (a: CommandOut, b: CommandOut) => toEpochMs(b.created_at) - toEpochMs(a.created_at);

function mergeCommands(existing: CommandOut[], incoming: CommandOut[]): CommandOut[] {
  const byId = new Map<string, CommandOut>();
  for (const c of existing) byId.set(c.id, c);
  for (const c of incoming) {
    const old = byId.get(c.id);
    if (!old || isNewerCommand(c, old)) byId.set(c.id, c);
  }
  return Array.from(byId.values()).sort(byCreatedDesc).slice(0, MAX_COMMANDS);
}

const logKey = (l: LogOut) => `${toEpochMs(l.ts)}|${l.level}|${l.msg}`;

/** 合并日志并去重：REST 回填和 WS 推送可能包含同一条日志 */
function mergeLogs(existing: LogOut[], incoming: LogOut[]): LogOut[] {
  const seen = new Set<string>();
  const out: LogOut[] = [];
  for (const l of [...incoming, ...existing]) {
    const k = logKey(l);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(l);
  }
  return out.sort((a, b) => toEpochMs(b.ts) - toEpochMs(a.ts)).slice(0, MAX_LOGS);
}

export function dashboardReducer(state: DashboardState, action: DashboardAction): DashboardState {
  switch (action.type) {
    case 'select_robot':
      if (action.robotId === state.selectedRobotId) return state;
      // 换了机器人：清空上一台的命令和日志，进入加载态
      return { ...initialDashboardState, selectedRobotId: action.robotId };

    case 'select_command':
      return { ...state, selectedCommandId: action.commandId };

    case 'commands_loaded':
      // 慢响应：请求发出后用户已经切到别的机器人了，这份结果作废
      if (action.robotId !== state.selectedRobotId) return state;
      // 合并而不是替换：请求在路上时 WS 可能已经推来了更新的状态
      return {
        ...state,
        commands: mergeCommands(state.commands, action.commands),
        commandsStatus: 'ready',
        commandsError: null,
      };

    case 'commands_failed':
      if (action.robotId !== state.selectedRobotId) return state;
      // 已经有数据时（断线重连后的刷新失败）保留旧数据，只记下错误
      return {
        ...state,
        commandsStatus: state.commandsStatus === 'ready' ? 'ready' : 'error',
        commandsError: action.error,
      };

    case 'command_upsert': {
      const cmd = action.command;
      if (cmd.robot_id !== state.selectedRobotId) return state;
      const next = mergeCommands(state.commands, [cmd]);
      // 没有任何变化（比如收到一条乱序的旧事件）就返回原对象，React 会跳过这次渲染
      const changed = next.length !== state.commands.length || next.some((c, i) => c !== state.commands[i]);
      return changed ? { ...state, commands: next } : state;
    }

    case 'logs_loaded':
      if (action.robotId !== state.selectedRobotId) return state;
      return { ...state, logs: mergeLogs(state.logs, action.logs), logsStatus: 'ready', logsError: null };

    case 'logs_failed':
      if (action.robotId !== state.selectedRobotId) return state;
      return { ...state, logsStatus: state.logsStatus === 'ready' ? 'ready' : 'error', logsError: action.error };

    case 'log_received':
      if (action.robotId !== state.selectedRobotId) return state;
      return { ...state, logs: mergeLogs(state.logs, [action.log]) };

    default:
      return state;
  }
}
