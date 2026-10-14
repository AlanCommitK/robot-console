import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { useAuth } from '../auth';
import { describeError, isAbortError } from '../api';
import { DashboardSocket, type ConnectionState } from '../ws';
import { FleetStore, type RobotView } from '../lib/fleetStore';
import { ROLE_LABEL } from '../lib/format';
import { dashboardReducer, initialDashboardState, type LoadStatus } from './dashboardState';
import { ConnectionIndicator } from '../components/ConnectionIndicator';
import { Logo } from '../components/Logo';
import { RobotDetail } from '../components/RobotDetail';
import { RobotTable } from '../components/RobotTable';
import type { CommandOut } from '../types';

/** 表格刷新节奏（4 Hz），与图表一致 */
const TABLE_REFRESH_MS = 250;

/**
 * 看板页：WebSocket 连接、机器人列表、“选中哪台机器人”都放在这一层。
 * 原因是这些数据同时被两块兄弟区域使用——上面的表格（所有机器人）和下面的详情区（选中的那台），
 * 按 React 的“状态提升”原则，状态放在它们最近的公共父组件，也就是这里。
 * 反过来，只有某个子组件自己关心的状态（命令表单的输入值、图表的 4 Hz 快照、时间线的加载态）就留在子组件里。
 */
export function Dashboard() {
  const { session, api, logout } = useAuth();

  // 实时数据仓库：普通的可变对象，用 useRef 持有——它在组件整个生命周期里只创建一次，修改它也不会触发渲染
  const storeRef = useRef<FleetStore | null>(null);
  if (storeRef.current === null) storeRef.current = new FleetStore();
  const store = storeRef.current;

  const [conn, setConn] = useState<ConnectionState>({ status: 'connecting', attempt: 0 });
  const [everConnected, setEverConnected] = useState(false);
  /** 每次 +1 都会让下面依赖它的 effect 重新拉取 REST 数据（断线重连后、点“重试”时） */
  const [resyncEpoch, setResyncEpoch] = useState(0);
  const [robots, setRobots] = useState<RobotView[]>([]);
  const [robotsStatus, setRobotsStatus] = useState<LoadStatus>('loading');
  const [robotsError, setRobotsError] = useState<string | null>(null);
  const [backfill, setBackfill] = useState<LoadStatus>('loading');
  const [data, dispatch] = useReducer(dashboardReducer, initialDashboardState);

  const token = session?.token ?? null;
  const selectedId = data.selectedRobotId;

  // ① 实时连接：登录后建立，组件卸载（登出）时关闭。token 变了（重新登录）就换一条新连接
  useEffect(() => {
    if (!token) return;
    const socket = new DashboardSocket({
      // 每次（重新）连接前用 JWT 换一张一次性票据：JWT 本身从不出现在 WebSocket 的 URL 里
      fetchTicket: (signal) => api.wsTicket(signal).then((t) => t.ticket),
      onEvent: (ev) => {
        switch (ev.type) {
          case 'telemetry':
            store.applyTelemetry(ev);
            break;
          case 'robot_status':
            store.applyStatus(ev);
            break;
          case 'command_update':
            dispatch({ type: 'command_upsert', command: ev.command });
            break;
          case 'log':
            dispatch({ type: 'log_received', robotId: ev.robot_id, log: { ts: ev.ts, level: ev.level, msg: ev.msg } });
            break;
          default:
            break; // pong 只用于保活/探活，ws.ts 已经记录了“最后收到消息的时间”
        }
      },
      onStateChange: (s) => {
        setConn(s);
        if (s.status === 'open') setEverConnected(true);
      },
      // 推送是“错过就没了”的：断线期间的事件不会补发。所以重连成功后用 REST 把快照重新拉一遍
      onOpen: ({ reconnect }) => {
        if (reconnect) setResyncEpoch((n) => n + 1);
      },
      onAuthFailed: () => logout('实时连接鉴权失败（token 无效或已过期），请重新登录'),
    });
    socket.start();
    return () => socket.stop();
  }, [token, api, store, logout]);

  // ② 机器人列表：首次加载，以及每次重连后校正
  useEffect(() => {
    const ctrl = new AbortController();
    const startedAt = Date.now();
    api
      .robots(ctrl.signal)
      .then((list) => {
        store.seedFromRest(list, startedAt);
        setRobots(store.snapshot());
        setRobotsStatus('ready');
        setRobotsError(null);
      })
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        setRobotsError(describeError(err));
        setRobotsStatus((s) => (s === 'ready' ? 'ready' : 'error'));
      });
    return () => ctrl.abort();
  }, [api, store, resyncEpoch]);

  // ③ 节流：每 250 ms 检查一次仓库有没有变化，有才把快照交给 React 渲染表格
  useEffect(() => {
    let lastVersion = store.getVersion();
    const timer = window.setInterval(() => {
      const version = store.getVersion();
      if (version === lastVersion) return;
      lastVersion = version;
      setRobots(store.snapshot());
    }, TABLE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [store]);

  // ④ 还没选中任何机器人时，默认选中第一台
  useEffect(() => {
    if (selectedId === null && robots.length > 0) dispatch({ type: 'select_robot', robotId: robots[0].id });
  }, [robots, selectedId]);

  // ⑤ 选中机器人（或重连）后：回填最近 120 秒遥测、命令列表、日志。
  //    切换机器人时 cleanup 里的 abort() 会取消上一台还没回来的请求；即使有漏网之鱼，reducer 也会按 robotId 丢弃。
  useEffect(() => {
    if (!selectedId) return;
    const id = selectedId;
    const ctrl = new AbortController();
    setBackfill('loading');
    api
      .telemetry(id, 120, ctrl.signal)
      .then((rows) => {
        store.mergeBackfill(id, rows);
        setBackfill('ready');
      })
      .catch((err: unknown) => {
        if (!isAbortError(err)) setBackfill('error');
      });
    api
      .commands(id, 50, ctrl.signal)
      .then((commands) => dispatch({ type: 'commands_loaded', robotId: id, commands }))
      .catch((err: unknown) => {
        if (!isAbortError(err)) dispatch({ type: 'commands_failed', robotId: id, error: describeError(err) });
      });
    api
      .logs(id, 100, ctrl.signal)
      .then((logs) => dispatch({ type: 'logs_loaded', robotId: id, logs }))
      .catch((err: unknown) => {
        if (!isAbortError(err)) dispatch({ type: 'logs_failed', robotId: id, error: describeError(err) });
      });
    return () => ctrl.abort();
  }, [api, store, selectedId, resyncEpoch]);

  // 传给子组件的回调用 useCallback 固定下来，配合子组件的 React.memo：
  // 表格每秒刷新 4 次时，命令列表、日志这些 props 没变的子组件可以直接跳过渲染
  const selectRobot = useCallback((robotId: string) => dispatch({ type: 'select_robot', robotId }), []);
  const selectCommand = useCallback((commandId: string) => dispatch({ type: 'select_command', commandId }), []);
  const onCommandCreated = useCallback((command: CommandOut) => {
    dispatch({ type: 'command_upsert', command });
    dispatch({ type: 'select_command', commandId: command.id }); // 自动打开刚下发命令的时间线
  }, []);
  const retry = useCallback(() => setResyncEpoch((n) => n + 1), []);

  if (!session) return null;

  const socketOpen = conn.status === 'open';
  const reconnecting = conn.status === 'reconnecting' || (conn.status === 'connecting' && conn.attempt > 0);
  const selectedRobot = robots.find((r) => r.id === selectedId) ?? null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <Logo size={30} />
          <div>
            <div className="brand-name">机器人集群控制台</div>
            <div className="brand-sub">Robot Console</div>
          </div>
        </div>
        <div className="topbar-right">
          <ConnectionIndicator state={conn} />
          <span className="divider" aria-hidden="true" />
          <div className="user">
            <span className="avatar" aria-hidden="true">
              {session.username.slice(0, 1).toUpperCase()}
            </span>
            <span className="user-name">{session.username}</span>
            <span className={`tag tag--${session.role}`}>{ROLE_LABEL[session.role]}</span>
          </div>
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => logout()}>
            退出登录
          </button>
        </div>
      </header>

      {reconnecting && (
        <div className="banner banner--warn" role="alert">
          {everConnected
            ? '实时连接已断开，正在自动重连。重连前表格和图表中的数据可能已过期。'
            : '暂时无法建立实时连接，正在自动重试。'}
        </div>
      )}

      <main className="page">
        <RobotTable
          robots={robots}
          status={robotsStatus}
          error={robotsError}
          selectedId={selectedId}
          socketOpen={socketOpen}
          onSelect={selectRobot}
          onRetry={retry}
        />

        {selectedRobot ? (
          // key：切换机器人时整个详情区重新挂载，表单、图表等局部状态自动重置，不会串到另一台机器人上
          <RobotDetail
            key={selectedRobot.id}
            robot={selectedRobot}
            socketOpen={socketOpen}
            store={store}
            backfill={backfill}
            data={data}
            isAdmin={session.role === 'admin'}
            onCommandCreated={onCommandCreated}
            onSelectCommand={selectCommand}
          />
        ) : (
          robotsStatus === 'ready' &&
          robots.length > 0 && <div className="card state">在上方表格中选择一台机器人查看详情</div>
        )}
      </main>
    </div>
  );
}
