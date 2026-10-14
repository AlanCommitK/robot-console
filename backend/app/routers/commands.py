"""命令：创建（仅 admin，支持 Idempotency-Key）、列表、详情（含事件时间线）。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status

from app.commands_service import (
    PublishFailedError,
    create_command,
    get_command_detail,
    list_commands,
)
from app.deps import AdminUser, CurrentUser, ServicesDep, SessionDep
from app.models import Robot
from app.schemas import CommandDetailOut, CommandIn, CommandOut

router = APIRouter(prefix="/api", tags=["commands"])


@router.post(
    "/robots/{robot_id}/commands",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": CommandOut, "description": "Replay of an existing Idempotency-Key"},
        403: {"description": "Viewer role"},
        404: {"description": "Unknown robot"},
        503: {"description": "Could not publish to RabbitMQ (command marked failed)"},
    },
)
async def create_robot_command(
    robot_id: str,
    body: CommandIn,
    response: Response,
    session: SessionDep,
    services: ServicesDep,
    user: AdminUser,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ] = None,
) -> CommandOut:
    # 202 Accepted 而不是 201/200：命令只是"已接受、已排队"，执行结果要稍后通过状态变化得知。
    if await session.get(Robot, robot_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="robot not found")
    try:
        result = await create_command(
            session,
            robot_id=robot_id,
            command_type=body.type,
            params=body.params.model_dump(),
            idempotency_key=idempotency_key,
            created_by=user.username,
            replica=services.settings.replica_id,
            mq=services.mq,
            bus=services.bus,
        )
    except PublishFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not publish command to RabbitMQ; command marked failed",
        ) from exc
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result.command


@router.get("/commands", response_model=list[CommandOut])
async def get_commands(
    session: SessionDep,
    _user: CurrentUser,
    robot_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CommandOut]:
    return await list_commands(session, robot_id=robot_id or None, limit=limit)


@router.get(
    "/commands/{command_id}",
    response_model=CommandDetailOut,
    responses={404: {"description": "Unknown command"}},
)
async def get_command(
    command_id: uuid.UUID, session: SessionDep, _user: CurrentUser
) -> CommandDetailOut:
    detail = await get_command_detail(session, command_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="command not found")
    return detail
