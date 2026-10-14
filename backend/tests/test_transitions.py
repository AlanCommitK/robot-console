"""transition_locked（apply_event 和 sweeper 共用的转移逻辑）与 sweeper 的规则，不需要数据库。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app import state_machine as sm
from app.commands_service import transition_locked
from app.models import Command, CommandEvent
from app.sweeper import SweepLimits, _sweep_event

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def make_command(status: str, **fields: Any) -> Command:
    earlier = NOW - timedelta(minutes=10)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "robot_id": "robot-1",
        "type": "move_to",
        "params": {"x": 1, "y": 2},
        "status": status,
        "attempts": 1,
        "error": None,
        "created_by": "admin",
        "created_at": earlier,
        "updated_at": earlier,
        "dispatched_at": earlier,
        "acked_at": earlier if status == sm.ACKED else None,
        "finished_at": None,
    }
    values.update(fields)
    return Command(**values)


def test_result_timeout_fails_an_acked_command() -> None:
    session = FakeSession()
    command = make_command(sm.ACKED)
    transition = transition_locked(
        session,  # type: ignore[arg-type]
        command,
        sm.RESULT_TIMEOUT,
        "swept: no result within 300s after ack",
        replica="api-1",
        now=NOW,
    )
    assert transition is not None
    assert (transition.previous, transition.new_status) == (sm.ACKED, sm.FAILED)
    assert (command.status, command.error) == (sm.FAILED, "result_timeout")
    assert command.finished_at == NOW
    assert command.updated_at == NOW
    [event] = session.added
    assert isinstance(event, CommandEvent)
    assert (event.from_status, event.to_status) == (sm.ACKED, sm.FAILED)
    assert event.detail == "swept: no result within 300s after ack"
    assert event.replica == "api-1"


def test_swept_expiry_uses_the_normal_expire_event() -> None:
    session = FakeSession()
    command = make_command(sm.QUEUED, attempts=0, dispatched_at=None)
    transition = transition_locked(
        session,  # type: ignore[arg-type]
        command,
        sm.EXPIRE,
        "swept: no update for over 90s while queued",
        replica="api-2",
        now=NOW,
    )
    assert transition is not None
    assert (command.status, command.error, command.attempts) == (sm.EXPIRED, "expired", 0)


def test_ignored_event_changes_nothing() -> None:
    session = FakeSession()
    command = make_command(sm.SUCCEEDED, finished_at=NOW - timedelta(minutes=1))
    before = (command.status, command.updated_at, command.finished_at, command.error)
    assert (
        transition_locked(
            session,  # type: ignore[arg-type]
            command,
            sm.RESULT_TIMEOUT,
            replica="api-1",
            now=NOW,
        )
        is None
    )
    assert (command.status, command.updated_at, command.finished_at, command.error) == before
    assert session.added == []


def test_sweep_limits_follow_the_settings() -> None:
    limits = SweepLimits.from_settings(command_ttl_ms=30_000, result_timeout_s=300)
    assert limits.stale_after_s == 90  # COMMAND_TTL_MS + 60 s
    assert limits.result_timeout_s == 300


def test_sweep_event_per_status() -> None:
    limits = SweepLimits(stale_after_s=90, result_timeout_s=300)
    assert _sweep_event(make_command(sm.ACKED), limits) == (
        sm.RESULT_TIMEOUT,
        "swept: no result within 300s after ack",
    )
    for status in (sm.QUEUED, sm.DISPATCHED):
        assert _sweep_event(make_command(status), limits) == (
            sm.EXPIRE,
            f"swept: no update for over 90s while {status}",
        )
