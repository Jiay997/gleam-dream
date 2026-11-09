"""项目域服务层模块。

实现项目全生命周期业务：
- **状态机**：14 态白名单流转（00 契约 §6.1），paused/failed 进入时把
  原状态存入 config（``_paused_before`` / ``_failed_before``），退出时还原；
  每次流转写 operation_logs 并发布 SSE ``status_changed`` 事件；
- **项目 CRUD**：创建（自动插入 owner 授权）、分页列表、详情、
  乐观锁更新（lock_version）、软删除、预算/集数校验；
- **快照与回滚**：手动/自动快照（上限 30，超限清理最旧匿名快照）、
  按 state 恢复项目字段（幂等判断）、回滚快照再留痕；
- **团队与项目成员**：团队创建/邀请/移除（级联清理项目授权）、
  项目三级权限设置与成员列表；敏感操作统一写 audit_logs。

事件发布（后续任务复用）::

    from app.infra.event_bus import event_bus
    await event_bus.publish(project_id, "status_changed", {...})
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.infra.event_bus import event_bus
from app.models.billing import CreditAccount
from app.models.iam import AuditLog, OperationLog, ProjectMember, Team, TeamMember, User
from app.models.project import Project, ProjectSnapshot
from app.repositories.project_repo import ProjectRepository, TeamRepository
from app.schemas.project import (
    ProjectCreateRequest,
    ProjectPermissionSetRequest,
    ProjectUpdateRequest,
    TeamCreateRequest,
    TeamMemberAddRequest,
    project_member_out,
    project_out,
    snapshot_out,
    team_member_out,
    team_out,
)
from app.services import analytics_service

logger = logging.getLogger("gleamdream.services.project")

# =====================================================================
# 状态机白名单（对齐 00 契约 §6.1 ProjectStatus，务必保持一致）
# =====================================================================

#: 状态流转白名单：当前状态 -> 允许的目标状态集合
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"scripting", "archived"}),
    "scripting": frozenset({"script_review", "failed", "draft"}),
    "script_review": frozenset({"scripting", "asset_preparing"}),
    "asset_preparing": frozenset({"asset_review", "failed"}),
    "asset_review": frozenset({"asset_preparing", "storyboarding"}),
    "storyboarding": frozenset({"storyboard_review", "failed"}),
    "storyboard_review": frozenset({"storyboarding", "rendering"}),
    "rendering": frozenset({"post_producing", "failed"}),
    "post_producing": frozenset({"final_review", "failed"}),
    "final_review": frozenset({"post_producing", "completed"}),
    "completed": frozenset({"archived", "rendering"}),
    # archived 无出口；paused/failed 经 /resume 或恢复动作回到记录在
    # config 中的原状态（_paused_before / _failed_before）
    "archived": frozenset(),
    "paused": frozenset(),
    "failed": frozenset(),
}

#: 快照数量上限（超出时清理最旧的非手动快照）
SNAPSHOT_LIMIT = 30

#: 快照 state 中记录的项目字段（回滚时按此恢复）
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "status",
    "config",
    "budget_credits",
    "spent_credits",
    "title",
    "synopsis",
    "genre",
    "style_preset_key",
    "aspect_ratio",
    "language",
    "target_episode_count",
    "current_script_id",
    "current_storyboard_id",
)


def _utcnow() -> datetime:
    """获取当前 UTC 时间（timezone-aware）。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


def _ensure_not_archived(project: Project) -> None:
    """校验项目未归档，已归档则抛 40004。

    Args:
        project: 项目 ORM 实例。

    Raises:
        BizError: 40004 项目已归档。
    """
    if project.status == "archived":
        raise BizError(ErrorCode.PROJECT_ARCHIVED)


def _project_state(project: Project) -> dict[str, Any]:
    """抽取项目关键字段快照（不含触发器标记，用于比对与留痕）。

    Args:
        project: 项目 ORM 实例。

    Returns:
        JSON 可序列化的状态字典（archived_at 为 ISO 字符串）。
    """
    state: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        value = getattr(project, field)
        state[field] = value
    state["archived_at"] = project.archived_at.isoformat() if project.archived_at else None
    return state


async def _add_operation_log(
    session: AsyncSession,
    *,
    project_id: int | None,
    entity_id: int | None,
    action: Literal["create", "update", "delete"],
    diff: dict[str, Any],
    user_id: int | None = None,
    request_id: str | None = None,
) -> None:
    """追加一条操作日志（who/when/what/diff）。

    Args:
        session: 异步数据库会话。
        project_id: 关联项目 ID（可空）。
        entity_id: 实体 ID（可空）。
        action: 动作 create/update/delete。
        diff: 变更前后差异字典。
        user_id: 操作者用户 ID（系统触发时为 None）。
        request_id: 请求 ID（可空）。
    """
    session.add(
        OperationLog(
            actor_user_id=user_id,
            project_id=project_id,
            entity_type="project",
            entity_id=entity_id,
            action=action,
            diff=diff,
            request_id=request_id,
        )
    )


