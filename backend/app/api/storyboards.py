"""分镜域路由模块（Task 7b）。

提供分镜生成（202 异步任务模型）、分镜板列表/详情、镜头编辑与重排、
锚点绑定、分镜锁定与审批决策（storyboard 域专用 decide）、草稿预览
与画布布局保存。所有响应统一 ``success_envelope`` 包络；写操作经项目
级 ACL（owner 或 project_members 三级权限）。

路由变量 ``router = APIRouter(tags=["storyboards"])``（无 prefix，端点
自带完整路径），由 ``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id, project_guard, require_project_acl
from app.core.errors import success_envelope
from app.models.project import Project
from app.models.script import ScriptLine
from app.models.storyboard import Shot, Storyboard
from app.repositories.storyboard_repo import StoryboardRepository
from app.schemas.storyboard import (
    DirectorNoteRequest,
    FrameComposeRequest,
    FrameRedrawRequest,
    ShotAnchorRequest,
    ShotBatchPatchRequest,
    ShotCopyRequest,
    ShotPatchRequest,
    ShotReorderRequest,
    StoryboardDecisionRequest,
    StoryboardGenerateRequest,
    StoryboardLayoutRequest,
    StoryboardRollbackRequest,
    StoryboardTemplateRequest,
)
from app.services import storyboard_service

#: 分镜域路由（主控聚合使用：tags=[storyboards]，路径自带前缀）
router = APIRouter(tags=["storyboards"])


async def _storyboard_guard(
    storyboard_id: int,
    user_id: str,
    session: AsyncSession,
    need: str,
) -> Storyboard:
    """分镜板 ACL 守卫：加载分镜板并按归属项目校验权限。

    Args:
        storyboard_id: 分镜板 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        校验通过的分镜板 ORM 实例。

    Raises:
        BizError: 10002 分镜板不存在；20003 无权限。
    """
    storyboard = await storyboard_service.get_storyboard_or_error(session, storyboard_id)
    await require_project_acl(int(storyboard.project_id), user_id, session, need)  # type: ignore[arg-type]
    return storyboard


async def _shot_guard(
    shot_id: int,
    user_id: str,
    session: AsyncSession,
    need: str,
) -> Shot:
    """镜头 ACL 守卫：加载镜头并按归属项目校验权限。

    Args:
        shot_id: 镜头 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        校验通过的镜头 ORM 实例。

    Raises:
        BizError: 10002 镜头不存在；20003 无权限。
    """
    shot = await storyboard_service.get_shot_or_error(session, shot_id)
    await require_project_acl(int(shot.project_id), user_id, session, need)  # type: ignore[arg-type]
    return shot


@router.post("/projects/{project_id}/storyboards/generate")
async def generate_storyboard(
    payload: StoryboardGenerateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """发起分镜生成（202 异步任务模型；同集已有分镜板时幂等返回 200）。

    Args:
        payload: 生成请求体（episode_no）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        新发起返回 202；幂等命中返回 200（data.idempotent=True）。

    Raises:
        BizError: 10002 项目尚无剧本或集号不存在。
    """
    data = await storyboard_service.generate_storyboard(
        session, project, payload.episode_no, int(user_id),
        pacing_mode=payload.pacing_mode, template_id=payload.template_id,
        target_shot_count=payload.target_shot_count,
        target_duration_ms=payload.target_duration_ms,
        max_shot_duration_ms=payload.max_shot_duration_ms,
    )
    if data.get("idempotent"):
        return JSONResponse(status_code=200, content=success_envelope(data, request_id))
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.get("/projects/{project_id}/storyboards")
async def list_storyboards(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询项目分镜板列表（按集号升序，含镜头数/状态/时长合计）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"items": [...]}``。
    """
    items = await storyboard_service.list_storyboards(session, int(project.id))
    return success_envelope({"items": items}, request_id)


