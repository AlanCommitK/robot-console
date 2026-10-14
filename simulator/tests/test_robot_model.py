import math
import random

import pytest

import robot_model as rm
from robot_model import (
    BatteryDepleted,
    BatteryLow,
    CommandResult,
    LruSet,
    RobotModel,
    WanderStarted,
)

DT = 0.1


def make_robot(**overrides) -> RobotModel:
    """A robot at the origin with a full battery that will not start wandering by itself."""
    fields = {"x": 0.0, "y": 0.0, "home_x": 0.0, "home_y": 0.0, "battery": 100.0}
    fields.update(overrides)
    return RobotModel(**fields)


def run(
    robot: RobotModel, seconds: float, rng: random.Random | None = None, dt: float = DT
) -> list:
    rng = rng or random.Random(0)
    events = []
    for _ in range(round(seconds / dt)):
        events.extend(robot.step(dt, rng))
    return events


def results(events) -> list[CommandResult]:
    return [e for e in events if isinstance(e, CommandResult)]


# ---------------------------------------------------------------- dedupe set
class TestLruSet:
    def test_new_then_duplicate(self):
        seen = LruSet(capacity=3)
        assert seen.add("a") is True
        assert seen.add("a") is False
        assert "a" in seen and len(seen) == 1

    def test_evicts_least_recently_used(self):
        seen = LruSet(capacity=3)
        for key in ("a", "b", "c"):
            seen.add(key)
        seen.add("a")  # duplicate refreshes "a" -> "b" is now the oldest
        seen.add("d")
        assert "b" not in seen
        assert {"a", "c", "d"} == {k for k in ("a", "b", "c", "d") if k in seen}
        assert len(seen) == 3

    def test_size_stays_bounded(self):
        seen = LruSet(capacity=1000)
        for i in range(5000):
            seen.add(f"cmd-{i}")
        assert len(seen) == 1000
        assert "cmd-4999" in seen and "cmd-0" not in seen

    def test_default_capacity_is_1000(self):
        assert LruSet().capacity == 1000

    def test_rejects_zero_capacity(self):
        with pytest.raises(ValueError):
            LruSet(capacity=0)


# ---------------------------------------------------------------- backoff
class TestBackoff:
    @pytest.mark.parametrize(
        ("attempt", "ceiling"),
        [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0), (4, 15.0), (10, 15.0), (1000, 15.0)],
    )
    def test_delay_within_equal_jitter_window(self, attempt, ceiling):
        rng = random.Random(1)
        for _ in range(200):
            delay = rm.backoff_delay(attempt, rng)
            assert ceiling / 2 <= delay <= ceiling

    def test_jitter_spreads_simultaneous_reconnects(self):
        rng = random.Random(7)
        delays = {round(rm.backoff_delay(3, rng), 3) for _ in range(50)}
        assert len(delays) > 40  # robots that dropped together do not retry together


# ---------------------------------------------------------------- spawn / telemetry
class TestSpawnAndSnapshot:
    def test_spawn_positions_are_spread_out(self):
        robots = [RobotModel.spawn(i, 4) for i in range(4)]
        for i, a in enumerate(robots):
            for b in robots[i + 1 :]:
                assert math.hypot(a.x - b.x, a.y - b.y) > 10
        assert len({r.battery for r in robots}) == 4

    def test_snapshot_has_spec_fields(self):
        snap = RobotModel.spawn(0, 4).snapshot()
        assert set(snap) == {"x", "y", "theta", "speed", "battery", "mode"}
        assert snap["mode"] in {"idle", "moving", "charging", "error"}

    def test_mode_idle_vs_charging(self):
        assert make_robot(battery=100.0).mode == "idle"
        assert make_robot(battery=50.0).mode == "charging"


