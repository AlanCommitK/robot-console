// 与 SPEC.md §8（REST）和 §10（看板 WebSocket）逐字段对应的类型定义。
// 字段名保持后端的 snake_case，不做驼峰转换：前后端对照阅读时一眼就能对上。
//
// 注意：TypeScript 类型只在编译期存在，运行时会被擦除。
// 来自网络的数据并不会因为“声明了类型”就真的长这样——所以 ws.ts 里还会做一次轻量的运行时检查。

export type Role = 'admin' | 'viewer';
export type RobotMode = 'idle' | 'moving' | 'charging' | 'error';
export type CommandType = 'move_to' | 'stop' | 'set_speed';
export type CommandStatus = 'queued' | 'dispatched' | 'acked' | 'succeeded' | 'failed' | 'expired';
export type LogLevel = 'info' | 'warn' | 'error';

/** iso8601 时间字符串，例如 "2026-10-13T07:12:00.123456+00:00" */
export type IsoDateTime = string;

// ---------- REST ----------

/** POST /api/auth/login 的请求体 */
export interface LoginRequest {
  username: string;
  password: string;
}

/** POST /api/auth/login 的响应 */
export interface LoginResponse {
  access_token: string;
  token_type: 'bearer';
  username: string;
  role: Role;
  expires_at: IsoDateTime;
}

/** GET /api/me */
export interface Me {
  username: string;
  role: Role;
}

/** POST /api/ws-ticket：用 JWT 换来的一次性 WebSocket 票据（只能用一次，expires_in 秒后作废） */
export interface WsTicket {
  ticket: string;
  expires_in: number;
}

/** 一条遥测（REST 里 ts 是 iso8601；WebSocket 里 ts 是 epoch 秒，见 WsTelemetryEvent） */
export interface TelemetryOut {
  ts: IsoDateTime;
  x: number;
  y: number;
  theta: number;
  speed: number;
  battery: number;
  mode: RobotMode;
}

/** GET /api/robots 列表中的一项 */
export interface RobotOut {
  id: string;
  name: string;
  online: boolean;
  replica: string | null;
  last_seen: IsoDateTime | null;
  state: TelemetryOut | null;
}

/** GET /api/robots/{id}/logs 中的一项（最新的在前） */
export interface LogOut {
  ts: IsoDateTime;
  level: LogLevel;
  msg: string;
}

export interface MoveToParams {
  x: number;
  y: number;
}
export interface SetSpeedParams {
  speed: number;
}
export type StopParams = Record<string, never>;

/** POST /api/robots/{id}/commands 的请求体：用“可辨识联合类型”保证 type 和 params 一一对应 */
export type CommandIn =
  | { type: 'move_to'; params: MoveToParams }
  | { type: 'set_speed'; params: SetSpeedParams }
  | { type: 'stop'; params: StopParams };

export interface CommandOut {
  id: string;
  robot_id: string;
  type: CommandType;
  params: Record<string, unknown>;
  status: CommandStatus;
  attempts: number;
  error: string | null;
  created_by: string;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  dispatched_at: IsoDateTime | null;
  acked_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
}

export interface CommandEvent {
  from_status: CommandStatus | null;
  to_status: CommandStatus;
  detail: string | null;
  replica: string | null;
  at: IsoDateTime;
}

/** GET /api/commands/{id} = CommandOut + 事件时间线 */
export interface CommandDetail extends CommandOut {
  events: CommandEvent[];
}

/** 所有错误响应的格式。FastAPI 的 422 校验错误里 detail 是数组，所以这里两种都要接住 */
export interface ErrorBody {
  detail?: string | Array<{ msg?: string; loc?: Array<string | number> }>;
}

// ---------- WebSocket：GET /ws/dashboard?ticket=<一次性票据> ----------

export interface WsTelemetryEvent {
  type: 'telemetry';
  robot_id: string;
  replica: string;
  /** epoch 秒（浮点数），注意和 REST 的 iso8601 不同 */
  ts: number;
  x: number;
  y: number;
  theta: number;
  speed: number;
  battery: number;
  mode: RobotMode;
}

export interface WsRobotStatusEvent {
  type: 'robot_status';
  robot_id: string;
  online: boolean;
  replica: string;
}

export interface WsCommandUpdateEvent {
  type: 'command_update';
  command: CommandOut;
}

export interface WsLogEvent {
  type: 'log';
  robot_id: string;
  ts: IsoDateTime;
  level: LogLevel;
  msg: string;
}

export interface WsPongEvent {
  type: 'pong';
}

export type DashboardEvent =
  | WsTelemetryEvent
  | WsRobotStatusEvent
  | WsCommandUpdateEvent
  | WsLogEvent
  | WsPongEvent;