@router.get("/storyboards/{storyboard_id}")
async def get_storyboard(
    storyboard_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询分镜板详情（含镜头列表与当前首帧 media_url）。

    Args:
        storyboard_id: 分镜板 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为分镜板详情字典。

    Raises:
        BizError: 10002 分镜板不存在；20003 无权限。
    """
    await _storyboard_guard(storyboard_id, user_id, session, "view")
    data = await storyboard_service.get_storyboard_detail(session, storyboard_id)
    return success_envelope(data, request_id)


@router.patch("/shots/batch")
async def patch_shots_batch(
    payload: ShotBatchPatchRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """批量修改镜头参数（AC-F4-08-2~3）。

    ``confirm=false`` 只返回「N 个镜头将修改」与逐项变更摘要；确认后保存，
    对白超 6 秒的镜头单独标红（其余镜头修改不受回滚）。

    分镜板上下文由首个镜头反查（批量资源自带归属）。

    Args:
        payload: 批量修改请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {confirmed, affected, changes, qc_flags, shots}。

    Raises:
        BizError: 10001 无待修改字段；10002 镜头不存在；40001 分镜已锁定；
            20003 无权限。
    """
    first_shot = await storyboard_service.get_shot_or_error(session, payload.shot_ids[0])
    storyboard = await _storyboard_guard(
        int(first_shot.storyboard_id), user_id, session, "edit"
    )
    data = await storyboard_service.batch_patch_shots(
        session, storyboard, payload, int(user_id)
    )
    return success_envelope(data, request_id)



@router.patch("/shots/{shot_id}")
async def patch_shot(
    shot_id: int,
    payload: ShotPatchRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """局部编辑镜头（image_prompt/shot_size/camera_move/duration_ms/emotion_tag）。

    duration_ms 超出 1000~15000 由参数校验返回 10001；锁定镜头不可编辑。

    Args:
        shot_id: 镜头 ID。
        payload: PATCH 请求体（仅显式传入字段生效）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的镜头字典。

    Raises:
        BizError: 10001 参数非法；40001 镜头已锁定；20003 无权限。
    """
    shot = await _shot_guard(shot_id, user_id, session, "edit")
    changes = payload.model_dump(exclude_unset=True)
    data = await storyboard_service.patch_shot(session, shot, changes)
    return success_envelope(data, request_id, message="镜头已更新")


@router.post("/storyboards/{storyboard_id}/shots/reorder")
async def reorder_shots(
    storyboard_id: int,
    payload: ShotReorderRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """重排镜头（order_index 全量重算）并返回钩子位移提示。

    Args:
        storyboard_id: 分镜板 ID。
        payload: 重排请求体（shot_id + before/after 锚点二选一）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"episode_duration_ms", "hook_position_changed", "shots"}``。

    Raises:
        BizError: 10001 锚点参数非法；10002 镜头不存在；40001 分镜已锁定；20003 无权限。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "edit")
    data = await storyboard_service.reorder_shots(
        session, storyboard, payload.model_dump(exclude_unset=True),
    )
    return success_envelope(data, request_id, message="镜头顺序已更新")


