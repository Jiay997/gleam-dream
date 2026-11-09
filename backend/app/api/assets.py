"""资产域路由模块（Task 7b）。

提供资产规划（LLM 提取清单）、形象生成/重生成（202 异步任务模型）、
人工审核（approve/reject/lock）、音色绑定、真人授权留痕、锚点影响面
统计与音色库查询。所有响应统一 ``success_envelope`` 包络；写操作经
项目级 ACL（owner 或 project_members 三级权限）。

路由变量 ``router = APIRouter(tags=["assets"])``（无 prefix，端点自带
完整路径），由 ``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id, project_guard, require_project_acl
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.asset import Asset
from app.models.project import Project
from app.schemas.asset import (
    AssetGenerateRequest,
    AssetLabelsRequest,
    AssetRejectRequest,
    BatchRegenerateRequest,
    FaceAuthorizationRequest,
    FaceAuthorizationRevokeRequest,
    SceneVariantRequest,
    VoiceBindRequest,
    VoicePreviewRequest,
)
from app.services import asset_service, tts_voice_service

#: 资产域路由（主控聚合使用：tags=[assets]，路径自带前缀）
router = APIRouter(tags=["assets"])


async def _asset_guard(
    asset_id: int,
    user_id: str,
    session: AsyncSession,
    need: str,
) -> Asset:
    """单资产 ACL 守卫：加载资产并按其归属项目校验权限。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        校验通过的资产 ORM 实例。

    Raises:
        BizError: 10002 资产不存在；20003 无权限。
    """
    asset = await asset_service.get_asset_or_error(session, asset_id)
    if asset.project_id:
        await require_project_acl(int(asset.project_id), user_id, session, need)  # type: ignore[arg-type]
    return asset


def _accepted(data: dict[str, Any], request_id: str) -> JSONResponse:
    """构造 202 受理响应（统一包络）。

    Args:
        data: 业务数据。
        request_id: 请求 ID。

    Returns:
        HTTP 202 的 JSONResponse。
    """
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.post("/projects/{project_id}/assets/plan")
async def plan_assets(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """发起资产规划：后台 LLM 从剧本提取角色/场景/道具清单（202）。

    重复调用幂等（同类型同名资产复用不重复创建）；完成后发布
    ``assets.planned`` 事件。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 统一包络，data 为受理回执。

    Raises:
        BizError: 10002 项目尚无剧本。
    """
    data = await asset_service.plan_assets(session, project, int(user_id))
    return _accepted(data, request_id)


@router.post("/projects/{project_id}/assets/cleanup-planning")
async def cleanup_planning_assets(
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """清理项目内滞留的 planning 资产（P1-4 修复）。

    用途：规划异常中断/重复提交留下的 planning 资产不参与生成与锁定，
    却会永久阻塞「全部实体资产已锁定 → 资产审核」的状态推进。本端点
    物理删除这些未产出任何版本、未被锚点引用的空壳资产。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{project_id, deleted, asset_ids}``。

    Raises:
        BizError: 20003 无权限。
    """
    data = await asset_service.purge_planning_assets(session, int(project.id))
    return success_envelope(data, request_id)



@router.get("/projects/{project_id}/assets")
async def list_assets(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    asset_type: str | None = Query(default=None, max_length=32, description="类型筛选"),
    status: str | None = Query(default=None, max_length=32, description="状态筛选"),
    search: str | None = Query(default=None, max_length=64, description="名称/标签关键词"),
    labels: str | None = Query(default=None, description="标签多选（逗号分隔，AND 匹配）"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
) -> dict[str, Any]:
    """分页查询项目资产列表（类型/状态/关键词/标签筛选）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        asset_type: 资产类型筛选。
        status: 状态筛选。
        search: 名称/标签关键词（AC-F3-11-1）。
        labels: 标签多选（逗号分隔，AND 匹配）。
        page: 页码。
        page_size: 每页条数。

    Returns:
        成功包络，data 为 ``{"items", "page", "page_size", "total"}``。
    """
    label_list = [item.strip() for item in labels.split(",") if item.strip()] if labels else None
    data = await asset_service.list_assets(
        session, int(project.id),
        asset_type=asset_type, status=status, page=page, page_size=page_size,
        search=search, labels=label_list,
    )
    return success_envelope(data, request_id)


@router.get("/assets/{asset_id}")
async def get_asset(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询资产详情（详情 + 版本 + QC 报告 + 音色 + 引用镜头数）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为资产详情字典。

    Raises:
        BizError: 10002 资产不存在；20003 无权限。
    """
    await _asset_guard(asset_id, user_id, session, "view")
    data = await asset_service.get_asset_detail(session, asset_id)
    return success_envelope(data, request_id)


