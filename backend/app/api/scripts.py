"""剧本域路由模块（/projects/{id}/scripts、/scripts、/script-lines）。

提供剧本上传解析、剧本树查询、只读大纲、台词行编辑、AI 局部改写、
版本 diff 与合规送审端点。所有响应统一 ``success_envelope`` 包络；
鉴权经 :func:`app.core.deps.require_project_acl`（按资源反查项目做 ACL）。

路由变量 ``router = APIRouter(tags=["script"])``（路径自带完整前缀，
由主控在 ``app/api/__init__.py`` 聚合）。

端点清单：
- POST /projects/{project_id}/scripts/upload   上传剧本（202 异步解析）
- GET  /projects/{project_id}/scripts/current  当前剧本树
- POST /projects/{project_id}/scripts/rewrite  AI 局部改写（同步）
- GET  /projects/{project_id}/script-outline   只读集-场大纲（P07）
- GET  /scripts/{script_id}                    指定版本剧本树
- GET  /scripts/{script_id}/diff               版本行级 diff
- POST /scripts/{script_id}/submit-review      提交合规审核
- PATCH /script-lines/{line_id}                编辑台词行（版本+1）
- GET  /projects/{project_id}/script/versions  版本列表（V4.0）
- POST /projects/{project_id}/script/versions/{version_id}/restore
                                               历史版本设为当前（V4.0）
- PATCH /projects/{project_id}/script/versions/{version_id}
                                               版本命名备注（V4.0）
- GET  /projects/{project_id}/script/export    剧本导出 md/docx/txt（V4.0）
- POST /projects/{project_id}/script/continue  续写 N 集（V4.0）
- POST /projects/{project_id}/script/expand    单场扩写（V4.0）
- GET  /projects/{project_id}/script/runs      生成任务列表（V4.0）
- POST /projects/{project_id}/script/runs/{run_id}/cancel
                                               生成中终止（V4.0）
- GET  /templates/script-packs                 剧本模板包列表（V4.0）
- POST /projects/{project_id}/apply-template   套用模板包（V4.0）
"""

from __future__ import annotations

from typing import Annotated, Any

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import (
    PermissionNeed,
    get_current_user,
    get_request_id,
    project_guard,
    require_project_acl,
)
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.project import Project
from app.models.script import Script, ScriptLine
from app.repositories.script_repo import ScriptRepository
from app.schemas.script import (
    ApplyTemplateRequest,
    ScriptContinueRequest,
    ScriptExpandRequest,
    ScriptLinePatchRequest,
    ScriptRewriteRequest,
    VersionRestoreRequest,
)
from app.services import script_run_service, script_service
from app.services.template_packs import list_script_packs

#: 剧本域路由（路径自带完整前缀；主控聚合：include_router(scripts.router)）
router = APIRouter(tags=["script"])


async def _script_with_acl(
    script_id: int,
    user_id: str,
    session: AsyncSession,
    need: PermissionNeed,
) -> Script:
    """加载剧本版本并校验其所属项目 ACL。

    Args:
        script_id: 剧本版本 ID。
        user_id: 当前用户 ID。
        session: 数据库会话。
        need: 所需权限等级（view/edit/export）。

    Returns:
        剧本 ORM 实例。

    Raises:
        BizError: 10002 剧本不存在；20003 无权限。
    """
    script = await ScriptRepository(session).get_script(script_id)
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本不存在")
    await require_project_acl(int(script.project_id), user_id, session, need)
    return script


async def _line_with_acl(
    line_id: int,
    user_id: str,
    session: AsyncSession,
) -> ScriptLine:
    """加载台词行并校验其所属项目 ACL（edit）。

    Args:
        line_id: 剧本行 ID。
        user_id: 当前用户 ID。
        session: 数据库会话。

    Returns:
        剧本行 ORM 实例。

    Raises:
        BizError: 10002 行不存在；20003 无权限。
    """
    line = await ScriptRepository(session).get_line(line_id)
    if line is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本行不存在")
    await require_project_acl(int(line.project_id), user_id, session, "edit")
    return line