# ---------------------------------------------------------------- move_to
class TestMoveTo:
    def test_moves_at_cruise_speed_and_faces_heading(self):
        robot = make_robot()
        assert (
            robot.apply_command("c1", "move_to", {"x": 0.0, "y": 10.0}, random.Random(0), 0.0) == []
        )
        robot.step(1.0, random.Random(0))
        assert robot.y == pytest.approx(0.5)  # default 0.5 m/s
        assert robot.theta == pytest.approx(math.pi / 2)
        assert robot.mode == "moving" and robot.speed == pytest.approx(0.5)

    def test_arrives_within_tolerance_and_succeeds_once(self):
        robot = make_robot()
        robot.apply_command("c1", "move_to", {"x": 3.0, "y": 4.0}, random.Random(0), 0.0)
        events = run(robot, 12)
        assert results(events) == [CommandResult("c1", True)]
        assert math.hypot(robot.x - 3.0, robot.y - 4.0) <= rm.ARRIVAL_TOLERANCE_M
        assert robot.target is None and robot.speed == 0.0 and robot.mode != "moving"

    def test_fail_rate_one_hits_obstacle_halfway(self):
        robot = make_robot()
        robot.apply_command("c1", "move_to", {"x": 10.0, "y": 0.0}, random.Random(0), 1.0)
        events = run(robot, 15)  # obstacle after 5 m = 10 s (stay well below the 20 s wander delay)
        assert results(events) == [CommandResult("c1", False, "obstacle detected")]
        assert robot.x == pytest.approx(5.0)
        assert robot.target is None

    def test_fail_rate_is_drawn_once_at_start(self):
        rng = random.Random(123)
        failures = 0
        for i in range(2000):
            robot = make_robot()
            robot.apply_command(f"c{i}", "move_to", {"x": 1.0, "y": 0.0}, rng, 0.25)
            failures += robot.obstacle_after_m is not None
        assert 400 < failures < 600

    def test_new_move_preempts_previous_one(self):
        robot = make_robot()
        robot.apply_command("c1", "move_to", {"x": 10.0, "y": 0.0}, random.Random(0), 0.0)
        run(robot, 2)
        out = robot.apply_command("c2", "move_to", {"x": -10.0, "y": 0.0}, random.Random(0), 0.0)
        assert out == [CommandResult("c1", False, "preempted by command c2")]
        assert robot.command_id == "c2"

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"x": 1.0},
            {"x": "1", "y": 2},
            {"x": True, "y": 0},
            {"x": 51, "y": 0},
            {"x": float("nan"), "y": 0},
            None,
        ],
    )
    def test_invalid_params_fail_immediately(self, params):
        robot = make_robot()
        out = robot.apply_command("c1", "move_to", params, random.Random(0), 0.0)
        assert out == [CommandResult("c1", False, "invalid params for move_to")]
        assert robot.target is None


# ---------------------------------------------------------------- stop / set_speed / unknown
class TestOtherCommands:
    def test_stop_cancels_running_move_and_succeeds(self):
        robot = make_robot()
        robot.apply_command("c1", "move_to", {"x": 10.0, "y": 0.0}, random.Random(0), 0.0)
        run(robot, 2)
        out = robot.apply_command("c2", "stop", {}, random.Random(0), 0.0)
        assert out == [
            CommandResult("c1", False, "cancelled by stop command c2"),
            CommandResult("c2", True),
        ]
        x_after_stop = robot.x
        assert robot.speed == 0.0 and robot.target is None
        run(robot, 2)
        assert robot.x == x_after_stop  # it really stopped

    def test_stop_when_idle_just_succeeds(self):
        out = make_robot().apply_command("c1", "stop", {}, random.Random(0), 0.0)
        assert out == [CommandResult("c1", True)]

    def test_set_speed_changes_cruise_speed(self):
        robot = make_robot()
        assert robot.apply_command("c1", "set_speed", {"speed": 2.0}, random.Random(0), 0.0) == [
            CommandResult("c1", True)
        ]
        robot.apply_command("c2", "move_to", {"x": 10.0, "y": 0.0}, random.Random(0), 0.0)
        robot.step(1.0, random.Random(0))
        assert robot.x == pytest.approx(2.0)
        assert robot.speed == pytest.approx(2.0)

    def test_set_speed_zero_parks_robot_with_target_kept(self):
        robot = make_robot(battery=90.0)
        robot.apply_command("c1", "move_to", {"x": 10.0, "y": 0.0}, random.Random(0), 0.0)
        robot.apply_command("c2", "set_speed", {"speed": 0}, random.Random(0), 0.0)
        events = run(robot, 5)
        assert results(events) == [] and robot.x == 0.0
        assert robot.mode == "charging" and robot.battery > 90.0

    @pytest.mark.parametrize("params", [{"speed": -0.1}, {"speed": 2.01}, {"speed": "fast"}, {}])
    def test_set_speed_rejects_out_of_range(self, params):
        robot = make_robot()
        out = robot.apply_command("c1", "set_speed", params, random.Random(0), 0.0)
        assert out == [CommandResult("c1", False, "invalid params for set_speed")]
        assert robot.cruise_speed == rm.DEFAULT_SPEED_MPS

    def test_unknown_command_fails(self):
        out = make_robot().apply_command("c1", "dance", {}, random.Random(0), 0.0)
        assert out == [CommandResult("c1", False, "unknown command 'dance'")]


