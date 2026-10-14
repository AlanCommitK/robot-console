"""机器人：列表/详情（数据库 + Redis 实时状态）、遥测历史（PostgreSQL）、最近日志（Redis）。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import CurrentUser, Services, ServicesDep, SessionDep
from app.models import Robot, Telemetry
from app.redis_bus import RobotLiveState
from app.schemas import LogOut, RobotOut, TelemetryOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/robots", tags=["robots"])


@router.get("", response_model=list[RobotOut])
async def list_robots(
    session: SessionDep, services: ServicesDep, _user: CurrentUser
) -> list[RobotOut]:
    robots = (await session.execute(select(Robot).order_by(Robot.id))).scalars().all()
    live = await _live_states(services, [robot.id for robot in robots])
    return [_robot_out(robot, live[robot.id]) for robot in robots]


@router.get("/{robot_id}", response_model=RobotOut, responses={404: {"description": "Unknown"}})
async def get_robot(
    robot_id: str, session: SessionDep, services: ServicesDep, _user: CurrentUser
) -> RobotOut:
    robot = await _get_robot_or_404(session, robot_id)
    live = await _live_states(services, [robot.id])
    return _robot_out(robot, live[robot.id])


@router.get("/{robot_id}/telemetry", response_model=list[TelemetryOut])
async def robot_telemetry(
    robot_id: str,
    session: SessionDep,
    _user: CurrentUser,
    seconds: Annotated[int, Query(ge=1, le=3600)] = 120,
) -> list[TelemetryOut]:
    await _get_robot_or_404(session, robot_id)
    since = datetime.now(UTC) - timedelta(seconds=seconds)
    # 命中复合索引 (robot_id, ts DESC)：先按 robot_id 定位，再在时间范围内顺序读取。
    stmt = (
        select(Telemetry)
        .where(Telemetry.robot_id == robot_id, Telemetry.ts >= since)
        .order_by(Telemetry.ts.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [TelemetryOut.model_validate(row) for row in rows]


@router.get("/{robot_id}/logs", response_model=list[LogOut])
async def robot_logs(
    robot_id: str,
    session: SessionDep,
    services: ServicesDep,
    _user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[LogOut]:
    await _get_robot_or_404(session, robot_id)
    try:
        raw_logs = await services.bus.get_logs(robot_id, limit)
    except RedisError as exc:
        raise _redis_unavailable() from exc
    logs: list[LogOut] = []
    for raw in raw_logs:  # LPUSH 让最新的在表头，所以这里天然是"最新在前"
        try:
            logs.append(LogOut.model_validate_json(raw))
        except ValidationError:
            logger.warning("skipping malformed log entry", extra={"robot_id": robot_id})
    return logs


async def _get_robot_or_404(session: AsyncSession, robot_id: str) -> Robot:
    robot = await session.get(Robot, robot_id)
    if robot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="robot not found")
    return robot


async def _live_states(services: Services, robot_ids: Sequence[str]) -> dict[str, RobotLiveState]:
    try:
        return await services.bus.get_live_states(robot_ids)
    except RedisError as exc:
        raise _redis_unavailable() from exc


def _redis_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="live state unavailable (redis)"
    )


def _robot_out(robot: Robot, live: RobotLiveState) -> RobotOut:
    return RobotOut(
        id=robot.id,
        name=robot.name,
        # 在线 = online 键存在。这个键带 TTL、每条遥测续期，副本崩溃时也会自动过期。
        online=live.online_replica is not None,
        replica=live.online_replica,
        last_seen=_epoch_to_datetime(live.state.get("last_seen")),
        state=_parse_state(live.state),
    )


def _parse_state(fields: dict[str, str]) -> TelemetryOut | None:
    """Redis 哈希里的值都是字符串，这里转回数字；字段不全（从没上报过）时返回 None。"""
    try:
        return TelemetryOut(
            ts=datetime.fromtimestamp(float(fields["ts"]), UTC),
            x=float(fields["x"]),
            y=float(fields["y"]),
            theta=float(fields["theta"]),
            speed=float(fields["speed"]),
            battery=float(fields["battery"]),
            mode=fields["mode"],
        )
    except (KeyError, ValueError, OverflowError):
        return None


def _epoch_to_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (ValueError, OverflowError):
        return None
