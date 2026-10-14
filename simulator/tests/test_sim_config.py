"""sim.py 的配置默认值，以及机器人连接时凭证的去向（请求头，而不是 URL）。"""

import asyncio

import pytest

import sim

SIM_ENV = ("WS_URL", "ROBOTS", "TELEMETRY_HZ", "CHAOS_DISCONNECT_PER_MIN", "FAIL_RATE", "LOG_LEVEL")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SIM_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_the_spec(clean_env: None) -> None:
    cfg = sim.Config.from_env()
    assert cfg.telemetry_hz == 5.0
    assert cfg.ws_url == "ws://nginx/ws/robot"
    assert cfg.chaos_per_min == 0.5
    assert cfg.fail_rate == 0.05
    assert [robot_id for robot_id, _ in cfg.robots] == [f"robot-{n}" for n in range(1, 5)]


def test_token_travels_in_the_authorization_header_not_the_url(clean_env: None) -> None:
    cfg = sim.Config.from_env()
    agent = sim.RobotAgent("robot-1", "dev-token-robot-1", 0, 1, cfg, asyncio.Event())
    assert agent.url == "ws://nginx/ws/robot?robot_id=robot-1"
    assert "token" not in agent.url
    assert agent.auth_headers == {"Authorization": "Bearer dev-token-robot-1"}


def test_existing_query_string_is_kept(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WS_URL", "ws://example:8080/ws/robot?debug=1")
    agent = sim.RobotAgent("robot-2", "t", 0, 1, sim.Config.from_env(), asyncio.Event())
    assert agent.url == "ws://example:8080/ws/robot?debug=1&robot_id=robot-2"
