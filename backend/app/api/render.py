"""生成执行域路由模块（Task 8 · Render）。

提供渲染成本估算、批量提交（Idempotency-Key 幂等）、任务分页列表、
任务详情、取消、重试、单镜重新生成与渲染事件 SSE 订阅共 8 个端点。
所有响应统一 ``success_envelope`` 包络；写操作经项目级 ACL（owner 或
project_members 三级权限），任务级端点按任务归属项目校验权限。

路由变量 ``router = APIRouter(tags=["render"])``（无 prefix，端点自带
完整路径），由 ``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id, project_guard, require_project_acl
from app.core.errors import success_envelope
from app.infra.event_bus import event_bus
from app.models.project import Project
from app.models.render import RenderTask
from app.repositories.render_repo import RenderRepository
from app.schemas.render import (
    BatchRenderRequest,
    BatchRetryRequest,
    EpisodeLockRequest,
    ManualPassRequest,
    QueueJumpRequest,
    ShotVersionLabelRequest,
)
from app.services import project_service, render_service

#: 生成执行域路由（主控聚合使用：tags=[render]，路径自带前缀）
router = APIRouter(tags=["render"])


async def _task_guard(
    task_id: int,
    user_id: str,
    session: AsyncSession,
    need: str,
) -> RenderTask:
    """渲染任务 ACL 守卫：加载任务并按归属项目校验权限。

    Args:
        task_id: 渲染任务 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        归属校验通过的渲染任务 ORM 实例。

    Raises:
        BizError: 60002 任务不存在；20003 无权限。
    """
    task = await RenderRepository(session).get_task(task_id)
    if task is None:
        from app.core.errors import BizError, ErrorCode

        raise BizError(ErrorCode.TASK_NOT_FOUND)
    await require_project_acl(int(task.project_id), user_id, session, need)  # type: ignore[arg-type]
    return task


@router.get("/projects/{project_id}/render/estimate")
async def estimate_render(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    shot_ids: Annotated[str | None, Query(description="逗号分隔镜头 ID；空=全部锁定镜头")] = None,
) -> dict[str, Any]:
    """按镜头估算渲染成本（视频单价 = ceil(时长秒) × 路由模型单价）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        shot_ids: 逗号分隔镜头 ID（如 ``1,2,3``）；空表示全部锁定镜头。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{per_shot, total_credits, shot_count,
        available_credits}``。

    Raises:
        BizError: 60001 无可用锁定镜头。
    """
    ids = (
        [int(x) for x in shot_ids.split(",") if x.strip().isdigit()]
        if shot_ids
        else None
    )
    data = await render_service.estimate(session, project, ids)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/render-tasks/batch")
async def batch_submit_render(
    payload: BatchRenderRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """批量提交渲染任务（202 异步模型；同 Idempotency-Key 幂等回放）。

    body ``shot_ids`` 为空时提交该项目全部 status=locked 的镜头；
    未锁定/缺提示词的镜头跳过并记入 warnings；全部不可用 → 60001。

    Args:
        payload: 请求体（shot_ids 可选）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        idempotency_key: 幂等键请求头（必填）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        新提交返回 202；幂等命中返回 200（data.idempotent=True）。

    Raises:
        BizError: 10001 缺少幂等键；60001 无可用锁定镜头；
            30001 积分不足（data 携带 needed/available）。
    """
    data = await render_service.batch_submit(
        session, project, int(user_id), payload.shot_ids, idempotency_key,
        strategy_tier=payload.strategy_tier, max_credits=payload.max_credits,
    )
    # D28 修复：渲染批次受理即推进项目状态 storyboard_review → rendering
    # （幂等命中为重放，不重复推进；状态机白名单外状态自动跳过）
    if not data.get("idempotent") and project.status == "storyboard_review":
        await project_service.transition(
            project, "rendering", "渲染批次受理", session=session, user_id=int(user_id)
        )
    status_code = 200 if data.get("idempotent") else 202
    return JSONResponse(status_code=status_code, content=success_envelope(data, request_id))


@router.get("/projects/{project_id}/render-tasks")
async def list_render_tasks(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    status: Annotated[str | None, Query(description="任务状态筛选")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query(description="-created_at / created_at")] = "-created_at",
) -> dict[str, Any]:
    """分页查询项目渲染任务列表（含镜头摘要、成本、failure_type）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        status: 状态筛选。
        page: 页码。
        page_size: 每页条数。
        sort: 排序。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, page, page_size, total}``。

    Raises:
        BizError: 10001 分页/排序参数非法。
    """
    data = await render_service.list_tasks(
        session, project, status=status, page=page, page_size=page_size, sort=sort,
    )
    return success_envelope(data, request_id)


@router.get("/render-tasks/{task_id}")
async def get_render_task(
    task_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询渲染任务详情（含 outputs、qc_reports、冻结/结算状态）。

    Args:
        task_id: 渲染任务 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为任务详情字典。

    Raises:
        BizError: 60002 任务不存在；20003 无权限。
    """
    task = await _task_guard(task_id, user_id, session, "view")
    project = Project(id=task.project_id)
    data = await render_service.get_task_detail(session, project, task_id)
    return success_envelope(data, request_id)


