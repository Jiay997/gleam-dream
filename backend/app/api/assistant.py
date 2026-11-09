"""AI 创作助手路由（PRD V6.4 第 6 章多智能体架构）。

端点：
- ``GET /projects/{project_id}/assistant/context``：上下文徽标与场景清单（view）；
- ``GET /projects/{project_id}/assistant/conversation``：会话详情与最近消息（view）；
- ``POST /projects/{project_id}/assistant/messages``：发送消息并生成回复（edit）；
- ``POST /projects/{project_id}/assistant/actions/{action_id}/execute``：
  确认执行动作（edit，必须显式 confirm=true）；
- ``DELETE /projects/{project_id}/assistant/conversation``：清空会话（edit）。

所有响应统一 ``success_envelope``；对话失败在服务层降级为 degraded 消息，
动作执行失败按业务码抛出（前端 toast + 刷新会话展示失败态）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id, project_guard
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.project import Project
from app.schemas.assistant import (
    AssistantActionExecuteRequest,
    AssistantMessageRequest,
)
from app.services import assistant_service

#: 创作助手路由
router = APIRouter(tags=["assistant"])


@router.get("/projects/{project_id}/assistant/context")
async def get_assistant_context(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    episode_no: Annotated[int, Query(ge=1, description="聚焦集号")] = 1,
) -> dict[str, Any]:
    """查询助手上下文与场景清单。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        episode_no: 聚焦集号（默认 1）。

    Returns:
        成功包络，data 为 ``{context, scenarios, agent_labels}``。
    """
    data = await assistant_service.context_payload(session, project, episode_no)
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/assistant/conversation")
async def get_assistant_conversation(
    project: Annotated[Project, Depends(project_guard("view"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    episode_no: Annotated[int, Query(ge=1, description="聚焦集号")] = 1,
) -> dict[str, Any]:
    """读取当前用户在该项目的助手会话（无则自动创建）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        episode_no: 聚焦集号（默认 1）。

    Returns:
        成功包络，data 为 ``{conversation, messages, actions, scenarios,
        context_version}``。
    """
    data = await assistant_service.get_conversation_detail(
        session, project=project, user_id=int(user_id), episode_no=episode_no
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/assistant/messages")
async def post_assistant_message(
    payload: AssistantMessageRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """发送一条助手消息并生成回复。

    Args:
        payload: 消息请求体（content/episode_no/client_message_id）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{conversation, message, actions, degraded,
        context_version}``。

    Raises:
        BizError: 10001 消息为空；10004 限流；20003 越权；80001 由服务层
            降级为 degraded 消息（不向上抛出）。
    """
    content = payload.content.strip()
    if not content:
        raise BizError(ErrorCode.PARAM_INVALID, "消息不能为空")
    data = await assistant_service.send_message(
        session,
        project=project,
        user_id=int(user_id),
        content=content,
        episode_no=payload.episode_no,
        client_message_id=payload.client_message_id,
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/assistant/actions/{action_id}/execute")
async def post_assistant_action_execute(
    action_id: int,
    payload: AssistantActionExecuteRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """确认执行助手动作（HITL；客户端只提交 action_id）。

    Args:
        action_id: 动作 ID。
        payload: 确认请求体（``confirm=true``）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{action, message}``；202 类长任务动作
        ``action.status=accepted``。

    Raises:
        BizError: 10001 动作过期/参数篡改；10002 动作不存在；20003 越权；
            30002 预算超限；下游业务异常（10002/40001/40002/80001 等）。
    """
    action, message = await assistant_service.execute_action(
        session,
        project=project,
        user_id=int(user_id),
        action_id=action_id,
    )
    return success_envelope(
        {
            "action": assistant_service.action_payload(action),
            "message": assistant_service.message_payload(message)
            if message is not None
            else None,
        },
        request_id,
    )


@router.delete("/projects/{project_id}/assistant/conversation")
async def delete_assistant_conversation(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """清空当前用户的助手会话（软删除）。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"deleted": true}``。

    Raises:
        BizError: 10002 会话不存在。
    """
    data = await assistant_service.delete_conversation(
        session, project=project, user_id=int(user_id)
    )
    return success_envelope(data, request_id)