async def _add_audit_log(
    session: AsyncSession,
    *,
    user_id: int,
    action: str,
    project_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """追加一条审计日志（越权/敏感操作留痕）。

    Args:
        session: 异步数据库会话。
        user_id: 操作者用户 ID。
        action: 动作标识（如 acl_denied / team.invite）。
        project_id: 关联项目 ID（可空）。
        target_type: 目标类型（可空）。
        target_id: 目标 ID（可空）。
        detail: 详情字典（可空）。
    """
    session.add(
        AuditLog(
            actor_type="user",
            actor_user_id=user_id,
            project_id=project_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
    )


async def _available_credits(session: AsyncSession, user_id: int) -> int:
    """查询用户可用积分余额（可用 = 余额 - 冻结额；无账户视为 0）。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。

    Returns:
        可用积分数（≥0）。
    """
    stmt = select(CreditAccount).where(
        CreditAccount.owner_type == "user",
        CreditAccount.user_id == user_id,
    )
    account = (await session.execute(stmt)).scalar_one_or_none()
    if account is None:
        return 0
    return max(int(account.balance_credits - account.frozen_credits), 0)


async def _check_budget(session: AsyncSession, user_id: int, budget_credits: int) -> None:
    """校验项目预算不超过用户可用余额（0 表示不限，直接通过）。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。
        budget_credits: 拟设置的预算上限。

    Raises:
        BizError: 30002 预算超限（data 携带 available/budget）。
    """
    if budget_credits <= 0:
        return
    available = await _available_credits(session, user_id)
    if budget_credits > available:
        raise BizError(
            ErrorCode.BUDGET_EXCEEDED,
            f"预算超出可用积分（可用 {available}，预算 {budget_credits}）",
            data={"available": available, "budget": budget_credits},
        )


# =====================================================================
# 项目 CRUD
# =====================================================================


async def create_project(
    session: AsyncSession,
    user_id: int,
    payload: ProjectCreateRequest,
    request_id: str | None = None,
) -> Project:
    """创建项目（status=draft），并自动插入 owner 授权记录。

    校验：
    - ``target_episode_count`` 须在 1~30（否则 40005）；
    - ``budget_credits`` 不超过用户可用余额（否则 30002）；
    - 同一用户（owner）下未删除项目中不允许同名（否则 40006）。

    Args:
        session: 异步数据库会话。
        user_id: 创建者（项目 owner）用户 ID。
        payload: 创建请求体。
        request_id: 请求 ID（写入操作日志，可空）。

    Returns:
        创建后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40005 集数超限；30002 预算超限；40006 项目重名。
    """
    if not (1 <= payload.target_episode_count <= 30):
        raise BizError(
            ErrorCode.EPISODE_LIMIT_EXCEEDED,
            data={"target_episode_count": payload.target_episode_count, "allowed": "1~30"},
        )
    await _check_budget(session, user_id, payload.budget_credits)
    # P1-1 修复：同名项目拦截（owner 维度，未删除范围内）
    duplicated = (
        await session.execute(
            select(Project.id)
            .where(
                Project.owner_user_id == user_id,
                Project.title == payload.title,
                Project.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first()
    if duplicated is not None:
        raise BizError(
            ErrorCode.PROJECT_TITLE_DUPLICATED,
            message=f"已存在同名项目「{payload.title}」，请更换名称",
            data={"title": payload.title},
        )

    project = Project(
        owner_user_id=user_id,
        team_id=payload.team_id,
        title=payload.title,
        synopsis=payload.synopsis,
        genre=payload.genre,
        style_preset_key=payload.style_preset_key,
        aspect_ratio=payload.aspect_ratio,
        language=payload.language,
        status="draft",
        target_episode_count=payload.target_episode_count,
        budget_credits=payload.budget_credits,
        config=payload.config if payload.config is not None else {},
    )
    session.add(project)
    await session.flush()  # 取得自增 id 供成员/日志引用

    # owner 自动获得最高项目授权（export 包含 edit/view）
    session.add(
        ProjectMember(
            project_id=project.id,
            user_id=user_id,
            permission="export",
            granted_by=user_id,
        )
    )
    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="create",
        diff={"title": payload.title, "status": "draft"},
        user_id=user_id,
        request_id=request_id,
    )
    await session.commit()

    # 埋点：项目创建 + 首项目激活（PRD 激活定义；埋点失败不影响创建结果）
    try:
        from sqlalchemy import func as _func

        owned_count = int(
            (
                await session.execute(
                    select(_func.count())
                    .select_from(Project)
                    .where(Project.owner_user_id == user_id, Project.deleted_at.is_(None))
                )
            ).scalar_one()
        )
        await analytics_service.track(
            "project_created",
            user_id=user_id,
            project_id=int(project.id),
            props={"genre": payload.genre, "target_episode_count": payload.target_episode_count},
        )
        if owned_count <= 1:
            await analytics_service.track(
                analytics_service.EVENT_PROJECT_ACTIVATED,
                user_id=user_id,
                project_id=int(project.id),
            )
    except Exception:  # noqa: BLE001
        logger.warning("项目创建埋点失败 project_id=%s", project.id, exc_info=True)
    return project


async def get_project_or_404(session: AsyncSession, project_id: int) -> Project:
    """按 ID 查询存活项目，不存在抛 10002（权限校验由 ACL 依赖负责）。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        项目 ORM 实例。

    Raises:
        BizError: 10002 项目不存在。
    """
    project = await ProjectRepository.get_by_id(session, project_id)
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")
    return project


async def list_projects(
    session: AsyncSession,
    user_id: int,
    *,
    scope: str,
    status: str | None,
    genre: str | None,
    page: int,
    page_size: int,
    sort: str,
) -> dict[str, Any]:
    """分页查询项目列表。

    Args:
        session: 异步数据库会话。
        user_id: 当前用户 ID。
        scope: 范围 created/joined/all。
        status: 状态筛选。
        genre: 题材筛选。
        page: 页码。
        page_size: 每页条数。
        sort: 排序（``-`` 前缀倒序）。

    Returns:
        分页结构 ``{"items", "page", "page_size", "total"}``。
    """
    items, total = await ProjectRepository.list_page(
        session,
        owner_user_id=user_id,
        scope=scope,
        status=status,
        genre=genre,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return {
        "items": [project_out(p) for p in items],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


async def update_project(
    session: AsyncSession,
    project: Project,
    payload: ProjectUpdateRequest,
    user_id: int,
    request_id: str | None = None,
) -> Project:
    """更新项目基础字段（乐观锁 + 归档拦截 + 预算校验）。

    仅处理请求体中显式传入的字段；``lock_version`` 携带且不匹配时抛
    10006；更新成功后版本号自增并写操作日志。

    Args:
        session: 异步数据库会话。
        project: 待更新的项目 ORM 实例。
        payload: 更新请求体（PATCH 语义）。
        user_id: 操作者用户 ID。
        request_id: 请求 ID（可空）。

    Returns:
        更新后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40004 已归档；10006 乐观锁冲突；30002 预算超限。
    """
    _ensure_not_archived(project)
    if payload.lock_version is not None and payload.lock_version != project.lock_version:
        raise BizError(
            ErrorCode.VERSION_CONFLICT,
            data={"expected": project.lock_version, "actual": payload.lock_version},
        )

    changes = payload.model_dump(exclude_unset=True, exclude={"lock_version"})
    # title 显式传 null 无意义，忽略；budget 变更需重新校验余额
    if "title" in changes and changes["title"] is None:
        changes.pop("title")
    if "budget_credits" in changes and changes["budget_credits"] is not None:
        await _check_budget(session, user_id, int(changes["budget_credits"]))

    if not changes:
        return project

    diff = {"before": {k: getattr(project, k) for k in changes}, "after": dict(changes)}
    for key, value in changes.items():
        setattr(project, key, value)
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff=diff,
        user_id=user_id,
        request_id=request_id,
    )
    await session.commit()
    await session.refresh(project)  # 取回 server onupdate 的 updated_at，避免过期属性触发同步 IO
    return project


async def delete_project(
    session: AsyncSession,
    project: Project,
    user_id: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    """软删除项目（写入 deleted_at，不物理删除）。

    Args:
        session: 异步数据库会话。
        project: 待删除的项目 ORM 实例。
        user_id: 操作者用户 ID。
        request_id: 请求 ID（可空）。

    Returns:
        ``{"id": ..., "deleted": True}``。
    """
    project.deleted_at = _utcnow()
    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="delete",
        diff={"title": project.title, "status": project.status},
        user_id=user_id,
        request_id=request_id,
    )
    await session.commit()
    return {"id": project.id, "deleted": True}


# =====================================================================
# 状态机
# =====================================================================


async def transition(
    project: Project,
    target: str,
    reason: str | None = None,
    *,
    session: AsyncSession,
    user_id: int | None = None,
) -> Project:
    """按白名单流转项目状态（核心状态机函数）。

    规则：
    - 已归档项目 -> 40004；
    - 目标状态不在 ``STATUS_TRANSITIONS[当前状态]`` -> 40001（data 携带
      from/to/allowed）；
    - 进入 ``paused`` / ``failed`` 时把当前状态写入 config 的
      ``_paused_before`` / ``_failed_before``；
    - 写 operation_logs，提交后发布 SSE ``status_changed`` 事件。

    Args:
        project: 项目 ORM 实例。
        target: 目标状态。
        reason: 流转原因（写入日志与事件，可空）。
        session: 异步数据库会话（关键字参数）。
        user_id: 触发用户 ID（系统触发时可空）。

    Returns:
        流转后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40004 已归档；40001 状态不允许流转。
    """
    _ensure_not_archived(project)
    allowed = STATUS_TRANSITIONS.get(project.status, frozenset())
    if target not in allowed:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            data={"from": project.status, "to": target, "allowed": sorted(allowed)},
        )

    old_status = project.status
    config = dict(project.config or {})
    if target == "paused":
        config["_paused_before"] = old_status
    elif target == "failed":
        config["_failed_before"] = old_status
    project.config = config
    project.status = target
    if target == "archived":
        project.archived_at = _utcnow()
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"status": {"from": old_status, "to": target}, "reason": reason},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)  # 取回 server onupdate 的 updated_at
    await event_bus.publish(
        project.id,
        "status_changed",
        {"from": old_status, "to": target, "reason": reason, "lock_version": project.lock_version},
    )
    return project


async def pause_project(session: AsyncSession, project: Project, user_id: int) -> Project:
    """暂停项目：任意非 paused/archived 状态进入 paused，原状态存入 config。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        暂停后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40004 已归档；40001 已处于暂停状态。
    """
    _ensure_not_archived(project)
    if project.status == "paused":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "项目已处于暂停状态")

    old_status = project.status
    config = dict(project.config or {})
    config["_paused_before"] = old_status
    project.config = config
    project.status = "paused"
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"status": {"from": old_status, "to": "paused"}, "reason": "pause"},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)  # 取回 server onupdate 的 updated_at
    await event_bus.publish(project.id, "status_changed", {"from": old_status, "to": "paused", "reason": "pause"})
    return project


