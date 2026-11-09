"""导出分发域路由模块（Task 9 · Export & Distribution）。

提供导出任务列表/创建（export ACL + Idempotency-Key 幂等）、任务详情
（下载 URL）、批量导出（ZIP 打包）、发行元数据生成（真实 LLM）、平台
授权 Stub OAuth（列表/授权/删除）、模拟分发与分发记录列表共 10 个端点。
所有响应统一 ``success_envelope`` 包络；导出类操作需 export 权限，
分发类操作需 edit+ 权限。

路由变量 ``router = APIRouter(tags=["export"])``（无 prefix，端点自带
完整路径），由 ``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id, project_guard, require_project_acl
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.project import Project
from app.schemas.export import (
    BatchExportRequest,
    DistributeRequest,
    ExportCreateRequest,
    ExportPresetRequest,
    ExportReuseRequest,
    MetadataGenerateRequest,
    PlatformAccountCreateRequest,
    SpecCheckRequest,
)
from app.services import export_service

#: 导出分发域路由（主控聚合使用：tags=[export]，路径自带前缀）
router = APIRouter(tags=["export"])


async def _export_job_guard(
    export_job_id: int, user_id: str, session: AsyncSession
) -> Project:
    """导出任务 ACL 守卫：按任务归属项目校验 view+ 权限。

    Args:
        export_job_id: 导出任务 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。

    Returns:
        任务归属项目实例。

    Raises:
        BizError: 10002 导出任务不存在；20003 无权限。
    """
    detail = await export_service.get_export_detail(session, export_job_id)
    project = await require_project_acl(
        int(detail["project_id"]), user_id, session, "view"
    )
    return project


@router.get("/projects/{project_id}/exports")
async def list_exports(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询项目导出任务列表（创建时间降序）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    data = await export_service.list_exports(session, project)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/exports")
async def create_export(
    payload: ExportCreateRequest,
    project: Annotated[Project, Depends(project_guard("export"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """创建单集导出任务（202 异步；同 Idempotency-Key 幂等回放）。

    流程：套餐权益校验（free→720p+强制水印 / pro→1080p / studio→4k+可
    去水印，超权益 → 70003）→ 时间线必须 approved（否则 40001）→ 落库
    pending → 后台生成 master 时间线 + 成片占位产物 → succeeded →
    发 ``export.done``。

    Args:
        payload: 请求体（resolution/with_watermark/episode_no）。
        project: ACL 校验通过的项目实例（需 export 权限）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        idempotency_key: 幂等键请求头（可选）。

    Returns:
        新提交返回 202；幂等命中返回 200（data.idempotent=True）。

    Raises:
        BizError: 70003 权益不足（data 携带 current_plan/required_benefit）；
            40001 时间线未审批。
    """
    data = await export_service.create_export(
        session,
        project,
        resolution=payload.resolution,
        with_watermark=payload.with_watermark,
        episode_no=payload.episode_no,
        idempotency_key=idempotency_key,
        preset_id=payload.preset_id,
        name_template=payload.name_template,
    )
    status_code = 200 if data.get("idempotent") else 202
    return JSONResponse(status_code=status_code, content=success_envelope(data, request_id))


@router.get("/exports/{export_job_id}")
async def get_export_detail(
    export_job_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询导出任务详情（含产物下载 URL /files/...）。

    Args:
        export_job_id: 导出任务 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为任务详情字典。

    Raises:
        BizError: 10002 导出任务不存在；20003 无权限。
    """
    detail = await export_service.get_export_detail(session, export_job_id)
    await require_project_acl(int(detail["project_id"]), user_id, session, "view")
    return success_envelope(detail, request_id)