@router.post("/render-tasks/{task_id}/cancel")
async def cancel_render_task(
    task_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """取消渲染任务（仅 pending/queued 可取消；running → 40001）。

    取消后全额退款，镜头回 locked，发布 ``render.task.updated`` 事件。

    Args:
        task_id: 渲染任务 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{task_id, status, refunded_credits,
        shot_status}``。

    Raises:
        BizError: 60002 任务不存在；40001 状态不可取消；20003 无权限。
    """
    task = await _task_guard(task_id, user_id, session, "edit")
    project = Project(id=task.project_id)
    data = await render_service.cancel_task(session, project, task_id)
    return success_envelope(data, request_id)


@router.post("/render-tasks/{task_id}/retry")
async def retry_render_task(
    task_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """重试渲染任务（failed/manual_required/cancelled → queued，202）。

    复用未结算冻结或重新预检冻结差额；retry_count ≥ max_retries → 60004。

    Args:
        task_id: 渲染任务 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 包络，data 为 ``{task_id, status, retry_count,
        reused_freeze}``。

    Raises:
        BizError: 60002 任务不存在；40001 状态不允许重试；60004 超限；
            30001 重新冻结积分不足；20003 无权限。
    """
    task = await _task_guard(task_id, user_id, session, "edit")
    # 2026-11-04 QA 回归修复：这里曾用 Project(id=task.project_id) 构造仅含
    # id 的轻量实例，冻结已释放（4 轮执行器重试耗尽后的 manual_required 任务
    # 必然如此）时 retry_task 会读 project.owner_user_id 重新预检冻结，读到
    # None 直接 TypeError → 500（10003）。改为完整加载项目记录。
    from app.core.errors import BizError, ErrorCode  # 与本文件其他端点一致的局部导入

    project = await session.get(Project, task.project_id)
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")
    data = await render_service.retry_task(session, project, task_id)
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.post("/shots/{shot_id}/regenerate")
async def regenerate_shot(
    shot_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """单镜重新生成（等价 batch 单镜；新任务、旧任务保留，202）。

    Args:
        shot_id: 镜头 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 包络，data 为 batch 单镜结果（含 batch_key）。

    Raises:
        BizError: 10002 镜头不存在；40001 镜头状态不允许；30001 积分不足；
            20003 无权限。
    """
    from sqlalchemy import select

    from app.core.errors import BizError, ErrorCode
    from app.models.storyboard import Shot

    shot = (
        await session.execute(select(Shot).where(Shot.id == shot_id))
    ).scalar_one_or_none()
    if shot is None:
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")
    project = await require_project_acl(int(shot.project_id), user_id, session, "edit")
    data = await render_service.regenerate_shot(session, project, int(user_id), shot_id)
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.get("/projects/{project_id}/render-tasks/events")
async def render_task_events(
    project: Annotated[Project, Depends(project_guard("view"))],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """渲染任务 SSE 事件流（转发该项目 ``render.*`` 前缀事件，需 view+）。

    事件类型：``render.task.updated``（{task_id, shot_id, status, attempt,
    progress?}）/ ``render.batch.completed``（{succeeded, failed, total,
    cost}）。空闲 15s 注入心跳注释行；携带 Last-Event-ID 先回放断线期间
    事件再实时推送。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        last_event_id: 客户端最后收到的事件序号（EventSource 自动携带）。

    Returns:
        text/event-stream 的 StreamingResponse。
    """
    last_id: int | None = None
    if last_event_id and last_event_id.strip().isdigit():
        last_id = int(last_event_id.strip())
    project_id = int(project.id)

    async def event_stream() -> AsyncIterator[str]:
        """SSE 事件生成器：订阅总线并过滤 render.* 前缀事件。

        Yields:
            SSE 帧字符串（事件帧或心跳注释行）。
        """
        try:
            async for record in event_bus.subscribe(project_id, last_id):
                if record.get("heartbeat"):
                    yield ": ping\n\n"
                    continue
                if not str(record.get("event", "")).startswith("render."):
                    continue
                data = json.dumps(record["data"], ensure_ascii=False, default=str)
                yield f"id: {record['id']}\nevent: {record['event']}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =====================================================================
# V4.0 生成执行域增量端点
# =====================================================================


@router.get("/projects/{project_id}/render-dashboard")
async def get_render_dashboard(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """生成总览仪表盘：六态计数 + 总进度 + ETA + 实时消耗（AC-F5-07-1~2）。

    ETA 按近 10 分钟完成速率滚动估算；速率波动 > 50% 时 ``eta_volatile=True``
    并提示「波动较大」，不给精确到秒的承诺。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {counts, total, progress_percent, eta_seconds,
        eta_volatile, consumed_credits, available_credits, rate_per_minute}。
    """
    data = await render_service.get_dashboard(session, project)
    return success_envelope(data, request_id)


@router.post("/render-tasks/batch-retry")
async def post_batch_retry(
    payload: BatchRetryRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """失败镜头批量重试（AC-F5-08-1~2）。

    统一以新幂等键建任务、按预估重新冻结；余额不足与
    ``moderation_rejected`` 类型跳过并说明原因；``delay_minutes > 0``
    时登记延迟定时批量重试（倒计时可取消）。

    Args:
        payload: 批量重试请求体（task_ids / delay_minutes）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 包络，data 为 {accepted, skipped, delayed_task_ids, delay_minutes}。

    Raises:
        BizError: 60002 任务不存在；20003 无权限。
    """
    first_task = await render_service.get_task_any_project(session, payload.task_ids[0])
    project = await require_project_acl(int(first_task.project_id), user_id, session, "edit")
    data = await render_service.batch_retry(
        session,
        project,
        task_ids=payload.task_ids,
        delay_minutes=payload.delay_minutes,
        user_id=int(user_id),
    )
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.get("/render-delayed-retries")
async def get_delayed_retries(
    user_id: Annotated[str, Depends(get_current_user)],
    project_id: Annotated[int | None, Query(description="项目筛选")] = None,
) -> dict[str, Any]:
    """列出进行中的延迟批量重试（倒计时展示用）。

    Args:
        user_id: 当前用户 ID（鉴权）。
        project_id: 项目筛选。

    Returns:
        成功包络，data 为 {items, total}。
    """
    from app.tasks.executor import render_executor

    items = render_executor.list_delayed_retries(project_id)
    return success_envelope({"items": items, "total": len(items)})


@router.delete("/render-delayed-retries/{job_id}")
async def delete_delayed_retry(
    job_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
) -> dict[str, Any]:
    """取消一次已登记的延迟批量重试。

    Args:
        job_id: 延迟任务标识。
        user_id: 当前用户 ID（鉴权）。

    Returns:
        成功包络，data 为 {job_id, cancelled}。

    Raises:
        BizError: 10002 延迟任务不存在。
    """
    from app.tasks.executor import render_executor

    cancelled = render_executor.cancel_delayed_retry(job_id)
    if not cancelled:
        from app.core.errors import BizError, ErrorCode

        raise BizError(ErrorCode.NOT_FOUND, "延迟重试任务不存在或已执行")
    return success_envelope({"job_id": job_id, "cancelled": True})


@router.post("/render-tasks/{task_id}/manual-pass")
async def post_manual_pass(
    task_id: int,
    payload: ManualPassRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """人工标记合格，强制通过质检（V4.0 P0 SF-F5-11 / AC-F5-09-1~3）。

    必须勾选确认声明并填写放行理由（≥5 字）；通过后进入正常结算，
    原始质检得分保留，操作写 ``audit_logs``；同一镜头仅一次强制通过；
    ``manual_pass`` 标记使该镜头从 M4 一致性分母排除。

    Args:
        task_id: 渲染任务 ID。
        payload: 放行请求体（reason / acknowledged）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {task_id, status, manual_pass, settled_credits,
        shot_status}。

    Raises:
        BizError: 10001 未勾选确认/理由不足；40001 状态不允许或已用过；
            60002 任务不存在；20003 无权限。
    """
    task = await _task_guard(task_id, user_id, session, "edit")
    project = Project(id=task.project_id)
    data = await render_service.manual_pass_task(
        session,
        project,
        task_id,
        reason=payload.reason,
        acknowledged=payload.acknowledged,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id, message="已人工标记合格")


@router.post("/render-tasks/{task_id}/abandon")
async def post_abandon_task(
    task_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """放弃失败/待人工任务并全额解冻返还（AC-F5-04-3）。

    Args:
        task_id: 渲染任务 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {task_id, status, refunded_credits, shot_status}。

    Raises:
        BizError: 40001 状态不允许；60002 任务不存在；20003 无权限。
    """
    task = await _task_guard(task_id, user_id, session, "edit")
    project = Project(id=task.project_id)
    data = await render_service.abandon_task(session, project, task_id)
    return success_envelope(data, request_id, message="任务已放弃，冻结积分已返还")


@router.post("/episodes/{episode_id}/lock-versions")
async def post_episode_lock_versions(
    episode_id: int,
    payload: EpisodeLockRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按集一键锁定全部镜头版本（AC-F5-10-1~2）。

    默认锁定每镜头质检得分最高版本，可逐镜头改选；集内含待人工或失败
    镜头时拒绝并列出例外项。

    Args:
        episode_id: 集 ID。
        payload: 锁定请求体（selections）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {episode_id, locked, exceptions}。

    Raises:
        BizError: 10002 集不存在；40001 存在例外镜头（列出清单）；20003 无权限。
    """
    episode_project_id = await render_service.get_episode_project_id(session, episode_id)
    project = await require_project_acl(episode_project_id, user_id, session, "edit")
    data = await render_service.lock_episode_versions(
        session, project, episode_id, selections=payload.selections, user_id=int(user_id)
    )
    return success_envelope(data, request_id, message="本集镜头版本已锁定")


@router.post("/render-queues/{batch_key}/jump")
async def post_queue_jump(
    batch_key: str,
    payload: QueueJumpRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """单镜头插队：优先级升至当前批次队首（每批次免费 3 次，AC-F5-01-3）。

    Args:
        batch_key: 批次键。
        payload: 插队请求体（task_id）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {task_id, priority, priority_source, jump_used,
        free_quota}。

    Raises:
        BizError: 60002 任务不存在；40001 已生成中/状态不允许；
            60004 本批次插队额度已用完；20003 无权限。
    """
    jump_task = await render_service.get_task_any_project(session, payload.task_id)
    project = await require_project_acl(int(jump_task.project_id), user_id, session, "edit")
    data = await render_service.jump_queue(
        session, project, batch_key=batch_key, task_id=payload.task_id, user_id=int(user_id)
    )
    return success_envelope(data, request_id, message="已插队至队首")


@router.patch("/shot-versions/{version_id}")
async def patch_shot_version_label(
    version_id: int,
    payload: ShotVersionLabelRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """给镜头版本打标签/备注（AC-F5-03-3：预设标签 + 备注 ≤100 字）。

    Args:
        version_id: 镜头版本 ID。
        payload: 标签请求体（label / note）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {version_id, label, note}。

    Raises:
        BizError: 10002 版本不存在。
    """
    data = await render_service.label_shot_version(
        session,
        version_id=version_id,
        label=payload.label,
        note=payload.note,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)