@router.post("/assets/{asset_id}/generate")
async def generate_asset(
    asset_id: int,
    payload: AssetGenerateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """发起资产生成（202 异步任务模型，后台 stub 文生图 + 伪向量 QC）。

    Args:
        asset_id: 资产 ID。
        payload: 生成请求体（version_type + 可选 feedback）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 统一包络，data 为受理回执。

    Raises:
        BizError: 10001 版本类型不匹配；40001 状态不允许；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.generate_version(session, asset, payload)
    return _accepted(data, request_id)


@router.post("/assets/{asset_id}/regenerate")
async def regenerate_asset(
    asset_id: int,
    payload: AssetGenerateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """重新生成资产（同 generate，新版本产生且旧版本保留）。

    Args:
        asset_id: 资产 ID。
        payload: 生成请求体（version_type + 可选 feedback）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 统一包络，data 为受理回执。

    Raises:
        BizError: 同 generate。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.regenerate_version(session, asset, payload)
    return _accepted(data, request_id)


@router.post("/assets/{asset_id}/approve")
async def approve_asset(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """审核通过资产（pending_review → approved）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的资产字典。

    Raises:
        BizError: 40001 状态不允许；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.approve_asset(session, asset, int(user_id))
    return success_envelope(data, request_id, message="资产已通过审核")


@router.post("/assets/{asset_id}/reject")
async def reject_asset(
    asset_id: int,
    payload: AssetRejectRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """驳回资产（feedback ≥10 字，status=rejected，反馈存 meta）。

    Args:
        asset_id: 资产 ID。
        payload: 驳回请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的资产字典。

    Raises:
        BizError: 10001 反馈不足 10 字；40001 状态不允许；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.reject_asset(session, asset, payload, int(user_id))
    return success_envelope(data, request_id, message="资产已驳回")


@router.post("/assets/{asset_id}/lock")
async def lock_asset(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """锁定资产作为生成锚点（须版本齐全且 QC 通过，否则 60003）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为锁定后的资产字典。

    Raises:
        BizError: 60003 版本缺失/QC 未通过；40001 已锁定；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.lock_asset(session, asset, int(user_id))
    return success_envelope(data, request_id, message="资产已锁定")


@router.get("/assets/{asset_id}/versions")
async def list_asset_versions(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询资产版本列表（版本号倒序，含媒体 URL）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"items", "total"}``。

    Raises:
        BizError: 10002 资产不存在；20003 无权限。
    """
    await _asset_guard(asset_id, user_id, session, "view")
    detail = await asset_service.get_asset_detail(session, asset_id)
    return success_envelope(
        {"items": detail["versions"], "total": len(detail["versions"])}, request_id,
    )


@router.put("/assets/{asset_id}/voice")
async def bind_voice(
    asset_id: int,
    payload: VoiceBindRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """为角色资产绑定 TTS 音色（音色须存在且 available）。

    Args:
        asset_id: 资产 ID。
        payload: 绑定请求体（voice_id）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的资产字典。

    Raises:
        BizError: 10001 音色不可用/非角色资产；10002 音色不存在；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.bind_voice(session, asset, payload.voice_id)
    return success_envelope(data, request_id, message="音色已绑定")


@router.get("/tts-voices")
async def list_tts_voices(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    status: str | None = Query(default="available", max_length=32, description="状态筛选"),
    gender: str | None = Query(default=None, max_length=16, description="性别筛选 male/female/neutral"),
    exclude_test: bool = Query(default=True, description="是否排除自动化测试音色"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
) -> dict[str, Any]:
    """分页查询音色库（默认仅可用音色，并排除测试数据）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        status: 状态筛选（传空串查全部）。
        gender: 性别筛选（male/female/neutral）。
        exclude_test: 是否排除自动化测试音色（默认 true）。
        page: 页码。
        page_size: 每页条数。

    Returns:
        成功包络，data 为 ``{"items", "page", "page_size", "total"}``。
    """
    data = await asset_service.list_tts_voices(
        session,
        status=status or None,
        page=page,
        page_size=page_size,
        gender=gender or None,
        exclude_test=exclude_test,
    )
    return success_envelope(data, request_id)


@router.get("/tts-voices/{voice_id}")
async def get_tts_voice(
    voice_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询单个音色详情（含试听样音、绑定角色与引擎音色映射）。

    Args:
        voice_id: 音色 ID（``tts_voices.id``）。
        user_id: 当前用户 ID（仅用于登录鉴权）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为音色详情字典。

    Raises:
        BizError: 音色不存在（NOT_FOUND）。
    """
    data = await tts_voice_service.get_voice_detail(session, voice_id)
    return success_envelope(data, request_id)


@router.post("/tts-voices/{voice_id}/preview")
async def preview_tts_voice(
    voice_id: int,
    payload: VoicePreviewRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """合成音色试听样音（实时合成，不写入配音时间线）。

    Args:
        voice_id: 音色 ID。
        payload: 试听文本、语速与音调。
        user_id: 当前用户 ID（仅用于登录鉴权）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 含 ``audio_url``、``duration_ms``、``engine_voice`` 等。

    Raises:
        BizError: 音色不存在、不可用、文本非法或模型合成失败。
    """
    data = await tts_voice_service.synthesize_preview(
        session,
        voice_id,
        text=payload.text,
        rate=payload.rate,
        pitch=payload.pitch,
    )
    return success_envelope(data, request_id, message="试听音频已生成")


@router.post("/tts-voices/{voice_id}/regenerate")
async def regenerate_tts_voice(
    voice_id: int,
    payload: VoicePreviewRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """重新生成音色样音并持久化到音色库。

    Args:
        voice_id: 音色 ID。
        payload: 试听文本、语速与音调（可传空对象使用默认示例句）。
        user_id: 当前用户 ID（仅用于登录鉴权）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为新样音结果（``persisted=True``）。

    Raises:
        BizError: 音色不存在、不可用、文本非法或模型合成失败。
    """
    data = await tts_voice_service.regenerate_voice_sample(
        session,
        voice_id,
        text=payload.text,
        rate=payload.rate,
        pitch=payload.pitch,
    )
    return success_envelope(data, request_id, message="音色样音已重新生成")


@router.post("/assets/{asset_id}/face-authorization")
async def create_face_authorization(
    asset_id: int,
    payload: FaceAuthorizationRequest,
    request: Request,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入真人照片授权留痕（IP/UA/协议版本/时间戳存证）。

    Args:
        asset_id: 资产 ID。
        payload: 授权请求体（authorization_type + agreement_version）。
        request: 当前请求对象（提取客户端 IP 与 UA）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为授权记录字典。

    Raises:
        BizError: 10001 授权类型非法；10002 资产不存在；20003 无权限。
    """
    await _asset_guard(asset_id, user_id, session, "edit")
    asset = await asset_service.get_asset_or_error(session, asset_id)
    client_ip = request.client.host if request.client else None
    data = await asset_service.create_face_authorization(
        session, asset, payload.authorization_type, payload.agreement_version,
        client_ip, request.headers.get("user-agent"), int(user_id),
    )
    return success_envelope(data, request_id, message="授权记录已留存")


@router.get("/assets/{asset_id}/anchor-impact")
async def get_anchor_impact(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """统计资产作为锚点出现在多少镜头（character_asset_ids 包含匹配）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"asset_id", "affected_shots"}``。

    Raises:
        BizError: 10002 资产不存在；20003 无权限。
    """
    await _asset_guard(asset_id, user_id, session, "view")
    data = await asset_service.get_anchor_impact(session, asset_id)
    return success_envelope(data, request_id)


# =====================================================================
# V4.0 资产域增量端点
# =====================================================================


@router.patch("/assets/{asset_id}/labels")
async def patch_asset_labels(
    asset_id: int,
    payload: AssetLabelsRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """更新资产可检索标签（AC-F3-11-1~2，V4.0）。

    标签约束：≤10 个/资产、单标签 ≤12 字；用于资产墙搜索与批量定位。

    Args:
        asset_id: 资产 ID。
        payload: 标签请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的资产字典。

    Raises:
        BizError: 10001 标签超限；10002 资产不存在；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.update_asset_labels(
        session, asset, payload.labels, int(user_id)
    )
    return success_envelope(data, request_id, message="标签已更新")


@router.get("/assets/{asset_id}/regen-estimate")
async def get_regen_estimate(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """重抽成本透明预估（V4.0 P0 SF-F3-12 / AC-F3-03-2）。

    返回「本次预估 X 积分 + 该角色已累计消耗 Y 积分 / 已重抽 N 次」；
    累计消耗超成本基线 2 倍时追加「建议换描述策略」提示（不阻断）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为成本预估字典。

    Raises:
        BizError: 10002 资产不存在；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "view")
    data = await asset_service.estimate_regenerate_cost(session, asset)
    return success_envelope(data, request_id)


@router.post("/assets/batch-regenerate")
async def post_batch_regenerate(
    payload: BatchRegenerateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """批量重抽：多角色应用同一条反馈（AC-F3-10-1~2，V4.0）。

    ``confirm=false`` 只返回成本预估（「N 个角色 × 单价 = 预估积分」）；
    ``confirm=true`` 逐角色独立执行，真人授权已撤回的角色单独拦截并说明原因，
    单角色失败不阻塞其余。项目上下文由首个资产反查（批量资源自带归属）。

    Args:
        payload: 批量重抽请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        ``confirm=false`` 返回 200 预估；``confirm=true`` 返回 202 受理。

    Raises:
        BizError: 10001 跨项目资产/版本类型不匹配；10002 资产不存在；
            20003 无权限。
    """
    first = await asset_service.get_asset_or_error(session, payload.asset_ids[0])
    if first.project_id is None:
        raise BizError(ErrorCode.PARAM_INVALID, "跨项目资产不支持批量重抽")
    project = await require_project_acl(int(first.project_id), user_id, session, "edit")
    data = await asset_service.batch_regenerate(session, project, payload, int(user_id))
    if data.get("confirmed"):
        return _accepted(data, request_id)
    return JSONResponse(status_code=200, content=success_envelope(data, request_id))


@router.post("/assets/{asset_id}/variants")
async def post_asset_variants(
    asset_id: int,
    payload: SceneVariantRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """为场景资产生成时段/天气变体（AC-F3-08-1，V4.0）。

    每个变体生成独立 asset_version 并以场景 × 变体命名；同类变体最多 2 项。

    Args:
        asset_id: 资产 ID。
        payload: 变体请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 统一包络，data 为受理回执（含预估积分）。

    Raises:
        BizError: 10001 非场景资产/变体非法/超限；40001 状态不允许；
            20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "edit")
    data = await asset_service.generate_scene_variants(
        session, asset, payload, int(user_id)
    )
    return _accepted(data, request_id)


@router.get("/assets/{asset_id}/variants")
async def get_asset_variants(
    asset_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """列出场景资产的变体覆盖与缺失（AC-F3-08-2 变体缺失判定）。

    Args:
        asset_id: 资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{asset_id, variants, missing, available_tags}``。

    Raises:
        BizError: 10002 资产不存在；20003 无权限。
    """
    asset = await _asset_guard(asset_id, user_id, session, "view")
    data = await asset_service.list_scene_variants(session, asset)
    return success_envelope(data, request_id)


@router.get("/face-authorizations")
async def get_face_authorizations(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    project_id: Annotated[int | None, Query(description="项目筛选")] = None,
    status: Annotated[str | None, Query(description="状态筛选 active/revoked")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """查询我的真人授权记录（AC-F3-12-1，V4.0）。

    列表含角色、授权时间、IP、协议版本与状态 active/revoked。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        project_id: 项目筛选。
        status: 状态筛选。
        page: 页码。
        page_size: 每页条数。

    Returns:
        成功包络，data 为 ``{items, page, page_size, total}``。
    """
    data = await asset_service.list_face_authorizations(
        session,
        user_id=int(user_id),
        project_id=project_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return success_envelope(data, request_id)


@router.post("/face-authorizations/{authorization_id}/revoke")
async def post_revoke_face_authorization(
    authorization_id: int,
    payload: FaceAuthorizationRevokeRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """撤回真人授权（AC-F3-12-2~3，V4.0）。

    须输入角色名二次确认；撤回后该角色禁止发起新镜头生成
    （50003 + ``error_detail=authorization_revoked``），已生成内容保留，
    进行中任务照常完成结算，撤回写 ``audit_logs``。

    Args:
        authorization_id: 授权记录 ID。
        payload: 撤回请求体（confirm_name / reason）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{authorization, shots_blocked}``。

    Raises:
        BizError: 10001 角色名不匹配；10002 授权记录不存在；
            40001 已撤回；20003 无权限。
    """
    auth = await asset_service.AssetRepository.get_authorization(session, authorization_id)
    if auth is None:
        raise BizError(ErrorCode.NOT_FOUND, "授权记录不存在")
    if int(auth.user_id) != int(user_id):
        raise BizError(ErrorCode.FORBIDDEN, "无权操作他人的授权记录")
    if auth.project_id:
        await require_project_acl(int(auth.project_id), user_id, session, "edit")
    data = await asset_service.revoke_face_authorization(
        session,
        auth,
        confirm_name=payload.confirm_name,
        reason=payload.reason,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id, message="授权已撤回")