# ---------------------------------------------------------------- battery / mode
class TestBattery:
    def test_drains_while_moving(self):
        robot = make_robot(battery=80.0)
        robot.apply_command("c1", "move_to", {"x": 40.0, "y": 0.0}, random.Random(0), 0.0)
        run(robot, 10)
        assert robot.battery == pytest.approx(80.0 - 0.05 * 10, abs=1e-6)

    def test_recharges_when_not_moving_and_clamps_at_100(self):
        robot = make_robot(battery=50.0)
        run(robot, 10)
        assert robot.battery == pytest.approx(51.0, abs=1e-6)
        assert robot.mode == "charging"
        full = make_robot(battery=99.95)
        run(full, 5)
        assert full.battery == 100.0 and full.mode == "idle"

    def test_low_battery_warns_once_with_hysteresis(self):
        def lows(events):
            return [e for e in events if isinstance(e, BatteryLow)]

        rng = random.Random(0)
        robot = make_robot(battery=20.2)
        robot.apply_command("c1", "move_to", {"x": 45.0, "y": 0.0}, rng, 0.0)
        first = lows(run(robot, 10))  # 20.2 -> 19.7 %
        assert len(first) == 1 and first[0].battery < 20.0
        # park (speed 0 keeps the target, so no wander) and charge to ~21.7 %
        robot.apply_command("c2", "set_speed", {"speed": 0.0}, rng, 0.0)
        assert not lows(run(robot, 20))
        # drive below 20 % again: still latched, so no second warning
        robot.apply_command("c3", "set_speed", {"speed": 0.5}, rng, 0.0)
        assert not lows(run(robot, 60))
        assert robot.battery < 20.0
        # charge above 25 % -> re-armed for the next dip
        robot.apply_command("c4", "set_speed", {"speed": 0.0}, rng, 0.0)
        run(robot, 70)
        assert robot.battery > 25.0 and robot.low_battery_warned is False

    def test_battery_hits_zero_enters_error_and_recovers(self):
        robot = make_robot(battery=0.01)
        robot.apply_command("c1", "move_to", {"x": 40.0, "y": 0.0}, random.Random(0), 0.0)
        events = run(robot, 1)
        assert CommandResult("c1", False, "battery depleted") in events
        assert BatteryDepleted() in events
        assert robot.mode == "error" and robot.speed == 0.0
        # while in error, new moves are refused
        out = robot.apply_command("c2", "move_to", {"x": 1.0, "y": 0.0}, random.Random(0), 0.0)
        assert out == [CommandResult("c2", False, "robot in error state (battery depleted)")]
        run(robot, 40)  # recharge 0.1 %/s -> 4 % : still latched in error
        assert robot.mode == "error"
        run(robot, 15)  # > 5 % -> recovered
        assert robot.mode == "charging"

    def test_always_between_0_and_100(self):
        rng = random.Random(5)
        robot = make_robot(battery=0.02)
        robot.apply_command("c1", "move_to", {"x": 40.0, "y": 0.0}, rng, 0.0)
        for _ in range(20_000):  # ~33 simulated minutes: depletion, recovery, wandering
            robot.step(DT, rng)
            assert 0.0 <= robot.battery <= 100.0


# ---------------------------------------------------------------- wander
class TestWander:
    def test_wanders_after_20_s_idle_without_command_result(self):
        robot = make_robot()
        assert not run(robot, 19.5)
        events = run(robot, 1.0)
        wander = [e for e in events if isinstance(e, WanderStarted)]
        assert len(wander) == 1
        assert robot.target is not None and robot.command_id is None
        assert abs(wander[0].x) <= rm.WANDER_RANGE_M and abs(wander[0].y) <= rm.WANDER_RANGE_M
        assert not results(run(robot, 120))  # finishing a wander is not a command result

    def test_wander_target_stays_inside_world(self):
        robot = make_robot(x=49.0, y=-49.0, home_x=49.0, home_y=-49.0)
        rng = random.Random(3)
        for _ in range(20):
            robot.idle_s = rm.WANDER_AFTER_IDLE_S + 1
            robot.target = None
            robot.step(DT, rng)
            tx, ty = robot.target
            assert abs(tx) <= rm.WORLD_LIMIT_M and abs(ty) <= rm.WORLD_LIMIT_M

    def test_move_to_replaces_wander_silently(self):
        robot = make_robot()
        run(robot, 21)
        assert robot.target is not None and robot.command_id is None
        out = robot.apply_command("c1", "move_to", {"x": 1.0, "y": 1.0}, random.Random(0), 0.0)
        assert out == []  # nothing to preempt: wandering is not a command

    def test_no_wander_while_executing_command(self):
        robot = make_robot()
        robot.apply_command("c1", "set_speed", {"speed": 0.0}, random.Random(0), 0.0)
        robot.apply_command("c2", "move_to", {"x": 5.0, "y": 0.0}, random.Random(0), 0.0)
        assert not [e for e in run(robot, 60) if isinstance(e, WanderStarted)]