async def resume_project(session: AsyncSession, project: Project, user_id: int) -> Project:
    """恢复暂停或失败的项目：还原 config 中记录的原始状态。

    - ``status=paused``：还原 ``config._paused_before``；
    - ``status=failed``：还原 ``config._failed_before``（失败恢复入口，
      修复 failed 项目无任何出边导致的死锁——测试报告 #29）。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        恢复后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40001 项目未处于暂停或失败状态。
    """
    if project.status == "paused":
        restore_key, from_status = "_paused_before", "paused"
    elif project.status == "failed":
        restore_key, from_status = "_failed_before", "failed"
    else:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "项目未处于暂停或失败状态，无法恢复")

    config = dict(project.config or {})
    before = config.pop(restore_key, None)
    project.config = config
    # 防御：记录值若本身是不可恢复状态（failed/paused/archived），回退 draft
    if not before or before in ("failed", "paused", "archived"):
        before = "draft"
    project.status = before
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"status": {"from": from_status, "to": project.status}, "reason": "resume"},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)  # 取回 server onupdate 的 updated_at
    await event_bus.publish(
        project.id, "status_changed", {"from": from_status, "to": project.status, "reason": "resume"}
    )
    return project


async def recover_failed_project(
    session: AsyncSession,
    project: Project,
    reason: str,
    user_id: int | None = None,
) -> Project:
    """从 failed 状态恢复项目（供 HITL 审批等流程自动调用，幂等）。

    与 :func:`resume_project` 的区别：本函数用于审批通过等系统侧恢复场景，
    对非 failed 状态直接原样返回（幂等），不抛错，便于调用方无条件前置调用。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        reason: 恢复原因（写入操作日志与事件）。
        user_id: 触发用户 ID（系统触发时可空）。

    Returns:
        恢复后的项目实例（非 failed 时原样返回，不提交）。

    Raises:
        BizError: 不抛出；恢复过程异常仅向上传播数据库错误。
    """
    if project.status != "failed":
        return project

    config = dict(project.config or {})
    before = config.pop("_failed_before", None)
    project.config = config
    if not before or before in ("failed", "paused", "archived"):
        before = "draft"
    old_status = project.status
    project.status = before
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"status": {"from": old_status, "to": project.status}, "reason": reason},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)
    await event_bus.publish(
        project.id, "status_changed", {"from": old_status, "to": project.status, "reason": reason}
    )
    return project