@router.post("/shots/{shot_id}/anchors")
async def bind_anchors(
    shot_id: int,
    payload: ShotAnchorRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """绑定镜头锚点（资产须 locked，否则 50001）并返回组装后的 final_prompt。

    Args:
        shot_id: 镜头 ID。
        payload: 锚点绑定请求体（角色资产 ID 列表 + 可选场景资产）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 含 final_prompt 与锚点 ID 列表。

    Raises:
        BizError: 10001 类型不匹配；10002 资产不存在；40001 镜头已锁定；
            50001 资产未锁定；20003 无权限。
    """
    shot = await _shot_guard(shot_id, user_id, session, "edit")
    data = await storyboard_service.bind_anchors(
        session, shot, payload.character_asset_ids, payload.scene_asset_id,
    )
    return success_envelope(data, request_id, message="锚点已绑定")


@router.post("/storyboards/{storyboard_id}/lock")
async def lock_storyboard(
    storyboard_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """锁定分镜（全部镜头须至少 1 个锚点，缺则 60005 附缺锚点清单）。

    锁定后创建 storyboard 审批单（7 天有效）并发布 ``approval.created``。

    Args:
        storyboard_id: 分镜板 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 含分镜板与审批单 ID。

    Raises:
        BizError: 40001 已锁定/无镜头；60005 存在缺锚点镜头；20003 无权限。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "edit")
    data = await storyboard_service.lock_storyboard(session, storyboard, int(user_id))
    return success_envelope(data, request_id, message="分镜已锁定")


@router.post("/storyboards/{storyboard_id}/decide")
async def decide_storyboard(
    storyboard_id: int,
    payload: StoryboardDecisionRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """分镜审批决策（approve → 项目流转 rendering；reject → 解锁回待审）。

    Args:
        storyboard_id: 分镜板 ID。
        payload: 决策请求体（action + 可选 feedback）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 统一包络，data 为决策回执。

    Raises:
        BizError: 10001 reject 反馈不足 10 字；10002 无 pending 审批单；
            40001 已过期或项目状态不允许流转；20003 无权限。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "edit")
    data = await storyboard_service.decide_storyboard(
        session, storyboard, payload.action, payload.feedback, int(user_id),
    )
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.get("/storyboards/{storyboard_id}/draft-preview")
async def draft_preview(
    storyboard_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """分镜草稿预览（关键帧 URL + 字幕 + 时长合计；音频由后续任务填充）。

    Args:
        storyboard_id: 分镜板 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"storyboard_id", "shots", "total_duration_ms"}``。

    Raises:
        BizError: 10002 分镜板不存在；20003 无权限。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "view")
    data = await storyboard_service.draft_preview(session, storyboard)
    return success_envelope(data, request_id)


@router.post("/storyboards/{storyboard_id}/layout")
async def save_layout(
    storyboard_id: int,
    payload: StoryboardLayoutRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """保存分镜画布布局（按用户维度 upsert，P12 画布用）。

    Args:
        storyboard_id: 分镜板 ID。
        payload: 布局请求体（nodes + 可选 viewport）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为保存回执。

    Raises:
        BizError: 10002 分镜板不存在；20003 无权限。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "edit")
    data = await storyboard_service.save_layout(
        session, storyboard, int(user_id),
        [node.model_dump() for node in payload.nodes], payload.viewport,
    )
    return success_envelope(data, request_id, message="布局已保存")


# =====================================================================
# V4.0 分镜域增量端点
# =====================================================================


@router.put("/projects/{project_id}/director-notes")
async def put_director_notes(
    payload: DirectorNoteRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入导演策略（节奏模式 / 特写密度 / 对话机位规则）（AC-F4-07-1~3）。

    三种模式：``fast`` 快节奏（镜头均值 2000-3000ms、特写 ≥40%）、
    ``balanced`` 均衡、``slow`` 慢节奏（均值 3000-4500ms、空镜上调）。
    该集已有非 draft 分镜板时返回 ``protected_manual_episode=True`` 提示
    先建手动快照（不静默覆盖，AC-F4-07-3）。

    Args:
        payload: 策略请求体。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {note, pacing_profile, protected_manual_episode, message}。

    Raises:
        BizError: 10002 集号不存在；10001 模板不存在；20003 无权限。
    """
    data = await storyboard_service.upsert_director_note(
        session, project_id=int(project.id), body=payload, user_id=int(user_id)
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/director-notes")
async def get_director_notes(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询导演策略（全剧 + 按集），含题材推荐的默认节奏模式。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {project_note, episode_notes, recommended_pacing,
        available_modes}。
    """
    data = await storyboard_service.get_director_notes(session, int(project.id))
    return success_envelope(data, request_id)


@router.post("/shots/{shot_id}/copy")
async def post_shot_copy(
    shot_id: int,
    payload: ShotCopyRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """复制镜头并插入指定位置（AC-F4-08-1）。

    复制体继承 final_prompt/camera_move/duration_ms/emotion_tag/
    asset_anchor_ids；order_index 与 shot_no 自动重算，ID 全新。

    Args:
        shot_id: 源镜头 ID。
        payload: 复制请求体（mode / anchor_shot_id）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {new_shot, order_index, op_seq}。

    Raises:
        BizError: 10002 源镜头/锚点镜头不存在；40001 分镜已锁定；20003 无权限。
    """
    shot = await _shot_guard(shot_id, user_id, session, "edit")
    storyboard = await storyboard_service.get_storyboard_or_error(
        session, int(shot.storyboard_id)
    )
    data = await storyboard_service.copy_shot(
        session, storyboard, shot_id, payload, int(user_id)
    )
    return success_envelope(data, request_id)


@router.patch("/shots/{shot_id}/frame/redraw")
async def patch_shot_frame_redraw(
    shot_id: int,
    payload: FrameRedrawRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """草稿静帧重出（AC-F4-03-3，每镜头 ≤3 次）。

    Args:
        shot_id: 镜头 ID。
        payload: 重出请求体（feedback ≤50 字）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {shot_id, redraft_count, remaining, media_url,
        suggest_change_prompt}。

    Raises:
        BizError: 60004 重出次数超限；80001 模型不可用；20003 无权限。
    """
    shot = await _shot_guard(shot_id, user_id, session, "edit")
    data = await storyboard_service.redraw_shot_frame(
        session, shot, payload.feedback, int(user_id), payload.mode
    )
    return success_envelope(data, request_id)


@router.post("/storyboards/{storyboard_id}/frames/compose")
async def post_storyboard_frames_compose(
    storyboard_id: int,
    payload: FrameComposeRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """批量合成整板整幅要素首帧（0 成本，本地 ffmpeg）。

    Args:
        storyboard_id: 分镜板 ID。
        payload: 请求体（可选指定镜头）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {total, succeeded, failed, results}。

    Raises:
        BizError: 20003 无权限；10002 分镜板不存在。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "edit")
    data = await storyboard_service.compose_storyboard_frames(
        session, storyboard, payload.shot_ids, int(user_id)
    )
    return success_envelope(data, request_id)


@router.get("/storyboards/{storyboard_id}/frames/qc")
async def get_storyboard_frames_qc(
    storyboard_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """获取整板画面 QC 门禁结果（对视听叙事表达的可测检查）。

    Args:
        storyboard_id: 分镜板 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {passed, checks, issues, items}。

    Raises:
        BizError: 20003 无权限；10002 分镜板不存在。
    """
    from app.services import frame_qc_service
    from app.services.shot_frame_spec import CompositionSpec, normalize_spec

    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "view")
    rows = await StoryboardRepository.list_shots(session, int(storyboard.id))
    items: list[frame_qc_service.FrameQcItem] = []
    for shot in rows:
        fallback = CompositionSpec(
            canvas={"w": 720, "h": 1280},
            shot_size=str(shot.shot_size or "medium"),
            safe_area={"top": 0.08, "bottom": 0.18},
        )
        spec = (
            normalize_spec(shot.composition_spec, fallback=fallback)
            if shot.composition_spec
            else None
        )
        speakers: list[str] = []
        text = str(shot.dialogue or "").strip()
        if text:
            lines = (
                await session.execute(
                    select(ScriptLine).where(
                        ScriptLine.project_id == int(shot.project_id),
                        ScriptLine.is_deleted.is_(False),
                        ScriptLine.line_type.in_(("dialogue", "narration")),
                    )
                )
            ).scalars().all()
            for line in lines:
                content = str(line.content or "").strip()
                if content and content in text and line.speaker_name:
                    name = str(line.speaker_name).strip()
                    if name and name not in speakers:
                        speakers.append(name)
        signature = ""
        if spec is not None:
            setting_id = (spec.setting or {}).get("asset_id") if spec.setting else None
            subject_ids = ",".join(str(item.asset_id) for item in spec.subjects)
            prop_ids = ",".join(str(item.asset_id) for item in spec.props)
            signature = f"{setting_id}|{subject_ids}|{prop_ids}|{spec.shot_size}|{spec.focus}"
        items.append(
            frame_qc_service.FrameQcItem(
                shot_id=int(shot.id),
                shot_no=int(shot.shot_no),
                shot_size=str(shot.shot_size or "medium"),
                spec=spec,
                frame_media_id=int(shot.first_frame_media_id) if shot.first_frame_media_id else None,
                degraded=list(
                    (shot.composition_spec or {}).get("degraded")
                    if isinstance(shot.composition_spec, dict)
                    else []
                ),
                speakers=speakers,
                source_signature=signature,
            )
        )
    report = frame_qc_service.evaluate_frames(items)
    report["items"] = [
        {
            "shot_id": item.shot_id,
            "shot_no": item.shot_no,
            "frame_media_id": item.frame_media_id,
            "degraded": item.degraded,
            "source_signature": item.source_signature,
        }
        for item in items
    ]
    return success_envelope(report, request_id)


@router.get("/storyboard/ops-history")
async def get_storyboard_ops_history(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    storyboard_id: Annotated[int, Query(ge=1, description="分镜板 ID")] = 1,
) -> dict[str, Any]:
    """查询画布操作历史（最近 100 步，仅当次会话有效，AC-F4-09-1）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        storyboard_id: 分镜板 ID。

    Returns:
        成功包络，data 为 {storyboard_id, last_seq, items}。

    Raises:
        BizError: 10002 分镜板不存在；20003 无权限。
    """
    await _storyboard_guard(storyboard_id, user_id, session, "view")
    data = await storyboard_service.get_ops_history(
        session, storyboard_id, int(user_id)
    )
    return success_envelope(data, request_id)


@router.post("/storyboard/rollback")
async def post_storyboard_rollback(
    payload: StoryboardRollbackRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """把画布回退到指定步骤（被删镜头以原参数恢复，后续历史截断）。

    Args:
        payload: 回退请求体（storyboard_id / seq / confirm）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {confirmed, target_seq, restored_shots,
        truncated_steps}。

    Raises:
        BizError: 10001 步骤不存在；40001 分镜已锁定；20003 无权限。
    """
    storyboard = await _storyboard_guard(payload.storyboard_id, user_id, session, "edit")
    data = await storyboard_service.rollback_to_seq(
        session,
        storyboard,
        seq=payload.seq,
        confirm=payload.confirm,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)


@router.get("/storyboard-templates")
async def get_storyboard_templates(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """列出内置 + 自定义分镜模板（AC-F4-10-1~2）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {builtin, custom, custom_limit}。
    """
    data = await storyboard_service.list_storyboard_templates(session, int(user_id))
    return success_envelope(data, request_id)


@router.post("/storyboard-templates")
async def post_storyboard_template(
    payload: StoryboardTemplateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """保存自定义分镜模板（机位规则 ≤10 条、每条 ≤50 字，上限 20 个）。

    Args:
        payload: 模板请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {template, total}。

    Raises:
        BizError: 10001 规则条目非法；50005 自定义模板已达上限。
    """
    data = await storyboard_service.save_storyboard_template(
        session, user_id=int(user_id), body=payload
    )
    return success_envelope(data, request_id, message="分镜模板已保存")