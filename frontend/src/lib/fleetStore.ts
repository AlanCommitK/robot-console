import { RingBuffer } from './ringBuffer';
import { toEpochMs } from './format';
import type { RobotMode, RobotOut, TelemetryOut, WsRobotStatusEvent, WsTelemetryEvent } from '../types';

/** 图表时间窗口：最近 120 秒 */
export const WINDOW_MS = 120_000;
/** 每台机器人最多保留 600 个点 = 5 Hz（模拟器默认上报频率）× 120 s */
export const BUFFER_CAPACITY = 600;
/** 后端判定在线的 TTL 是 5 s（ROBOT_ONLINE_TTL_S），前端多留 1 s 余量 */
export const STALE_AFTER_MS = 6_000;

/** 统一后的遥测样本：t 是 epoch 毫秒 */
export interface Sample {
  t: number;
  x: number;
  y: number;
  theta: number;
  speed: number;
  battery: number;
  mode: RobotMode;
}

/** 交给 React 渲染的、不可变的机器人快照（表格的一行） */
export interface RobotView {
  id: string;
  name: string;
  online: boolean;
  replica: string | null;
  /** 最后一次收到这台机器人数据的时间（epoch 毫秒） */
  lastSeenMs: number | null;
  latest: Sample | null;
}

interface Entry {
  id: string;
  name: string;
  online: boolean;
  replica: string | null;
  lastSeenMs: number | null;
  latest: Sample | null;
  buffer: RingBuffer<Sample>;
  /** 最后一次收到该机器人 WS 事件的本地时间；用来判断一份 REST 快照是否比它旧 */
  lastEventAt: number;
  version: number;
}

const collator = new Intl.Collator('en', { numeric: true });

type RawSample = Pick<TelemetryOut, 'x' | 'y' | 'theta' | 'speed' | 'battery' | 'mode'> & { ts: number | string };

function toSample(src: RawSample): Sample | null {
  const t = toEpochMs(src.ts);
  if (!Number.isFinite(t)) return null;
  return {
    t,
    x: src.x,
    y: src.y,
    theta: typeof src.theta === 'number' ? src.theta : 0,
    speed: src.speed,
    battery: src.battery,
    mode: src.mode,
  };
}

/**
 * 机器人实时数据的“外部仓库”：放在 React state 之外的可变对象（Dashboard 里用 useRef 持有）。
 *
 * 为什么不把每条遥测都 setState：4 台机器人 × 5 Hz = 每秒 20 条消息。如果每条都触发一次 React 重新渲染，
 * 整个看板每秒要重算 20 次，图表也要重画 20 次，又费 CPU 又没有意义（人眼看不出 20 Hz 和 4 Hz 的差别）。
 * 所以消息到达时只做 O(1) 的写入（环形缓冲区 push），然后由界面按固定节奏（4 Hz）来取一份快照去渲染。
 * version 每次变化都 +1，界面比较 version 就知道“这 250 ms 里有没有新数据”，没有就跳过渲染。
 *
 * 机器人的在线状态也走这里（而不是直接 dispatch 到 React）：因为遥测是批量、延迟最多 250 ms 才生效的，
 * 如果 robot_status 绕过这个仓库立即生效，两类事件的先后顺序就会被打乱（比如“最后一条遥测”被晚于
 * “离线通知”应用，把机器人又标回在线）。所有改变机器人状态的事件按到达顺序写进同一个仓库，顺序就是对的。
 */
export class FleetStore {
  private readonly entries = new Map<string, Entry>();
  private version = 0;

  getVersion(): number {
    return this.version;
  }

  getRobotVersion(robotId: string): number {
    return this.entries.get(robotId)?.version ?? -1;
  }

  private entry(id: string): Entry {
    let e = this.entries.get(id);
    if (!e) {
      e = {
        id,
        name: id,
        online: false,
        replica: null,
        lastSeenMs: null,
        latest: null,
        buffer: new RingBuffer<Sample>(BUFFER_CAPACITY),
        lastEventAt: 0,
        version: 0,
      };
      this.entries.set(id, e);
    }
    return e;
  }

  private touch(e: Entry): void {
    e.version += 1;
    this.version += 1;
  }

  /**
   * 用 GET /api/robots 的结果初始化/校正状态（首次加载、断线重连后各调一次）。
   * requestStartedAt 是发请求的时间：如果在那之后又收到过某台机器人的 WS 事件，
   * 说明 WS 的数据比这份 REST 快照新，就只更新名字，不覆盖在线状态和最新遥测。
   */
  seedFromRest(robots: RobotOut[], requestStartedAt: number): void {
    for (const r of robots) {
      const e = this.entry(r.id);
      e.name = r.name;
      if (e.lastEventAt <= requestStartedAt) {
        e.online = r.online;
        e.replica = r.replica;
        const seen = toEpochMs(r.last_seen);
        e.lastSeenMs = Number.isFinite(seen) ? seen : null;
        const s = r.state ? toSample(r.state) : null;
        if (s) e.latest = s;
      }
      this.touch(e);
    }
  }

