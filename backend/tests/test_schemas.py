"""schemas.py 的单元测试：命令参数边界、机器人消息（遥测等）校验。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from app.schemas import (
    CommandIn,
    MoveToCommandIn,
    RobotAckMsg,
    RobotLogMsg,
    RobotResultMsg,
    RobotTelemetryMsg,
    SetSpeedCommandIn,
    StopCommandIn,
    dashboard_message_adapter,
    robot_message_adapter,
)

command_adapter: TypeAdapter[Any] = TypeAdapter(CommandIn)


def parse_command(payload: dict[str, Any]) -> Any:
    return command_adapter.validate_python(payload)


# --------------------------------------------------------------------------- 命令参数


@pytest.mark.parametrize("value", [-50, -50.0, 0, 12.5, 50, 50.0])
def test_move_to_accepts_coordinates_within_50(value: float) -> None:
    command = parse_command({"type": "move_to", "params": {"x": value, "y": -value}})
    assert isinstance(command, MoveToCommandIn)
    assert command.params.model_dump() == {"x": float(value), "y": float(-value)}


@pytest.mark.parametrize(
    "params",
    [
        {"x": 50.001, "y": 0},
        {"x": 0, "y": -50.001},
        {"x": 1e9, "y": 0},
        {"x": 0},  # 缺 y
        {"x": "far", "y": 0},
        {"x": 0, "y": 0, "z": 1},  # 多余字段
    ],
)
def test_move_to_rejects_bad_params(params: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_command({"type": "move_to", "params": params})


def test_move_to_rejects_nan_and_infinity() -> None:
    for raw in ('{"type":"move_to","params":{"x":NaN,"y":0}}',
                '{"type":"move_to","params":{"x":Infinity,"y":0}}'):
        with pytest.raises(ValidationError):
            command_adapter.validate_json(raw)


@pytest.mark.parametrize("speed", [0, 0.0, 1.25, 2, 2.0])
def test_set_speed_accepts_0_to_2(speed: float) -> None:
    command = parse_command({"type": "set_speed", "params": {"speed": speed}})
    assert isinstance(command, SetSpeedCommandIn)


@pytest.mark.parametrize("speed", [-0.01, 2.01, 10])
def test_set_speed_rejects_out_of_range(speed: float) -> None:
    with pytest.raises(ValidationError):
        parse_command({"type": "set_speed", "params": {"speed": speed}})


def test_stop_accepts_empty_or_missing_params() -> None:
    assert isinstance(parse_command({"type": "stop", "params": {}}), StopCommandIn)
    command = parse_command({"type": "stop"})
    assert isinstance(command, StopCommandIn)
    assert command.params.model_dump() == {}


def test_stop_rejects_params() -> None:
    with pytest.raises(ValidationError):
        parse_command({"type": "stop", "params": {"speed": 1}})


def test_unknown_command_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_command({"type": "self_destruct", "params": {}})


# --------------------------------------------------------------------------- 机器人消息


def telemetry(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "telemetry",
        "ts": 1_791_875_000.25,
        "x": 1.0,
        "y": -2.5,
        "theta": 0.5,
        "speed": 0.5,
        "battery": 87.5,
        "mode": "moving",
    }
    message.update(overrides)
    return message


def test_valid_telemetry_is_parsed() -> None:
    message = robot_message_adapter.validate_json(json.dumps(telemetry()))
    assert isinstance(message, RobotTelemetryMsg)
    assert message.battery == 87.5
    assert message.mode == "moving"


def test_telemetry_ignores_unknown_extra_fields() -> None:
    message = robot_message_adapter.validate_json(json.dumps(telemetry(firmware="1.2.3")))
    assert isinstance(message, RobotTelemetryMsg)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "flying"},
        {"battery": 100.1},
        {"battery": -1},
        {"ts": -5},
        {"ts": 1e20},
        {"x": "left"},
    ],
)
def test_invalid_telemetry_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(json.dumps(telemetry(**overrides)))


def test_telemetry_missing_field_is_rejected() -> None:
    message = telemetry()
    del message["speed"]
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(json.dumps(message))


def test_telemetry_rejects_nan() -> None:
    raw = json.dumps(telemetry()).replace('"x": 1.0', '"x": NaN')
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(raw)


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"type":"teleport"}', ""])
def test_garbage_robot_messages_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(raw)


def test_ack_result_and_log_messages() -> None:
    command_id = "1f0e1c2a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
    ack = robot_message_adapter.validate_json(json.dumps({"type": "ack", "command_id": command_id}))
    assert isinstance(ack, RobotAckMsg)
    assert str(ack.command_id) == command_id

    result = robot_message_adapter.validate_json(
        json.dumps({"type": "result", "command_id": command_id, "status": "failed",
                    "detail": "obstacle detected"})
    )
    assert isinstance(result, RobotResultMsg)
    assert result.detail == "obstacle detected"

    log = robot_message_adapter.validate_json(
        json.dumps({"type": "log", "level": "warn", "msg": "battery low"})
    )
    assert isinstance(log, RobotLogMsg)


def test_ack_with_invalid_uuid_is_rejected() -> None:
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json('{"type":"ack","command_id":"42"}')


def test_result_with_unknown_status_is_rejected() -> None:
    command_id = "1f0e1c2a-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(
            json.dumps({"type": "result", "command_id": command_id, "status": "maybe"})
        )


def test_log_message_length_and_level_are_bounded() -> None:
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(
            json.dumps({"type": "log", "level": "info", "msg": "x" * 501})
        )
    with pytest.raises(ValidationError):
        robot_message_adapter.validate_json(
            json.dumps({"type": "log", "level": "debug", "msg": "hi"})
        )


def test_dashboard_ping_only() -> None:
    assert dashboard_message_adapter.validate_json('{"type":"ping"}').type == "ping"
    with pytest.raises(ValidationError):
        dashboard_message_adapter.validate_json('{"type":"pong"}')