async def advance_chain(
    project: Project,
    chain: list[str],
    reason: str,
    *,
    session: AsyncSession,
    user_id: int | None = None,
) -> str:
    """按目标状态链依序推进项目状态（宽容失败，用于审批后的链式推进）。

    若当前状态位于链中，则从下一段开始推进；否则从头尝试。每段流转
    独立尝试：白名单不允许（40001）时记录告警并停止后续推进，已完成的
    段保留。修复审批 approve 后项目状态不推进（测试报告 #35）。

    Args:
        project: 项目 ORM 实例。
        chain: 目标状态序列（如 ``["post_producing", "final_review", "completed"]``）。
        reason: 流转原因（写入日志与事件）。
        session: 异步数据库会话（关键字参数）。
        user_id: 触发用户 ID（可空）。

    Returns:
        推进后的项目状态。

    Raises:
        BizError: 不抛出状态流转错误（内部捕获并告警）。
    """
    try:
        idx = chain.index(project.status)
        pending = chain[idx + 1:]
    except ValueError:
        pending = list(chain)
    for target in pending:
        try:
            await transition(project, target, reason, session=session, user_id=user_id)
        except BizError as exc:
            logger.warning(
                "链式推进 %s 失败 project_id=%s from=%s: %s",
                target, project.id, project.status, exc.message,
            )
            break
    return project.status