  applyTelemetry(ev: WsTelemetryEvent, receivedAt = Date.now()): void {
    const s = toSample(ev);
    if (!s) return;
    const e = this.entry(ev.robot_id);
    const last = e.buffer.last();
    // 同一台机器人的样本必须按时间递增，乱序/重复的旧样本丢掉，否则折线会“往回画”
    if (!last || s.t > last.t) {
      e.buffer.push(s);
      e.latest = s;
    }
    e.online = true; // 能收到遥测，说明它此刻就在线
    if (ev.replica) e.replica = ev.replica;
    // “最后上报时间”用浏览器本地的接收时间，而不是机器人自报的 ts：
    // 机器人、服务器、浏览器三台机器的时钟可能差几秒，用同一个时钟（浏览器的）算“多久没收到”才可靠
    e.lastSeenMs = receivedAt;
    e.lastEventAt = receivedAt;
    this.touch(e);
  }

  applyStatus(ev: WsRobotStatusEvent, receivedAt = Date.now()): void {
    const e = this.entry(ev.robot_id);
    e.online = ev.online;
    if (ev.replica) e.replica = ev.replica;
    e.lastEventAt = receivedAt;
    this.touch(e);
  }

  /**
   * 把 GET /api/robots/{id}/telemetry?seconds=120 的历史数据（数据库里约 2 Hz）合并进缓冲区。
   * 缓冲区里可能已经有页面打开后通过 WS 收到的实时数据（5 Hz），两者按时间合并、去重：
   * 历史数据只用来填补实时数据没有覆盖到的时间段（页面打开之前、断线期间）。
   */
  mergeBackfill(robotId: string, rows: TelemetryOut[]): void {
    const e = this.entry(robotId);
    const live = e.buffer.toArray();
    const liveTimes = new Set(live.map((s) => Math.round(s.t / 10)));
    const history = rows
      .map(toSample)
      .filter((s): s is Sample => s !== null && !liveTimes.has(Math.round(s.t / 10)));
    if (history.length === 0) return;

    const merged = [...history, ...live].sort((a, b) => a.t - b.t);
    e.buffer.clear();
    // 超过容量时环形缓冲区会自动挤掉最旧的点，正好保留最新的 BUFFER_CAPACITY 个
    for (const s of merged) e.buffer.push(s);
    if (!e.latest) e.latest = e.buffer.last() ?? null;
    this.touch(e);
  }

  /** 所有机器人的快照（按 id 自然排序：robot-2 排在 robot-10 前面） */
  snapshot(): RobotView[] {
    return Array.from(this.entries.values(), (e) => ({
      id: e.id,
      name: e.name,
      online: e.online,
      replica: e.replica,
      lastSeenMs: e.lastSeenMs,
      latest: e.latest,
    })).sort((a, b) => collator.compare(a.id, b.id));
  }

  /**
   * 某台机器人最近 WINDOW_MS 内的样本（从旧到新）以及窗口右端时间。
   * 窗口右端取 max(浏览器当前时间, 最新样本时间)：即使机器人时钟比浏览器快几秒，最新的点也不会被裁到窗口外。
   */
  series(robotId: string, now = Date.now()): { samples: Sample[]; end: number } {
    const e = this.entries.get(robotId);
    if (!e) return { samples: [], end: now };
    const newest = e.buffer.last();
    const end = Math.max(now, newest ? newest.t : 0);
    const cutoff = end - WINDOW_MS;
    return { samples: e.buffer.toArray().filter((s) => s.t >= cutoff), end };
  }
}

/**
 * 表格里显示的“在线”：服务端说在线，并且（实时连接正常时）最近 STALE_AFTER_MS 内确实收到过它的数据。
 * 后端的在线标记是一个 5 s 过期的 Redis key——机器人“悄无声息地”掉线时（没有正常关闭连接），
 * 服务端不会推送 robot_status，前端只能靠“多久没收到数据”自己判断。
 * 实时连接本身断开期间我们什么都收不到，这时不能据此判定机器人离线，保留最后已知状态即可（顶部会提示数据可能过期）。
 */
export function isLive(robot: RobotView, now: number, socketOpen: boolean): boolean {
  if (!robot.online) return false;
  if (!socketOpen || robot.lastSeenMs === null) return true;
  return now - robot.lastSeenMs < STALE_AFTER_MS;
}