@router.post("/projects/{project_id}/exports/batch")
async def batch_export(
    payload: BatchExportRequest,
    project: Annotated[Project, Depends(project_guard("export"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """批量导出：每集一个导出任务 + ZIP 打包占位成片（演示环境同步生成）。

    ZIP 条目名规则 ``{项目名}-EP{n}-{集标题}.mp4``（内容为导出元数据
    JSON 占位）。

    Args:
        payload: 请求体（resolution/with_watermark）。
        project: ACL 校验通过的项目实例（需 export 权限）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{zip_object_key, download_url, file_count,
        jobs}``。

    Raises:
        BizError: 70003 权益不足；40001 时间线未审批/无已渲染镜头。
    """
    data = await export_service.batch_export(
        session,
        project,
        resolution=payload.resolution,
        with_watermark=payload.with_watermark,
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/exports/metadata")
async def generate_metadata(
    payload: MetadataGenerateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """发行 Agent 生成发行元数据（真实 LLM，五段式提示词，不落库）。

    返回可编辑的 ``{title, description, tags, cover_suggestions}``，
    前端编辑后随分发请求提交。

    Args:
        payload: 请求体（当前为空对象，预留扩展）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{title, description, tags,
        cover_suggestions, episodes, source}``。
    """
    data = await export_service.generate_metadata(session, project)
    return success_envelope(data, request_id)


@router.get("/platform-accounts")
async def list_platform_accounts(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询当前用户全部平台授权（token 脱敏）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    data = await export_service.list_platform_accounts(session, int(user_id))
    return success_envelope(data, request_id)


@router.post("/platform-accounts")
async def authorize_platform(
    payload: PlatformAccountCreateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """平台授权（Stub OAuth）：创建 active 授权记录（token sha256 占位，
    有效期 30 天）。

    Args:
        payload: 请求体（platform/mock_authorize）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为授权字典。

    Raises:
        BizError: 10001 平台非法或未走模拟授权。
    """
    data = await export_service.authorize_platform(
        session, int(user_id), platform=payload.platform, mock_authorize=payload.mock_authorize
    )
    return success_envelope(data, request_id)


@router.delete("/platform-accounts/{account_id}")
async def delete_platform_account(
    account_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """删除平台授权（校验归属）。

    Args:
        account_id: 授权 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{id, deleted}``。

    Raises:
        BizError: 10002 授权不存在；20003 无权限删除他人授权。
    """
    data = await export_service.delete_platform_account(session, int(user_id), account_id)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/distribute")
async def distribute(
    payload: DistributeRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """分发到三方平台（模拟上传 2s，同步返回分发记录）。

    校验授权有效（过期 → 40001「授权已失效，请重新授权」）→
    distribution_records(succeeded, external_video_id=DEMO-*) 落库。

    Args:
        payload: 请求体（platform/metadata/export_job_id）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为分发记录字典。

    Raises:
        BizError: 10001 平台非法；40001 无有效授权/授权已失效；
            10002 导出任务不存在。
    """
    data = await export_service.distribute(
        session,
        project,
        int(user_id),
        platform=payload.platform,
        metadata=payload.metadata,
        export_job_id=payload.export_job_id,
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/distributions")
async def list_distributions(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询项目分发记录列表（创建时间降序）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    data = await export_service.list_distributions(session, project)
    return success_envelope(data, request_id)


# =====================================================================
# V4.0 导出分发域增量端点
# =====================================================================


@router.get("/projects/{project_id}/export-jobs")
async def get_export_jobs_history(
    project: Annotated[Project, Depends(project_guard("export"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    history: Annotated[bool, Query(description="是否历史视图")] = True,
) -> dict[str, Any]:
    """导出历史（AC-F7-06-1：时间/集数/参数摘要/文件状态，保留 90 天）。

    Args:
        project: ACL 校验通过的项目实例（需 export+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        history: 是否历史视图（True 按时间倒序并标记文件可下载性）。

    Returns:
        成功包络，data 为 {items, total, retention_days}。
    """
    data = await export_service.list_export_history(session, project, history=history)
    return success_envelope(data, request_id)


@router.post("/export-jobs/{job_id}/reuse")
async def post_export_reuse(
    job_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: ExportReuseRequest | None = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """复用历史导出参数重新导出（AC-F7-06-2）。

    同参数命中转码缓存时仅重打包不重转码；成片版本已更新时提示
    「将导出当前最新成片」。

    Args:
        job_id: 历史导出任务 ID。
        payload: 覆写参数（可空）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        idempotency_key: 幂等键请求头。

    Returns:
        202 包络，data 为 {export_job, reused_from, cache_hit,
        latest_timeline_changed}。

    Raises:
        BizError: 10002 历史记录不存在；20003 无权限。
    """
    from app.repositories.export_repo import ExportRepository

    source = await ExportRepository(session).get_export_job(job_id)
    if source is None:
        raise BizError(ErrorCode.NOT_FOUND, "历史导出记录不存在")
    project = await require_project_acl(int(source.project_id), user_id, session, "export")
    data = await export_service.reuse_export_job(
        session,
        project,
        job_id=job_id,
        resolution=(payload.resolution if payload else None),
        name_template=(payload.name_template if payload else None),
        idempotency_key=idempotency_key,
    )
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.post("/export-jobs/prespec-check")
async def post_prespec_check(
    payload: SpecCheckRequest,
    project_id: Annotated[int, Query(description="项目 ID（自检按项目成片时长判定）")],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """目标平台规格自检（AC-F7-07-1~2）。

    阻断项（时长上限/分辨率/画面比例）不通过时 ``blocked=True``，
    前端应禁用导出按钮并展示修改建议；规则库不可用时降级为通用基线。

    Args:
        payload: 自检请求体。
        project_id: 项目 ID（ACL 校验）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {platform, rules, checks, blocked, recommendations,
        degraded}。

    Raises:
        BizError: 10001 平台非法；20003 无权限。
    """
    project = await require_project_acl(project_id, user_id, session, "view")
    data = await export_service.prespec_check(
        session,
        project,
        platform=payload.platform,
        resolution=payload.resolution,
        episode_no=payload.episode_no,
    )
    return success_envelope(data, request_id)


@router.get("/export-presets")
async def get_export_presets(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """列出导出预设（上限 10 个，AC-F7-01-3）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {items, total, limit}。
    """
    data = await export_service.list_export_presets(session, int(user_id))
    return success_envelope(data, request_id)


@router.post("/export-presets")
async def post_export_preset(
    payload: ExportPresetRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """保存导出预设（同名覆盖，上限 10 个）。

    Args:
        payload: 预设请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {preset, total}。

    Raises:
        BizError: 70004 预设数量达上限。
    """
    data = await export_service.save_export_preset(session, int(user_id), payload)
    return success_envelope(data, request_id, message="导出预设已保存")


@router.delete("/export-presets/{preset_id}")
async def delete_export_preset(
    preset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """删除导出预设。

    Args:
        preset_id: 预设 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {preset_id, deleted}。

    Raises:
        BizError: 10002 预设不存在或非本人所有。
    """
    data = await export_service.delete_export_preset(session, int(user_id), preset_id)
    return success_envelope(data, request_id)


@router.post("/distributions/{record_id}/retry")
async def post_distribution_retry(
    record_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """分发重试（AC-F7-09-1）；授权过期类失败返回 ``need_reauth=True``。

    Args:
        record_id: 分发记录 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {record, need_reauth, retry_count}。

    Raises:
        BizError: 10002 记录不存在；40001 状态不允许重试；20003 无权限。
    """
    from sqlalchemy import select as sa_select

    from app.models.distribution import DistributionRecord

    record = (
        await session.execute(
            sa_select(DistributionRecord).where(DistributionRecord.id == record_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise BizError(ErrorCode.NOT_FOUND, "分发记录不存在")
    project = await require_project_acl(int(record.project_id), user_id, session, "edit")
    data = await export_service.retry_distribution(
        session, project, record_id=record_id, user_id=int(user_id)
    )
    return success_envelope(data, request_id)


@router.post("/distributions/{record_id}/withdraw")
async def post_distribution_withdraw(
    record_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """分发撤回（AC-F7-09-1，依平台能力；不支持时标记需后台手动删除）。

    Args:
        record_id: 分发记录 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {record, platform_capable, message}。

    Raises:
        BizError: 10002 记录不存在；40001 状态不允许撤回；20003 无权限。
    """
    from sqlalchemy import select as sa_select

    from app.models.distribution import DistributionRecord

    record = (
        await session.execute(
            sa_select(DistributionRecord).where(DistributionRecord.id == record_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise BizError(ErrorCode.NOT_FOUND, "分发记录不存在")
    project = await require_project_acl(int(record.project_id), user_id, session, "edit")
    data = await export_service.withdraw_distribution(
        session, project, record_id=record_id, user_id=int(user_id)
    )
    return success_envelope(data, request_id)