async def archive_project(session: AsyncSession, project: Project, user_id: int) -> Project:
    """归档项目（终态；归档前状态存入 config 供显式 unarchive 恢复，重复归档抛 40004）。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        归档后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40004 项目已归档。
    """
    if project.status == "archived":
        raise BizError(ErrorCode.PROJECT_ARCHIVED)

    old_status = project.status
    config = dict(project.config or {})
    config["_archived_before"] = old_status
    project.config = config
    project.status = "archived"
    project.archived_at = _utcnow()
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"status": {"from": old_status, "to": "archived"}, "reason": "archive"},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)  # 取回 server onupdate 的 updated_at
    await event_bus.publish(project.id, "status_changed", {"from": old_status, "to": "archived", "reason": "archive"})
    return project


async def unarchive_project(session: AsyncSession, project: Project, user_id: int) -> Project:
    """恢复归档项目（D16 修复：归档提供显式恢复路径，仅 Owner 可调用）。

    语义：还原 config 中 ``_archived_before`` 记录的归档前状态（历史归档
    无记录时回退 draft），清空 archived_at。与 resume（暂停恢复）同构的
    显式恢复动作，不改状态机白名单（archived 仍是流转终态，恢复必须
    走本专用入口并留痕）。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        user_id: 操作者用户 ID（Owner 校验由路由层保证）。

    Returns:
        恢复后的项目 ORM 实例（已提交）。

    Raises:
        BizError: 40001 项目未处于归档状态。
    """
    if project.status != "archived":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "项目未处于归档状态，无需恢复")

    config = dict(project.config or {})
    before = config.pop("_archived_before", None)
    project.config = config
    # 历史归档（无 _archived_before 记录）或脏值兜底回 draft
    project.status = before if before in STATUS_TRANSITIONS else "draft"
    project.archived_at = None
    project.lock_version += 1

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"status": {"from": "archived", "to": project.status}, "reason": "unarchive"},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)
    await event_bus.publish(project.id, "status_changed", {"from": "archived", "to": project.status, "reason": "unarchive"})
    return project


# =====================================================================
# 快照与回滚
# =====================================================================


