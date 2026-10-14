"""Pure robot model used by the simulator (SPEC §17).

这是一个“纯逻辑”模块：不做网络 I/O、不读系统时钟、不 sleep。
- 时间只通过 step(dt) 的 dt 参数推进；
- 随机数由调用方注入 random.Random（测试里用固定种子就能复现）。
因此“机器人怎么动、电量怎么变、命令怎么判成功/失败”全部可以被单元测试覆盖，
sim.py 只负责把这些结果搬到 WebSocket 上。
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from dataclasses import dataclass

# ---- motion / battery parameters (SPEC §17) ----
DEFAULT_SPEED_MPS = 0.5
MAX_SPEED_MPS = 2.0
WORLD_LIMIT_M = 50.0  # move_to accepts |x|, |y| <= 50, same bound the backend validates
ARRIVAL_TOLERANCE_M = 0.2
DRAIN_PCT_PER_S = 0.05
CHARGE_PCT_PER_S = 0.1
LOW_BATTERY_PCT = 20.0
# 迟滞（hysteresis，类似施密特触发器）：低于 20% 告警一次，回到 25% 以上才重新“上膛”，
# 避免电量在 20% 附近抖动时刷屏告警。
LOW_BATTERY_REARM_PCT = 25.0
# 同理：电量耗尽进入 error 后，要充回 5% 才解除，避免在 0% 附近“动一下又没电”来回切换。
ERROR_RECOVER_PCT = 5.0
WANDER_AFTER_IDLE_S = 20.0
WANDER_RANGE_M = 15.0  # wander waypoints are drawn within +-15 m of the robot's home position
SPAWN_RADIUS_M = 15.0

DEDUPE_CAPACITY = 1000
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 15.0


# ---------------------------------------------------------------------------
# Events returned by RobotModel.step() / apply_command()
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CommandResult:
    """A command reached its final outcome; sim.py sends it as a `result` message."""

    command_id: str
    succeeded: bool
    detail: str | None = None


@dataclass(frozen=True)
class BatteryLow:
    battery: float


@dataclass(frozen=True)
class BatteryDepleted:
    pass


@dataclass(frozen=True)
class WanderStarted:
    x: float
    y: float


Event = CommandResult | BatteryLow | BatteryDepleted | WanderStarted


# ---------------------------------------------------------------------------
# Robot state + behaviour
# ---------------------------------------------------------------------------
@dataclass
class RobotModel:
    x: float
    y: float
    home_x: float
    home_y: float
    theta: float = 0.0
    battery: float = 100.0
    cruise_speed: float = DEFAULT_SPEED_MPS  # speed used whenever the robot drives
    target: tuple[float, float] | None = None
    command_id: str | None = None  # move_to currently executing; None = wandering / no target
    obstacle_after_m: float | None = None  # set when this move was drawn to hit an obstacle
    travelled_m: float = 0.0
    idle_s: float = 0.0
    error: bool = False
    low_battery_warned: bool = False

    @classmethod
    def spawn(cls, index: int, total: int) -> RobotModel:
        """Spread robots on a circle, with different batteries and staggered first wander."""
        angle = 2.0 * math.pi * index / max(total, 1)
        x = round(SPAWN_RADIUS_M * math.cos(angle), 3)
        y = round(SPAWN_RADIUS_M * math.sin(angle), 3)
        return cls(
            x=x,
            y=y,
            home_x=x,
            home_y=y,
            theta=math.atan2(-y, -x),  # face the centre
            battery=96.0 - 13.0 * (index % 4),  # 96 / 83 / 70 / 57 % so the charts differ
            # first wander after 3 s, 5 s, 7 s ... so the dashboard shows motion right away
            idle_s=max(0.0, WANDER_AFTER_IDLE_S - 3.0 - 2.0 * index),
        )

    # ---- derived telemetry fields ----
    @property
    def moving(self) -> bool:
        return self.target is not None and self.cruise_speed > 0 and not self.error

    @property
    def speed(self) -> float:
        return self.cruise_speed if self.moving else 0.0

    @property
    def mode(self) -> str:
        if self.error:
            return "error"
        if self.moving:
            return "moving"
        if self.battery < 100.0:
            return "charging"  # not moving -> sitting on the charger
        return "idle"

    def snapshot(self) -> dict[str, float | str]:
        """Telemetry fields of SPEC §9 (without `type` and `ts`, which sim.py adds)."""
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "theta": round(self.theta, 4),
            "speed": round(self.speed, 3),
            "battery": round(self.battery, 2),
            "mode": self.mode,
        }

    # ---- commands ----
    def apply_command(
        self,
        command_id: str,
        command: str,
        params: object,
        rng: random.Random,
        fail_rate: float,
    ) -> list[CommandResult]:
        """Execute a NEW command (sim.py has already filtered duplicates).

        Returns the results that are known immediately. A successful move_to returns
        nothing here; its result comes later from step() when the robot arrives.
        Every command that gets executed ends with exactly one result, including a
        move_to that is interrupted by stop or by a newer move_to.
        """
        if command == "move_to":
            x, y = _number(params, "x"), _number(params, "y")
            if x is None or y is None or abs(x) > WORLD_LIMIT_M or abs(y) > WORLD_LIMIT_M:
                return [CommandResult(command_id, False, "invalid params for move_to")]
            if self.error:
                return [CommandResult(command_id, False, "robot in error state (battery depleted)")]
            # SPEC：失败与否在“开始时”就抽签决定，执行过程中不再随机
            will_fail = rng.random() < fail_rate
            results = self._halt(f"preempted by command {command_id}")
            self._set_target(x, y, command_id, will_fail)
            return results
        if command == "stop":
            # stop = abort the current motion (actual speed becomes 0).
            # The configured cruise speed is kept for later moves.
            results = self._halt(f"cancelled by stop command {command_id}")
            return [*results, CommandResult(command_id, True)]
        if command == "set_speed":
            speed = _number(params, "speed")
            if speed is None or not 0.0 <= speed <= MAX_SPEED_MPS:
                return [CommandResult(command_id, False, "invalid params for set_speed")]
            self.cruise_speed = speed
            return [CommandResult(command_id, True)]
        return [CommandResult(command_id, False, f"unknown command {command!r}")]

    # ---- time ----
    def step(self, dt: float, rng: random.Random) -> list[Event]:
        """Advance the simulation by dt seconds and return what happened."""
        events: list[Event] = []
        if dt <= 0:
            return events
        travelled = self._advance(dt, events)
        self._update_battery(dt, travelled > 0, events)
        self._maybe_wander(dt, rng, events)
        return events

    # ---- internals ----
    def _advance(self, dt: float, events: list[Event]) -> float:
        """Drive toward the target; returns metres travelled during this step."""
        if self.target is None or self.error:
            return 0.0
        tx, ty = self.target
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        stride = min(self.cruise_speed * dt, dist)
        if self.obstacle_after_m is not None and self.travelled_m + stride >= self.obstacle_after_m:
            # this move was drawn to fail: stop halfway and report the obstacle
            stride = max(0.0, self.obstacle_after_m - self.travelled_m)
            self._move(dx, dy, dist, stride)
            events.extend(self._halt("obstacle detected"))
            return stride
        self._move(dx, dy, dist, stride)
        if dist - stride <= ARRIVAL_TOLERANCE_M:
            command_id = self.command_id
            self._clear_target()
            if command_id is not None:
                events.append(CommandResult(command_id, True))
        return stride

    def _move(self, dx: float, dy: float, dist: float, stride: float) -> None:
        if dist <= 0 or stride <= 0:
            return
        self.theta = math.atan2(dy, dx)  # theta follows the heading
        self.x += dx / dist * stride
        self.y += dy / dist * stride
        self.travelled_m += stride

    def _update_battery(self, dt: float, moved: bool, events: list[Event]) -> None:
        if moved:
            self.battery = max(0.0, self.battery - DRAIN_PCT_PER_S * dt)
            if self.battery <= 0.0:
                self.error = True
                events.extend(self._halt("battery depleted"))
                events.append(BatteryDepleted())
        else:
            self.battery = min(100.0, self.battery + CHARGE_PCT_PER_S * dt)
            if self.error and self.battery >= ERROR_RECOVER_PCT:
                self.error = False
        if self.battery < LOW_BATTERY_PCT and not self.low_battery_warned:
            self.low_battery_warned = True
            events.append(BatteryLow(self.battery))
        elif self.battery >= LOW_BATTERY_REARM_PCT:
            self.low_battery_warned = False

    def _maybe_wander(self, dt: float, rng: random.Random, events: list[Event]) -> None:
        if self.target is not None or self.error:
            self.idle_s = 0.0
            return
        self.idle_s += dt
        if self.idle_s > WANDER_AFTER_IDLE_S:
            x = _clamp(self.home_x + rng.uniform(-WANDER_RANGE_M, WANDER_RANGE_M))
            y = _clamp(self.home_y + rng.uniform(-WANDER_RANGE_M, WANDER_RANGE_M))
            self._set_target(x, y, command_id=None, will_fail=False)
            events.append(WanderStarted(round(x, 3), round(y, 3)))

    def _set_target(self, x: float, y: float, command_id: str | None, will_fail: bool) -> None:
        dist = math.hypot(x - self.x, y - self.y)
        self.target = (x, y)
        self.command_id = command_id
        self.travelled_m = 0.0
        self.obstacle_after_m = dist / 2.0 if will_fail else None
        self.idle_s = 0.0

    def _halt(self, reason: str) -> list[CommandResult]:
        """Stop driving. A move_to that was still running gets a failed result."""
        command_id = self.command_id
        self._clear_target()
        return [] if command_id is None else [CommandResult(command_id, False, reason)]

    def _clear_target(self) -> None:
        self.target = None
        self.command_id = None
        self.obstacle_after_m = None
        self.travelled_m = 0.0


def _number(params: object, key: str) -> float | None:
    """Read a finite float from a params dict; bool / str / NaN are rejected."""
    if not isinstance(params, dict):
        return None
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _clamp(value: float, limit: float = WORLD_LIMIT_M) -> float:
    return max(-limit, min(limit, value))


# ---------------------------------------------------------------------------
# Duplicate-delivery protection
# ---------------------------------------------------------------------------
class LruSet:
    """Bounded set of recently processed command_ids (least recently used is evicted).

    为什么需要它：RabbitMQ 是 at-least-once（至少一次）投递——服务端没收到 ack 的消息
    （例如机器人刚收到命令、ack 还没送达连接就断了）会在重连后再投递一次。
    机器人按 command_id 去重：见过的 id 只重新回 ack，不再执行，
    于是“至少一次投递 + 幂等处理”在效果上等于“恰好执行一次”。
    容量有上限（1000）是为了内存不随运行时间无限增长；重复投递只会发生在最近的命令上，
    所以只记住最近的 N 个就够了。
    """

    def __init__(self, capacity: int = DEDUPE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._items: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> bool:
        """Remember key. Returns True if it is new, False if it was already seen."""
        if key in self._items:
            self._items.move_to_end(key)  # refresh recency
            return False
        self._items[key] = None
        if len(self._items) > self.capacity:
            self._items.popitem(last=False)  # evict the least recently used id
        return True

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# Reconnect delay
# ---------------------------------------------------------------------------
def backoff_delay(
    attempt: int,
    rng: random.Random,
    base: float = BACKOFF_BASE_S,
    cap: float = BACKOFF_CAP_S,
) -> float:
    """Exponential backoff with "equal jitter": a random delay in [ceiling/2, ceiling].

    ceiling = min(cap, base * 2**attempt) -> 1, 2, 4, 8, 15, 15 ... seconds.

    指数退避：服务端挂了时，重试间隔越拉越长，不会每秒狂敲一个已经起不来的服务。
    抖动（jitter）：nginx 或某个 API 副本重启时，所有机器人是“同一瞬间”断线的；
    如果大家都恰好等 1 s、2 s、4 s 再重连，就会一波一波同时砸向刚恢复的服务
    （惊群 / thundering herd）。给每次等待加随机量，重连就被摊开在一个时间窗里。
    """
    ceiling = min(cap, base * 2 ** min(attempt, 16))
    return ceiling / 2.0 + rng.uniform(0.0, ceiling / 2.0)