@router.post("/projects/{project_id}/scripts/upload")
async def post_scripts_upload(
    project_id: int,
    file: Annotated[UploadFile, File(description="剧本文档（txt/md，≤5MB，≤20 万字）")],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """上传剧本文档并触发异步解析（HTTP 202 语义）。

    校验：后缀仅支持 txt/md（docx 返回 50004 提示转档）；体积 ≤5MB、
    字数 ≤20 万（超限 40005）。解析在后台执行，进度经文档状态轮询
    （GET /documents/{id}）与项目事件流（document.parsed）获取。

    Args:
        project_id: 项目 ID。
        file: multipart 文件。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        202 成功包络，data 含 {document_id, status: "pending"}。

    Raises:
        BizError: 10002 项目不存在；20003 无权限；50004 文件非法；
            40005 超限；80003 存储失败。
    """
    await require_project_acl(project_id, user_id, session, "edit")
    data = await file.read()
    result = await script_service.upload_script_document(
        session,
        project_id=project_id,
        user_id=int(user_id),
        file_name=file.filename or "script.txt",
        data=data,
    )
    return JSONResponse(status_code=202, content=success_envelope(result))


@router.get("/projects/{project_id}/scripts/current")
async def get_scripts_current(
    project_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询项目当前（最新版本）剧本树。

    Args:
        project_id: 项目 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为 {script, episodes: [{...scenes: [...lines]}]} 树。

    Raises:
        BizError: 10002 项目不存在/尚无剧本；20003 无权限。
    """
    await require_project_acl(project_id, user_id, session, "view")
    tree = await script_service.get_current_script_tree(session, project_id)
    return success_envelope(tree)


@router.post("/projects/{project_id}/scripts/rewrite")
async def post_scripts_rewrite(
    project_id: int,
    body: ScriptRewriteRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """AI 局部改写（同步简化管线，不进图、不扣积分）。

    按集/场取上下文调用编剧 LLM（五段式提示词 + 只输出 JSON），替换
    受影响行并产生新版本；LLM 失败返回 80001 且不产生新版本。

    Args:
        project_id: 项目 ID。
        body: 改写请求（episode_no 必填，scene_no 缺省改写整集）。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 含 {new_version, script_id, changed_lines,
        changed_scene_count}。

    Raises:
        BizError: 10002 项目不存在/尚无剧本；20003 无权限；40002 集/场
            不存在；80001 模型不可用。
    """
    await require_project_acl(project_id, user_id, session, "edit")
    result = await script_service.rewrite_script(
        session,
        project_id=project_id,
        episode_no=body.episode_no,
        scene_no=body.scene_no,
        feedback=body.feedback,
        user_id=int(user_id),
        request_id=request_id,
    )
    return success_envelope(result)


@router.get("/projects/{project_id}/script-outline")
async def get_script_outline(
    project_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询剧本只读集-场大纲树（每集时长与钩子，供 P07 大纲页）。

    Args:
        project_id: 项目 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为 {script, episodes: [{...target_duration_seconds,
        hook, scenes: [...]}]}。

    Raises:
        BizError: 10002 项目不存在/尚无剧本；20003 无权限。
    """
    await require_project_acl(project_id, user_id, session, "view")
    outline = await script_service.get_outline_tree(session, project_id)
    return success_envelope(outline)


@router.get("/scripts/{script_id}")
async def get_script_detail(
    script_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """按剧本版本 ID 查询剧本树。

    Args:
        script_id: 剧本版本 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为剧本树。

    Raises:
        BizError: 10002 剧本不存在；20003 无权限。
    """
    script = await _script_with_acl(script_id, user_id, session, "view")
    tree = await script_service.build_script_tree(session, script)
    return success_envelope(tree)


@router.get("/scripts/{script_id}/diff")
async def get_script_diff(
    script_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    against: Annotated[int, Query(description="对照版本号")],
) -> dict[str, Any]:
    """查询当前版本与指定历史版本的行级 diff。

    对齐策略：按集/场分桶、按行位置对齐——文本不同进 changed，
    新版本独有进 added，旧版本独有进 removed。

    Args:
        script_id: 当前剧本版本 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        against: 对照版本号（不得等于当前版本）。

    Returns:
        成功包络，data 含 {added, removed, changed}。

    Raises:
        BizError: 10002 剧本/对照版本不存在；10001 against 非法；20003 无权限。
    """
    script = await _script_with_acl(script_id, user_id, session, "view")
    diff = await script_service.diff_script_versions(session, script=script, against_version=against)
    return success_envelope(diff)


@router.post("/scripts/{script_id}/submit-review")
async def post_script_submit_review(
    script_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """提交剧本合规审核（LLM 审核 Agent，同步执行）。

    首审 pass → pending_confirm；block → rejected；flag → 自动弱化重审
    一次后按结果放行/拒绝（末轮 flag 降级放行）。每轮落
    moderation_records 与 compliance_flags。

    Args:
        script_id: 剧本版本 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 含 {result, script_status, flags, softened, approval_id}；
        审核通过（pending_confirm）时同步创建 HITL-1 审批单并返回其 ID。

    Raises:
        BizError: 10002 剧本不存在；20003 无权限；80001 模型不可用
            （剧本状态回滚为提交前）。
    """
    await _script_with_acl(script_id, user_id, session, "edit")
    result = await script_service.submit_script_review(
        session,
        script_id=script_id,
        user_id=int(user_id),
        request_id=request_id,
    )
    return success_envelope(result)


@router.patch("/script-lines/{line_id}")
async def patch_script_line(
    line_id: int,
    body: ScriptLinePatchRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """编辑台词行（内容/说话人/情绪），产生剧本新版本。

    行就地更新（行 ID 不变），structure 快照同步更新，version_no+1，
    并落 operation_logs（diff 摘要）。

    Args:
        line_id: 剧本行 ID。
        body: 更新请求体（至少一个字段）。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 含 {line, new_version, script_id}。

    Raises:
        BizError: 10002 行不存在；10001 emotion 非法/空请求体；20003 无权限。
    """
    line = await _line_with_acl(line_id, user_id, session)
    result = await script_service.patch_script_line(
        session,
        line_id=int(line.id),
        content=body.content,
        speaker=body.speaker,
        emotion=body.emotion,
        user_id=int(user_id),
        request_id=request_id,
    )
    return success_envelope(result)


# =====================================================================
# V4.0 剧本版本 / 导出 / 续写扩写 / 生成运行态 / 模板包
# =====================================================================


@router.get("/projects/{project_id}/script/versions")
async def get_script_versions(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询项目的剧本版本列表（版本对比与「设为当前」入口）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为 {items: [版本摘要（含 is_current）], total, version_limit}。

    Raises:
        BizError: 10002 项目尚无剧本版本。
    """
    data = await script_service.list_script_versions(session, int(project.id))
    return success_envelope(data)


@router.post("/projects/{project_id}/script/versions/{version_id}/restore")
async def post_script_version_restore(
    version_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    body: VersionRestoreRequest | None = None,
) -> dict[str, Any]:
    """把历史版本「设为当前」：以该版本内容生成新版本（AC-F2-11-2）。

    版本链不破坏（parent 指向原最新版本），原历史版本仍可访问；
    新版本成为项目工作副本（episodes/scenes/lines 按快照重建）。

    Args:
        version_id: 历史版本 ID。
        body: 可选请求体（新版本备注 label）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {script, restored_from}。

    Raises:
        BizError: 10002 版本不存在；40001 已是最新版本；20003 无权限。
    """
    data = await script_service.restore_script_version(
        session,
        project_id=int(project.id),
        version_id=version_id,
        user_id=int(user_id),
        request_id=request_id,
        label=(body.label if body else None),
    )
    return success_envelope(data)


@router.patch("/projects/{project_id}/script/versions/{version_id}")
async def patch_script_version_label(
    version_id: int,
    body: VersionRestoreRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """给剧本版本写命名备注（版本列表可辨识）。

    Args:
        version_id: 版本 ID。
        body: 请求体（label 必填，≤50 字）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为 {script}。

    Raises:
        BizError: 10001 label 缺失；10002 版本不存在；20003 无权限。
    """
    if not body.label:
        raise BizError(ErrorCode.PARAM_INVALID, "label 不能为空")
    data = await script_service.update_version_label(
        session,
        project_id=int(project.id),
        version_id=version_id,
        label=body.label,
        user_id=int(user_id),
    )
    return success_envelope(data)


@router.get("/projects/{project_id}/script/export")
async def get_script_export(
    project: Annotated[Project, Depends(project_guard("view"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    fmt: Annotated[str, Query(alias="format", description="md / docx / txt")] = "md",
    version_id: Annotated[int | None, Query(description="目标版本 ID；空=当前版本")] = None,
) -> Response:
    """导出剧本为 md/docx/txt 文档（免费，AC-F2-10-1~3）。

    文件名 = ``{项目名}-剧本-v{版本号}.{ext}``；当剧本存在待人工校正字段时，
    文件首行插入「⚠ 本版本含待人工校正字段（N 处）」提示（AC-F2-10-2）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        fmt: 导出格式。
        version_id: 目标版本 ID。

    Returns:
        文件下载响应（Content-Disposition 携带文件名）。

    Raises:
        BizError: 10001 格式非法；10002 无剧本；20003 无权限。
    """
    payload, file_name, mime = await script_service.export_script(
        session,
        project_id=int(project.id),
        version_id=version_id,
        fmt=fmt,
        user_id=int(user_id),
    )
    return Response(
        content=payload,
        media_type=mime,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(file_name)}"
            ),
            "X-File-Name": quote(file_name),
        },
    )


@router.post("/projects/{project_id}/script/continue")
async def post_script_continue(
    body: ScriptContinueRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """续写 N 集：原集零改动，仅追加新集（AC-F2-08-1~3）。

    D31 修复：续写为长耗时 LLM 任务（实测 80 秒+），改为 202 后台受理；
    完成后发布 ``script.continued`` 事件，前端亦可轮询
    ``GET /projects/{id}/script/versions`` 感知新版本。

    Args:
        body: 续写请求（count / outline）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话（受理校验用）。
        request_id: 请求 ID。

    Returns:
        202 包络，data 为 {status, current_version, current_episodes}。

    Raises:
        BizError: 10002 剧本不存在。
    """
    script = await ScriptRepository(session).latest_script(int(project.id))
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，请先生成或上传")
    # 受理前同步校验集数上限：异步任务一旦受理就无法把错误回传给调用方，
    # 且 total_episodes 元数据可能滞后，需与实际工作副本集数取较大值。
    repo = ScriptRepository(session)
    db_episodes = await repo.episodes_of(int(script.id))
    structure_episodes = (script.structure or {}).get("episodes") or []
    current_episodes = max(
        int(script.total_episodes), len(db_episodes), len(structure_episodes)
    )
    if current_episodes + int(body.count) > script_service.MAX_EPISODES:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"续写后总集数将超过上限 {script_service.MAX_EPISODES}（当前 {current_episodes} 集）",
            error_detail="episode_limit_exceeded",
            data={
                "current": current_episodes,
                "requested": int(body.count),
                "limit": script_service.MAX_EPISODES,
            },
        )
    current_version = int(script.version_no)
    script_service.spawn_continue_script(
        project_id=int(project.id),
        script_id=int(script.id),
        count=body.count,
        outline_text=body.outline,
        user_id=int(user_id),
        request_id=request_id,
    )
    return JSONResponse(
        status_code=202,
        content=success_envelope(
            {
                "status": "accepted",
                "current_version": current_version,
                "current_episodes": current_episodes,
                "message": "续写任务已受理，完成后将发布 script.continued 事件",
            },
            request_id,
        ),
    )


@router.post("/projects/{project_id}/script/expand")
async def post_script_expand(
    body: ScriptExpandRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """单场扩写：仅该场产生新版本，并给出分集时长重算提示（AC-F2-08-2）。

    Args:
        body: 扩写请求（episode_no / scene_no / instruction ≤200 字）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {script, changed_lines, duration_change_seconds,
        duration_hint}。

    Raises:
        BizError: 10002 剧本不存在；40002 集/场不存在；80001 模型不可用。
    """
    script = await ScriptRepository(session).latest_script(int(project.id))
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，请先生成或上传")
    data = await script_service.expand_scene(
        session,
        project_id=int(project.id),
        script_id=int(script.id),
        episode_no=body.episode_no,
        scene_no=body.scene_no,
        instruction=body.instruction,
        user_id=int(user_id),
        request_id=request_id,
    )
    return success_envelope(data)


@router.get("/projects/{project_id}/script/runs")
async def get_script_runs(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """查询项目的剧本生成任务（进度页数据源，AC-F2-01-2 刷新恢复）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        limit: 返回条数上限。

    Returns:
        成功包络，data 为 {items: [运行态摘要（含进度百分比）], total}。
    """
    items = await script_run_service.list_runs(session, int(project.id), limit)
    return success_envelope({"items": items, "total": len(items)})


@router.post("/projects/{project_id}/script/runs/{run_id}/cancel")
async def post_script_run_cancel(
    run_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """终止剧本生成：保留已完成集，按已完集数结算并返还未执行部分。

    对齐 AC-F2-12-1~2：已完成 k 集保留为 ``draft``，未执行部分解冻返还；
    竞态下以先持久化的 ``completed_episodes`` 为准（结算走行锁事务，
    重复调用第二次因冻结已结算被拒，不重复扣费）。

    Args:
        run_id: 生成运行 ID。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为 {run, retained_episodes, settled_credits,
        refunded_credits, project_status}。

    Raises:
        BizError: 10002 运行不存在；40001 状态不允许终止；20003 无权限。
    """
    data = await script_run_service.cancel_run(
        session, project, run_id, int(user_id)
    )
    return success_envelope(data)


# ---- 模板包（US-F2-09）：列表无项目依赖，套用按项目校验 ACL ----
@router.get("/templates/script-packs")
async def get_script_packs(
    user_id: Annotated[str, Depends(get_current_user)],
    include_inactive: Annotated[bool, Query(description="是否包含已下线模板")] = False,
) -> dict[str, Any]:
    """查询剧本组合模板包列表（≥8 个，题材 × 画风）。

    Args:
        user_id: 当前用户 ID（仅用于鉴权）。
        include_inactive: 是否包含已下线模板。

    Returns:
        成功包络，data 为 {items: [模板包...], total, mutable_fields}。
    """
    from app.services.template_packs import TEMPLATE_MUTABLE_FIELDS

    packs = list_script_packs(include_inactive)
    return success_envelope(
        {"items": packs, "total": len(packs), "mutable_fields": list(TEMPLATE_MUTABLE_FIELDS)}
    )


@router.post("/projects/{project_id}/apply-template")
async def post_apply_template(
    body: ApplyTemplateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """套用模板包：参数回填项目并记录引用快照（AC-F2-09-1~3）。

    回填参数可覆盖；任一字段被手改后由 ``deviated_fields`` 标记「已偏离预设」。
    模板后续下线不影响本项目（参数快照留存）。

    Args:
        body: 套用请求（template_id / deviated_fields）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为 {template, project_params, deviated_fields, ref_id}。

    Raises:
        BizError: 10002 模板包不存在或已下线；20003 无权限。
    """
    data = await script_service.apply_script_template(
        session,
        project=project,
        template_id=body.template_id,
        user_id=int(user_id),
        deviated_fields=body.deviated_fields,
    )
    return success_envelope(data)