async def create_snapshot(
    session: AsyncSession,
    project: Project,
    *,
    trigger: Literal["manual", "auto", "rollback"] = "manual",
    name: str | None = None,
    checkpoint_id: str | None = None,
    user_id: int | None = None,
) -> ProjectSnapshot:
    """为项目创建状态快照（上限 30，超限清理最旧的非手动快照）。

    快照 state 为项目关键字段 JSONB（status/config/budget/spent/
    current_script_id 等），并记录 ``_trigger`` 触发来源。手动快照
    （name 非空）不受清理影响；auto/rollback 快照（label 为空）在超限时
    从最旧开始清理。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        trigger: 触发来源 manual/auto/rollback。
        name: 快照名称（手动快照可命名；自动/回滚快照为空）。
        checkpoint_id: 关联 LangGraph checkpoint ID（可空）。
        user_id: 触发用户 ID（可空）。

    Returns:
        创建的快照 ORM 实例（已 flush，随外层事务提交）。
    """
    state = _project_state(project)
    state["_trigger"] = trigger
    state_bytes = len(json.dumps(state, ensure_ascii=False, default=str))

    snapshot = ProjectSnapshot(
        project_id=project.id,
        sequence_no=await ProjectRepository.next_sequence_no(session, project.id),
        label=name,
        state=state,
        checkpoint_id=checkpoint_id,
        created_by=user_id,
        size_bytes=state_bytes,
    )
    session.add(snapshot)
    await session.flush()
    await ProjectRepository.prune_snapshots(session, project.id, SNAPSHOT_LIMIT)
    return snapshot


async def list_snapshots(session: AsyncSession, project_id: int) -> dict[str, Any]:
    """查询项目快照列表（创建时间倒序，state 摘要）。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        ``{"items": [快照摘要...], "total": n}``。
    """
    snapshots = await ProjectRepository.list_snapshots(session, project_id)
    return {
        "items": [snapshot_out(s, include_state=False) for s in snapshots],
        "total": len(snapshots),
    }


async def rollback_snapshot(
    session: AsyncSession,
    project: Project,
    snapshot: ProjectSnapshot,
    user_id: int,
) -> dict[str, Any]:
    """按快照 state 恢复项目字段，并新建一条 rollback 快照供再次回滚。

    - 需 edit 权限（由路由 ACL 依赖保证）；
    - 快照状态与当前完全一致时幂等返回（不新建快照）；
    - 恢复后 lock_version 自增、写操作日志，并发布 SSE 事件
      （``project_rollback``；状态变化时同时发布 ``status_changed``）。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        snapshot: 目标快照 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        ``{"rolled_back", "message", "project", "snapshot"}``。

    Raises:
        BizError: 40004 项目已归档。
    """
    _ensure_not_archived(project)

    state = dict(snapshot.state or {})
    target_state = {k: v for k, v in state.items() if not k.startswith("_")}

    # 幂等：目标快照状态与当前一致则不重复回滚
    if target_state == _project_state(project):
        return {
            "rolled_back": False,
            "message": "项目已处于该快照状态，无需回滚",
            "project": project_out(project),
            "snapshot": None,
        }

    old_status = project.status
    for field in _SNAPSHOT_FIELDS:
        if field in target_state:
            setattr(project, field, target_state[field])
    archived_raw = target_state.get("archived_at")
    project.archived_at = datetime.fromisoformat(archived_raw) if archived_raw else None
    project.lock_version += 1

    # 回滚动作本身生成一条 rollback 快照（可再次回滚）
    rollback_snap = await create_snapshot(session, project, trigger="rollback", user_id=user_id)

    await _add_operation_log(
        session,
        project_id=project.id,
        entity_id=project.id,
        action="update",
        diff={"rollback": {"snapshot_id": snapshot.id, "from": old_status, "to": project.status}},
        user_id=user_id,
    )
    await session.commit()
    await session.refresh(project)  # 取回 server onupdate 的 updated_at
    await session.refresh(rollback_snap)

    await event_bus.publish(
        project.id,
        "project_rollback",
        {"snapshot_id": snapshot.id, "from": old_status, "to": project.status},
    )
    if old_status != project.status:
        await event_bus.publish(
            project.id,
            "status_changed",
            {"from": old_status, "to": project.status, "reason": "rollback"},
        )

    return {
        "rolled_back": True,
        "message": "已回滚至快照状态",
        "project": project_out(project),
        "snapshot": snapshot_out(rollback_snap),
    }


# =====================================================================
# 团队（P1 基础）
# =====================================================================


async def create_team(
    session: AsyncSession,
    user_id: int,
    payload: TeamCreateRequest,
) -> dict[str, Any]:
    """创建团队，创建者为 owner 并写入团队成员与审计日志。

    Args:
        session: 异步数据库会话。
        user_id: 创建者用户 ID。
        payload: 创建请求体（团队名称）。

    Returns:
        团队响应字典（my_role=owner）。
    """
    team = Team(name=payload.name, owner_user_id=user_id)
    session.add(team)
    await session.flush()
    session.add(TeamMember(team_id=team.id, user_id=user_id, role="owner"))
    await _add_audit_log(
        session,
        user_id=user_id,
        action="team.create",
        target_type="team",
        target_id=team.id,
        detail={"name": payload.name},
    )
    await session.commit()
    await session.refresh(team)
    return team_out(team, role="owner")


