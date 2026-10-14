"""状态机的完整转移表测试：6 个状态 × 7 个事件 = 42 种组合，每一种都明确断言。"""

from __future__ import annotations

import itertools

import pytest

from app import state_machine as sm

# 期望的完整转移表（SPEC §7）。不在表里的组合都应该返回 None。
EXPECTED: dict[tuple[str, str], str] = {
    (sm.QUEUED, sm.DISPATCH): sm.DISPATCHED,
    (sm.DISPATCHED, sm.DISPATCH): sm.DISPATCHED,
    (sm.DISPATCHED, sm.ACK): sm.ACKED,
    (sm.DISPATCHED, sm.RESULT_SUCCEEDED): sm.SUCCEEDED,
    (sm.DISPATCHED, sm.RESULT_FAILED): sm.FAILED,
    (sm.ACKED, sm.RESULT_SUCCEEDED): sm.SUCCEEDED,
    (sm.ACKED, sm.RESULT_FAILED): sm.FAILED,
    (sm.QUEUED, sm.RESULT_FAILED): sm.FAILED,
    (sm.QUEUED, sm.EXPIRE): sm.EXPIRED,
    (sm.DISPATCHED, sm.EXPIRE): sm.EXPIRED,
    (sm.DISPATCHED, sm.ACK_TIMEOUT): sm.FAILED,
    (sm.ACKED, sm.RESULT_TIMEOUT): sm.FAILED,
}

ALL_PAIRS = list(itertools.product(sm.STATUSES, sm.EVENTS))


def test_table_covers_every_status_and_event() -> None:
    assert len(ALL_PAIRS) == 42
    assert set(sm.STATUSES) == {"queued", "dispatched", "acked", "succeeded", "failed", "expired"}
    assert set(sm.EVENTS) == {
        "dispatch",
        "ack",
        "result_succeeded",
        "result_failed",
        "expire",
        "ack_timeout",
        "result_timeout",
    }
    assert set(EXPECTED) <= set(ALL_PAIRS)  # 期望表里不能有拼错的状态/事件


@pytest.mark.parametrize(("status", "event"), ALL_PAIRS)
def test_next_status_matches_table(status: str, event: str) -> None:
    assert sm.next_status(status, event) == EXPECTED.get((status, event))


@pytest.mark.parametrize("status", sorted(sm.TERMINAL_STATUSES))
@pytest.mark.parametrize("event", sm.EVENTS)
def test_terminal_statuses_ignore_every_event(status: str, event: str) -> None:
    assert sm.is_terminal(status)
    assert sm.next_status(status, event) is None


def test_duplicate_ack_is_ignored() -> None:
    after_first = sm.next_status(sm.DISPATCHED, sm.ACK)
    assert after_first == sm.ACKED
    assert sm.next_status(after_first, sm.ACK) is None


def test_late_ack_after_result_is_ignored() -> None:
    # 乱序：结果先到（隐含 ack），真正的 ack 后到
    status = sm.next_status(sm.DISPATCHED, sm.RESULT_SUCCEEDED)
    assert status == sm.SUCCEEDED
    assert sm.next_status(status, sm.ACK) is None


def test_redelivery_keeps_dispatched() -> None:
    assert sm.next_status(sm.DISPATCHED, sm.DISPATCH) == sm.DISPATCHED


def test_expiry_cannot_override_success_and_success_cannot_override_expiry() -> None:
    assert sm.next_status(sm.SUCCEEDED, sm.EXPIRE) is None
    assert sm.next_status(sm.EXPIRED, sm.RESULT_SUCCEEDED) is None


def test_acked_command_cannot_expire_or_time_out_waiting_for_ack() -> None:
    # 已 ack 的消息早已从 RabbitMQ 删除，不会再有过期；ack 超时也只在等待 ack 时有意义。
    assert sm.next_status(sm.ACKED, sm.EXPIRE) is None
    assert sm.next_status(sm.ACKED, sm.ACK_TIMEOUT) is None
    assert sm.next_status(sm.ACKED, sm.DISPATCH) is None


def test_result_timeout_only_applies_after_ack() -> None:
    # 结果超时由 sweeper 发出，只针对"已 ack、迟迟没有结果"的命令；
    # 还没 ack 的命令由 ack 超时或过期处理。
    assert sm.next_status(sm.ACKED, sm.RESULT_TIMEOUT) == sm.FAILED
    assert sm.next_status(sm.QUEUED, sm.RESULT_TIMEOUT) is None
    assert sm.next_status(sm.DISPATCHED, sm.RESULT_TIMEOUT) is None


def test_late_result_after_result_timeout_is_ignored() -> None:
    status = sm.next_status(sm.ACKED, sm.RESULT_TIMEOUT)
    assert status == sm.FAILED
    assert sm.next_status(status, sm.RESULT_SUCCEEDED) is None


def test_unknown_inputs_are_ignored() -> None:
    assert sm.next_status("nonsense", sm.DISPATCH) is None
    assert sm.next_status(sm.QUEUED, "nonsense") is None