async def list_my_teams(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    """查询当前用户参与的全部团队（含角色）。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。

    Returns:
        团队响应字典列表。
    """
    rows = await TeamRepository.list_my_teams(session, user_id)
    return [team_out(team, role=role) for team, role in rows]


async def invite_team_member(
    session: AsyncSession,
    operator_id: int,
    team_id: int,
    payload: TeamMemberAddRequest,
) -> dict[str, Any]:
    """按手机号邀请用户加入团队（仅 owner/admin）。

    规则：
    - 操作者须为团队 owner 或 admin（否则 20003）；
    - 被邀请人手机号不存在 -> 10002；
    - 不能对 owner 执行任何角色操作 -> 20003；
    - 已是成员时更新其角色（邀请语义幂等）。

    Args:
        session: 异步数据库会话。
        operator_id: 操作者用户 ID。
        team_id: 团队 ID。
        payload: 邀请请求体（phone + role）。

    Returns:
        团队成员响应字典。

    Raises:
        BizError: 10002 团队/用户不存在；20003 无权限或目标为 owner。
    """
    team = await TeamRepository.get_team_by_id(session, team_id)
    if team is None:
        raise BizError(ErrorCode.NOT_FOUND, "团队不存在")
    operator_role = await TeamRepository.get_member_role(session, team_id, operator_id)
    if operator_role not in ("owner", "admin"):
        raise BizError(ErrorCode.FORBIDDEN, "仅团队 Owner/Admin 可邀请成员")

    target_user = (
        await session.execute(
            select(User).where(User.phone == payload.phone, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target_user is None:
        raise BizError(ErrorCode.NOT_FOUND, "被邀请用户不存在")
    if target_user.id == team.owner_user_id:
        raise BizError(ErrorCode.FORBIDDEN, "不能变更团队 Owner 的角色")

    member = await TeamRepository.get_team_member(session, team_id, target_user.id)
    if member is not None:
        member.role = payload.role
        member.invited_by = operator_id
    else:
        member = TeamMember(
            team_id=team_id,
            user_id=target_user.id,
            role=payload.role,
            invited_by=operator_id,
        )
        session.add(member)
    await _add_audit_log(
        session,
        user_id=operator_id,
        action="team.invite",
        target_type="team",
        target_id=team_id,
        detail={"target_user_id": target_user.id, "role": payload.role},
    )
    await session.commit()
    await session.refresh(member)
    return team_member_out(member, target_user)


async def remove_team_member(
    session: AsyncSession,
    operator_id: int,
    team_id: int,
    target_user_id: int,
) -> dict[str, Any]:
    """移除团队成员，并级联删除其在所属该团队项目上的全部授权。

    规则：
    - 操作者须为团队 owner 或 admin（否则 20003）；
    - 不能移除 owner（无论操作者是谁）-> 20003；
    - 被移除者不在团队中 -> 10002。

    Args:
        session: 异步数据库会话。
        operator_id: 操作者用户 ID。
        team_id: 团队 ID。
        target_user_id: 被移除用户 ID。

    Returns:
        ``{"removed": True, "revoked_project_grants": n}``。

    Raises:
        BizError: 10002 团队/成员不存在；20003 无权限或目标为 owner。
    """
    team = await TeamRepository.get_team_by_id(session, team_id)
    if team is None:
        raise BizError(ErrorCode.NOT_FOUND, "团队不存在")
    operator_role = await TeamRepository.get_member_role(session, team_id, operator_id)
    if operator_role not in ("owner", "admin"):
        raise BizError(ErrorCode.FORBIDDEN, "仅团队 Owner/Admin 可移除成员")
    if target_user_id == team.owner_user_id:
        raise BizError(ErrorCode.FORBIDDEN, "不能移除团队 Owner")

    member = await TeamRepository.get_team_member(session, team_id, target_user_id)
    if member is None:
        raise BizError(ErrorCode.NOT_FOUND, "该用户不是团队成员")

    await session.delete(member)
    revoked = await TeamRepository.remove_members_project_grants(session, team_id, target_user_id)
    await _add_audit_log(
        session,
        user_id=operator_id,
        action="team.member_remove",
        target_type="team",
        target_id=team_id,
        detail={"target_user_id": target_user_id, "revoked_project_grants": revoked},
    )
    await session.commit()
    return {"removed": True, "revoked_project_grants": revoked}


# =====================================================================
# 项目成员权限
# =====================================================================


async def set_project_permission(
    session: AsyncSession,
    project: Project,
    operator_id: int,
    target_user_id: int,
    payload: ProjectPermissionSetRequest,
) -> dict[str, Any]:
    """设置用户在项目上的权限（view/edit/export，upsert 语义）。

    规则：
    - 仅项目 Owner 可管理成员权限（防协作者自我提权/篡改）-> 20003；
    - 项目 Owner 权限固定为 export，不可变更 -> 20003；
    - 目标用户不存在 -> 10002。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        operator_id: 操作者用户 ID。
        target_user_id: 目标用户 ID。
        payload: 权限设置请求体。

    Returns:
        项目成员响应字典。

    Raises:
        BizError: 10002 用户不存在；20003 非 Owner 或目标为 Owner。
    """
    if operator_id != int(project.owner_user_id):
        raise BizError(ErrorCode.FORBIDDEN, "仅项目 Owner 可管理成员权限")
    if target_user_id == project.owner_user_id:
        raise BizError(ErrorCode.FORBIDDEN, "项目 Owner 权限固定，无需设置")

    target_user = (
        await session.execute(
            select(User).where(User.id == target_user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target_user is None:
        raise BizError(ErrorCode.NOT_FOUND, "目标用户不存在")

    member = await ProjectRepository.get_member(session, project.id, target_user_id)
    if member is not None:
        member.permission = payload.permission
        member.granted_by = operator_id
    else:
        member = ProjectMember(
            project_id=project.id,
            user_id=target_user_id,
            permission=payload.permission,
            granted_by=operator_id,
        )
        session.add(member)
    await _add_audit_log(
        session,
        user_id=operator_id,
        action="project.permission_set",
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        detail={"target_user_id": target_user_id, "permission": payload.permission},
    )
    await session.commit()
    await session.refresh(member)
    return project_member_out(member, target_user)


async def revoke_project_member(
    session: AsyncSession,
    project: Project,
    operator_id: int,
    target_user_id: int,
) -> dict[str, Any]:
    """撤销项目成员授权（D14 修复：补齐权限生命周期"撤销"一环）。

    规则：
    - 仅项目 Owner 可撤销成员授权 -> 20003；
    - 项目 Owner 权限固定，不可移除 -> 20003；
    - 目标用户不是项目成员 -> 10002；
    - 撤销后立即失效（下次访问走 ACL 判定无授权记录 -> 20003），审计留痕。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。
        operator_id: 操作者用户 ID。
        target_user_id: 目标用户 ID。

    Returns:
        ``{"revoked": True, "project_id", "user_id", "removed_permission"}``。

    Raises:
        BizError: 10002 目标不是项目成员；20003 非 Owner 或目标为 Owner。
    """
    if operator_id != int(project.owner_user_id):
        raise BizError(ErrorCode.FORBIDDEN, "仅项目 Owner 可管理成员权限")
    if target_user_id == project.owner_user_id:
        raise BizError(ErrorCode.FORBIDDEN, "项目 Owner 权限固定，无法移除")

    member = await ProjectRepository.get_member(session, project.id, target_user_id)
    if member is None:
        raise BizError(ErrorCode.NOT_FOUND, "该用户不是项目成员")
    removed_permission = member.permission

    await session.delete(member)
    await _add_audit_log(
        session,
        user_id=operator_id,
        action="project.permission_revoke",
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        detail={"target_user_id": target_user_id, "removed_permission": removed_permission},
    )
    await session.commit()
    return {
        "revoked": True,
        "project_id": int(project.id),
        "user_id": target_user_id,
        "removed_permission": removed_permission,
    }


async def list_project_members(session: AsyncSession, project: Project) -> dict[str, Any]:
    """查询项目成员列表（Owner 伪记录 + project_members 授权记录）。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。

    Returns:
        ``{"owner_user_id": ..., "items": [...]}``，items 按授权时间升序。
    """
    rows = await ProjectRepository.list_members(session, project.id)
    items: list[dict[str, Any]] = []
    owner = (
        await session.execute(select(User).where(User.id == project.owner_user_id))
    ).scalar_one_or_none()
    items.append(
        {
            "user_id": project.owner_user_id,
            "permission": "export",
            "granted_by": None,
            "nickname": owner.nickname if owner else None,
            "is_owner": True,
            "created_at": project.created_at.isoformat() if project.created_at else None,
        }
    )
    for member, user in rows:
        if member.user_id == project.owner_user_id:
            # owner 自身的授权记录合并到首行，避免重复
            items[0]["granted_by"] = member.granted_by
            continue
        entry = project_member_out(member, user)
        entry["is_owner"] = False
        items.append(entry)
    return {"owner_user_id": project.owner_user_id, "items": items}
