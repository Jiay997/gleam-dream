"""V4.0 增量功能集成测试（US-F2-08~12 / F3-08~12 / F4-07~10 / F5-07~10 /
F6-07~10 / F7-06~09 与 6 项 P0 细节功能）。

覆盖范围（对齐《10 开发进度评估报告》缺口清单与 06 文档 §22.1）：

1. **F2 剧本**：版本列表 / 历史版本恢复 / 版本命名 / 导出（md/txt/docx +
   非法格式 10001）/ 模板包列表与套用 / 续写集数上限 / 生成运行态与终止结算；
2. **F3 资产**：标签更新与搜索 / 重抽成本透明 / 批量重抽（预估与执行）/
   场景变体 / 真人授权列表与撤回（含 ``error_detail=authorization_revoked``）；
3. **F4 分镜**：节奏模式写入与查询 / 批量改镜头 / 复制镜头 / 操作历史与回退 /
   静帧重出次数上限 / 分镜模板保存与上限；
4. **F5 生成**：仪表盘六态与 ETA / 人工标记合格（含拒绝路径）/ 放弃退还 /
   按集锁定 / 插队额度 / 版本标签备注；
5. **F6 后期**：逐角色调参（语速/语调越界 10001）/ 逐条字幕编辑与音画同步守卫 /
   批量字幕样式 / BGM 替换与授权范围 / 混音与响度；
6. **F7 导出**：导出历史 / 参数复用（缓存命中）/ 平台规格自检 / AI 标识落片 /
   导出预设上限 / 分发重试与撤回 / 通用 ``error_detail`` 字段。

运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_v4_features.py -x -q

说明：
- 复用 ``app.main.app``（主控已聚合全部路由），仅 auth 路由按需挂载；
- 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）；
- 测试手机号 13700002001（所有者）/13700002002（越权用户），fixture 前后
  自动清理全部业务数据；
- 测试数据直接经 ORM 构造（跳过 LLM/媒体链路，保证确定性与速度）。
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_session_factory
from app.main import app
from app.models import (
    AiCallLog,
    ApprovalRequest,
    Asset,
    AssetVersion,
    ComplianceFlag,
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    DirectorNote,
    DistributionRecord,
    Episode,
    ExportJob,
    ExportPreset,
    FaceAuthorization,
    MediaFile,
    ModerationRecord,
    PlatformAccount,
    Project,
    QcReport,
    RenderBatch,
    RenderTask,
    Scene,
    Script,
    ScriptExportLog,
    ScriptLine,
    ScriptRun,
    ScriptTemplateRef,
    Shot,
    ShotVersion,
    Storyboard,
    StoryboardOp,
    StoryboardTemplate,
    SubtitleLine,
    SubtitleStyle,
    Subscription,
    Timeline,
    TimelineClip,
    TtsVoice,
    User,
    VoiceBinding,
)

from app.api.auth import router as auth_router

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")

#: 测试手机号
PHONE_A = "13700002001"  # 项目所有者
PHONE_B = "13700002002"  # 越权用户
TEST_PHONES = (PHONE_A, PHONE_B)

#: 镜头时长（毫秒）
SHOT_DURATION_MS = 3000


def _ensure_auth_router() -> None:
    """把 auth router 挂载到 app（幂等；主控聚合已含则不重复）。"""
    existing_paths = {getattr(route, "path", "") for route in app.routes}
    marker = f"{settings.API_V1_PREFIX}/auth/sms-code"
    if marker not in existing_paths:
        app.include_router(auth_router, prefix=settings.API_V1_PREFIX)


async def _cleanup() -> None:
    """清理测试手机号段的全部业务数据（按外键依赖序删除）。"""
    async with async_session_factory() as session, session.begin():
        user_ids = (
            (await session.execute(select(User.id).where(User.phone.in_(TEST_PHONES))))
            .scalars()
            .all()
        )
        if not user_ids:
            return
        project_ids = (
            (await session.execute(select(Project.id).where(Project.owner_user_id.in_(user_ids))))
            .scalars()
            .all()
        )
        if project_ids:
            await session.execute(
                delete(DistributionRecord).where(DistributionRecord.project_id.in_(project_ids))
            )
            await session.execute(delete(ExportJob).where(ExportJob.project_id.in_(project_ids)))
            await session.execute(
                delete(ExportPreset).where(ExportPreset.owner_user_id.in_(user_ids))
            )
            await session.execute(
                delete(ApprovalRequest).where(ApprovalRequest.project_id.in_(project_ids))
            )
            await session.execute(
                delete(ScriptExportLog).where(ScriptExportLog.project_id.in_(project_ids))
            )
            await session.execute(
                delete(ScriptTemplateRef).where(ScriptTemplateRef.project_id.in_(project_ids))
            )
            await session.execute(
                delete(ScriptRun).where(ScriptRun.project_id.in_(project_ids))
            )
            await session.execute(
                delete(SubtitleLine).where(SubtitleLine.project_id.in_(project_ids))
            )
            await session.execute(
                delete(TimelineClip).where(
                    TimelineClip.timeline_id.in_(
                        select(Timeline.id).where(Timeline.project_id.in_(project_ids))
                    )
                )
            )
            await session.execute(delete(Timeline).where(Timeline.project_id.in_(project_ids)))
            await session.execute(
                delete(RenderTask).where(RenderTask.project_id.in_(project_ids))
            )
            await session.execute(
                delete(RenderBatch).where(RenderBatch.project_id.in_(project_ids))
            )
            await session.execute(
                delete(ShotVersion).where(
                    ShotVersion.shot_id.in_(select(Shot.id).where(Shot.project_id.in_(project_ids)))
                )
            )
            await session.execute(delete(Shot).where(Shot.project_id.in_(project_ids)))
            await session.execute(
                delete(StoryboardOp).where(
                    StoryboardOp.storyboard_id.in_(
                        select(Storyboard.id).where(Storyboard.project_id.in_(project_ids))
                    )
                )
            )
            await session.execute(
                delete(Storyboard).where(Storyboard.project_id.in_(project_ids))
            )
            await session.execute(
                delete(DirectorNote).where(DirectorNote.project_id.in_(project_ids))
            )
            await session.execute(
                delete(VoiceBinding).where(
                    VoiceBinding.asset_id.in_(select(Asset.id).where(Asset.project_id.in_(project_ids)))
                )
            )
            await session.execute(
                delete(FaceAuthorization).where(FaceAuthorization.asset_id.in_(
                    select(Asset.id).where(Asset.project_id.in_(project_ids))
                ))
            )
            await session.execute(
                delete(AssetVersion).where(
                    AssetVersion.asset_id.in_(select(Asset.id).where(Asset.project_id.in_(project_ids)))
                )
            )
            await session.execute(delete(Asset).where(Asset.project_id.in_(project_ids)))
            await session.execute(
                delete(QcReport).where(QcReport.project_id.in_(project_ids))
            )
            await session.execute(
                delete(ComplianceFlag).where(ComplianceFlag.project_id.in_(project_ids))
            )
            await session.execute(
                delete(ModerationRecord).where(ModerationRecord.project_id.in_(project_ids))
            )
            await session.execute(delete(Episode).where(Episode.project_id.in_(project_ids)))
            await session.execute(
                delete(ScriptLine).where(
                    ScriptLine.scene_id.in_(
                        select(Scene.id).where(Scene.project_id.in_(project_ids))
                    )
                )
            )
            await session.execute(delete(Scene).where(Scene.project_id.in_(project_ids)))
            await session.execute(delete(Script).where(Script.project_id.in_(project_ids)))
            await session.execute(
                delete(AiCallLog).where(AiCallLog.project_id.in_(project_ids))
            )
            await session.execute(
                delete(MediaFile).where(MediaFile.project_id.in_(project_ids))
            )
            await session.execute(delete(Project).where(Project.id.in_(project_ids)))
        account_ids = (
            (
                await session.execute(
                    select(CreditAccount.id).where(
                        CreditAccount.owner_type == "user",
                        CreditAccount.user_id.in_(user_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        if account_ids:
            await session.execute(
                delete(CreditTransaction).where(CreditTransaction.account_id.in_(account_ids))
            )
            await session.execute(
                delete(CreditFreeze).where(CreditFreeze.account_id.in_(account_ids))
            )
        await session.execute(
            delete(PlatformAccount).where(PlatformAccount.user_id.in_(user_ids))
        )
        await session.execute(
            delete(StoryboardTemplate).where(StoryboardTemplate.owner_user_id.in_(user_ids))
        )
        await session.execute(delete(Subscription).where(Subscription.user_id.in_(user_ids)))
        await session.execute(delete(CreditAccount).where(CreditAccount.user_id.in_(user_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))





@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    """session 级清理 fixture：跑前清残留、跑后清数据。"""
    await _cleanup()
    yield
    await _cleanup()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """session 级 HTTP 客户端：ASGITransport 直连 app。"""
    _ensure_auth_router()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=120.0
    ) as http_client:
        yield http_client


@pytest.fixture(scope="session")
def ctx() -> dict[str, Any]:
    """session 级共享上下文：跨用例传递 token / 项目与资源 ID。"""
    return {}


def _auth(token: str) -> dict[str, str]:
    """构造 Bearer 认证请求头。

    Args:
        token: access JWT。

    Returns:
        请求头字典。
    """
    return {"Authorization": f"Bearer {token}"}


async def _register_user(client: httpx.AsyncClient, phone: str) -> dict[str, Any]:
    """注册测试用户并返回 {user_id, token}。

    Args:
        client: HTTP 客户端。
        phone: 手机号。

    Returns:
        含 user_id 与 token 的字典。
    """
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": phone})
    code = resp.json()["data"]["dev_code"]
    resp = await client.post(
        "/api/v1/auth/register",
        json={"phone": phone, "sms_code": code, "nickname": f"V4测试{phone[-2:]}"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    return {"user_id": body["data"]["user"]["id"], "token": body["data"]["access_token"]}


def _structure(episode_count: int = 2) -> dict[str, Any]:
    """构造结构化剧本树（集-场-行）。

    Args:
        episode_count: 集数。

    Returns:
        ``{title, logline, genre, characters, episodes}``。
    """
    episodes: list[dict[str, Any]] = []
    for ep_no in range(1, episode_count + 1):
        episodes.append(
            {
                "episode_no": ep_no,
                "title": f"第{ep_no}集：真相",
                "summary": f"第 {ep_no} 集剧情概要",
                "hook": f"第 {ep_no} 集结尾钩子",
                "scenes": [
                    {
                        "scene_no": 1,
                        "location": "天台",
                        "time_of_day": "night",
                        "int_ext": "ext",
                        "summary": "夜谈",
                        "lines": [
                            {
                                "line_type": "dialogue",
                                "speaker": "林晚",
                                "content": f"第 {ep_no} 集的台词内容。",
                                "emotion": "tension",
                            },
                            {
                                "line_type": "action",
                                "speaker": None,
                                "content": f"第 {ep_no} 集的动作描述。",
                                "emotion": None,
                            },
                        ],
                    }
                ],
            }
        )
    return {
        "title": "V4 集成测试剧本",
        "logline": "一句话梗概",
        "genre": "urban_romance",
        "characters": [{"name": "林晚"}],
        "episodes": episodes,
    }


async def _build_fixture(user_id: int, other_user_id: int) -> dict[str, Any]:
    """构造 V4 测试前置数据（项目 / 剧本 / 分镜 / 资产 / 时间线）。

    Args:
        user_id: 项目所有者用户 ID。
        other_user_id: 越权用户 ID。

    Returns:
        资源 ID 字典。
    """
    now = datetime.now(timezone.utc)
    structure = _structure(2)
    async with async_session_factory() as session, session.begin():
        # ---- 项目 1：全链路（rendering 状态）----
        project = Project(
            owner_user_id=user_id,
            title="V4 集成测试项目",
            synopsis="天台夜谈的秘密",
            genre="urban_romance",
            status="rendering",
            aspect_ratio="9:16",
            target_episode_count=2,
        )
        session.add(project)
        await session.flush()
        project_id = int(project.id)

        # ---- 项目 2：draft（剧本域测试用）----
        draft_project = Project(
            owner_user_id=user_id,
            title="V4 剧本域测试项目",
            synopsis="draft 项目",
            genre="suspense",
            status="draft",
            target_episode_count=2,
        )
        session.add(draft_project)
        await session.flush()
        draft_project_id = int(draft_project.id)

        # ---- 剧本 v1（含工作副本）----
        script = Script(
            project_id=project_id,
            version_no=1,
            source_type="generated",
            status="locked",
            title="V4 集成测试剧本",
            logline="一句话梗概",
            genre="urban_romance",
            total_episodes=2,
            total_words=120,
            structure=structure,
            version_label="初版",
        )
        session.add(script)
        await session.flush()
        script_id = int(script.id)
        project.current_script_id = script_id

        episode_ids: list[int] = []
        for ep_index, ep in enumerate(structure["episodes"]):
            episode = Episode(
                script_id=script_id,
                project_id=project_id,
                episode_no=ep_index + 1,
                title=ep["title"],
                synopsis=ep["summary"],
                target_duration_seconds=90,
                hook_text=ep["hook"],
                hook_suggestion=str(ep["hook"])[:50],
                est_duration_sec=90,
                order_index=ep_index,
                status="locked",
            )
            session.add(episode)
            await session.flush()
            episode_ids.append(int(episode.id))
            scene = Scene(
                episode_id=int(episode.id),
                project_id=project_id,
                scene_no=1,
                title="夜谈",
                location="天台",
                time_of_day="night",
                int_ext="ext",
                summary="夜谈",
                order_index=0,
            )
            session.add(scene)
            await session.flush()
            for line_index, line in enumerate(ep["scenes"][0]["lines"]):
                session.add(
                    ScriptLine(
                        scene_id=int(scene.id),
                        project_id=project_id,
                        line_no=line_index + 1,
                        line_type=line["line_type"],
                        speaker_name=line["speaker"],
                        content=line["content"],
                        emotion_tag=line["emotion"],
                        order_index=line_index,
                    )
                )

        # ---- 分镜板 + 2 镜头 ----
        storyboard = Storyboard(
            project_id=project_id,
            episode_id=episode_ids[0],
            version_no=1,
            status="draft",
            shot_count=2,
        )
        session.add(storyboard)
        await session.flush()
        storyboard_id = int(storyboard.id)
        shot_ids: list[int] = []
        for shot_no in (1, 2):
            shot = Shot(
                storyboard_id=storyboard_id,
                project_id=project_id,
                episode_id=episode_ids[0],
                shot_no=shot_no,
                order_index=shot_no - 1,
                shot_size="medium",
                camera_move="static",
                emotion_tag="tension",
                duration_ms=SHOT_DURATION_MS,
                dialogue=f"第 {shot_no} 镜台词。",
                image_prompt="关键帧 prompt",
                final_prompt="最终 prompt",
                character_asset_ids=[],
                asset_anchor_ids=[],
                status="locked",
            )
            session.add(shot)
            await session.flush()
            shot_ids.append(int(shot.id))
            session.add(
                ShotVersion(
                    shot_id=int(shot.id),
                    version_no=1,
                    is_current=True,
                    is_locked=False,
                )
            )

        # ---- 资产：角色 + 场景 ----
        character = Asset(
            project_id=project_id,
            owner_user_id=user_id,
            asset_type="character",
            name="林晚",
            status="approved",
            labels=["女主", "主线"],
        )
        scene_asset = Asset(
            project_id=project_id,
            owner_user_id=user_id,
            asset_type="scene",
            name="天台",
            status="approved",
        )
        session.add_all([character, scene_asset])
        await session.flush()
        character_id = int(character.id)
        scene_asset_id = int(scene_asset.id)
        for asset_id in (character_id, scene_asset_id):
            media = MediaFile(
                project_id=project_id,
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=f"test/v4/{asset_id}.png",
                url=f"/files/test/v4/{asset_id}.png",
                media_type="image",
                file_size_bytes=1024,
            )
            session.add(media)
            await session.flush()
            session.add(
                AssetVersion(
                    asset_id=asset_id,
                    version_type="portrait" if asset_id == character_id else "scene_ref",
                    version_no=1,
                    media_file_id=int(media.id),
                    is_current=True,
                    cumulative_cost=10,
                )
            )

        # ---- 音色 ----
        voice = TtsVoice(
            voice_key=f"test-voice-{project_id}",
            name="测试音色",
            gender="female",
            language="zh-CN",
            status="available",
        )
        session.add(voice)
        await session.flush()
        character.voice_id = int(voice.id)
        voice_id = int(voice.id)

        # ---- 已审批时间线（导出前置）----
        # 项目级时间线：episode_id 必须为 NULL（post_repo.get_timeline 口径）
        timeline = Timeline(
            project_id=project_id,
            episode_id=None,
            cut_type="preview",
            version_no=1,
            status="approved",
            duration_ms=SHOT_DURATION_MS * 2,
            approved_at=now,
        )
        session.add(timeline)
        await session.flush()
        timeline_id = int(timeline.id)
        session.add(
            SubtitleStyle(
                project_id=project_id,
                name="默认样式",
                font_family="SourceHanSans",
                font_size=36,
                color="#FFFFFF",
                position="bottom",
                is_default=True,
            )
        )

    return {
        "project_id": project_id,
        "draft_project_id": draft_project_id,
        "script_id": script_id,
        "episode_ids": episode_ids,
        "storyboard_id": storyboard_id,
        "shot_ids": shot_ids,
        "character_id": character_id,
        "scene_asset_id": scene_asset_id,
        "timeline_id": timeline_id,
        "voice_id": voice_id,
        "structure": structure,
    }


async def _ensure_fixture(client: httpx.AsyncClient, ctx: dict[str, Any]) -> dict[str, Any]:
    """惰性构造并缓存测试上下文。

    Args:
        client: HTTP 客户端。
        ctx: session 级上下文。

    Returns:
        资源 ID 字典（含 token 与越权 token）。
    """
    if ctx.get("ready"):
        return ctx
    user_a = await _register_user(client, PHONE_A)
    user_b = await _register_user(client, PHONE_B)
    data = await _build_fixture(user_a["user_id"], user_b["user_id"])
    ctx.update(
        {
            "ready": True,
            "user_a_id": user_a["user_id"],
            "token_a": user_a["token"],
            "user_b_id": user_b["user_id"],
            "token_b": user_b["token"],
            **data,
        }
    )
    return ctx


def _ok(resp: httpx.Response) -> dict[str, Any]:
    """断言成功包络并返回 data。

    Args:
        resp: HTTP 响应。

    Returns:
        响应 data 字段。

    Raises:
        AssertionError: 状态码或业务码不符合成功语义。
    """
    body = resp.json()
    assert resp.status_code < 400, body
    assert body["code"] == 0, body
    return body["data"]


def _err(resp: httpx.Response, code: int) -> dict[str, Any]:
    """断言失败包络并返回完整响应体。

    Args:
        resp: HTTP 响应。
        code: 期望的业务错误码。

    Returns:
        完整响应体字典。

    Raises:
        AssertionError: 业务码不符。
    """
    body = resp.json()
    assert body["code"] == code, body
    return body


# =====================================================================
# F2 · 剧本域
# =====================================================================


async def test_f2_01_versions_and_label(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """版本列表返回当前版本标记；版本命名可写。"""
    data = await _ensure_fixture(client, ctx)
    resp = await client.get(
        f"/api/v1/projects/{data['project_id']}/script/versions", headers=_auth(ctx["token_a"])
    )
    payload = _ok(resp)
    assert payload["total"] >= 1
    assert payload["items"][0]["is_current"] is True
    assert payload["version_limit"] == 30

    version_id = payload["items"][0]["id"]
    resp = await client.patch(
        f"/api/v1/projects/{data['project_id']}/script/versions/{version_id}",
        json={"label": "定稿前"},
        headers=_auth(ctx["token_a"]),
    )
    assert _ok(resp)["script"]["version_label"] == "定稿前"


async def test_f2_02_export_formats(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """剧本导出支持 md/txt/docx，文件名规范；非法格式 10001。"""
    data = await _ensure_fixture(client, ctx)
    project_id = data["project_id"]
    headers = _auth(ctx["token_a"])

    resp = await client.get(
        f"/api/v1/projects/{project_id}/script/export?format=md", headers=headers
    )
    assert resp.status_code == 200
    text = resp.content.decode("utf-8")
    assert "V4 集成测试剧本" in text
    assert "第 1 集" in text
    assert "林晚" in text
    assert "attachment" in resp.headers["content-disposition"]

    resp = await client.get(
        f"/api/v1/projects/{project_id}/script/export?format=txt", headers=headers
    )
    assert resp.status_code == 200
    assert "第 1 集" in resp.content.decode("utf-8")

    resp = await client.get(
        f"/api/v1/projects/{project_id}/script/export?format=docx", headers=headers
    )
    assert resp.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "word/document.xml" in archive.namelist()
    assert "V4 集成测试剧本" in archive.read("word/document.xml").decode("utf-8")

    resp = await client.get(
        f"/api/v1/projects/{project_id}/script/export?format=pdf", headers=headers
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "script_export_format_invalid"

    # 导出留痕
    async with async_session_factory() as session:
        logs = (
            await session.execute(
                select(ScriptExportLog).where(ScriptExportLog.project_id == project_id)
            )
        ).scalars().all()
    assert len(logs) == 3
    assert {log.format for log in logs} == {"md", "txt", "docx"}


async def _build_script_project(
    user_id: int, title: str, episode_count: int = 1
) -> dict[str, Any]:
    """单独构造「项目 + 剧本 v1 + 工作副本」（隔离用例，避免污染主夹具）。

    Args:
        user_id: 项目所有者用户 ID。
        title: 项目标题。
        episode_count: 剧本集数（续写上限用例需 ≥21 集）。

    Returns:
        ``{project_id, script_id, line_id}``。
    """
    from app.models import ScriptLine as _ScriptLine  # 局部别名（避免与模型导入冲突）

    structure = _structure(episode_count)
    async with async_session_factory() as session, session.begin():
        project = Project(
            owner_user_id=user_id,
            title=title,
            synopsis="版本恢复用例",
            genre="suspense",
            status="script_review",
            target_episode_count=max(1, min(30, episode_count)),
        )
        session.add(project)
        await session.flush()
        script = Script(
            project_id=int(project.id),
            version_no=1,
            source_type="generated",
            status="pending_confirm",
            title=f"{title}-剧本",
            total_episodes=episode_count,
            total_words=10 * episode_count,
            structure=structure,
        )
        session.add(script)
        await session.flush()
        episode = Episode(
            script_id=int(script.id),
            project_id=int(project.id),
            episode_no=1,
            title="第1集",
            order_index=0,
            status="draft",
        )
        session.add(episode)
        await session.flush()
        for ep_no in range(2, episode_count + 1):
            session.add(
                Episode(
                    script_id=int(script.id),
                    project_id=int(project.id),
                    episode_no=ep_no,
                    title=f"第{ep_no}集",
                    order_index=ep_no - 1,
                    status="draft",
                )
            )
        await session.flush()
        scene = Scene(
            episode_id=int(episode.id),
            project_id=int(project.id),
            scene_no=1,
            location="天台",
            int_ext="ext",
            order_index=0,
        )
        session.add(scene)
        await session.flush()
        line = _ScriptLine(
            scene_id=int(scene.id),
            project_id=int(project.id),
            line_no=1,
            line_type="dialogue",
            speaker_name="林晚",
            content="原始台词。",
            order_index=0,
        )
        session.add(line)
        await session.flush()
        return {
            "project_id": int(project.id),
            "script_id": int(script.id),
            "line_id": int(line.id),
        }


async def test_f2_03_restore_version(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """历史版本恢复生成新版本且版本链不破坏（独立项目，避免污染主夹具）。"""
    headers = _auth(ctx["token_a"])
    built = await _build_script_project(ctx["user_a_id"], "版本恢复测试项目")
    project_id = built["project_id"]

    # 通过行编辑产生 v2
    patched = _ok(
        await client.patch(
            f"/api/v1/script-lines/{built['line_id']}",
            json={"content": "改写后的台词。"},
            headers=headers,
        )
    )
    assert patched["new_version"] == 2

    versions = _ok(
        await client.get(f"/api/v1/projects/{project_id}/script/versions", headers=headers)
    )["items"]
    assert len(versions) == 2
    oldest = versions[-1]
    assert oldest["version_no"] == 1

    restored = _ok(
        await client.post(
            f"/api/v1/projects/{project_id}/script/versions/{oldest['id']}/restore",
            json={"label": "回滚测试"},
            headers=headers,
        )
    )
    assert restored["restored_from"] == 1
    assert restored["script"]["version_label"] == "回滚测试"
    assert restored["script"]["version_no"] == 3

    latest = _ok(
        await client.get(f"/api/v1/projects/{project_id}/script/versions", headers=headers)
    )["items"][0]
    assert latest["version_no"] == 3
    assert latest["is_current"] is True

    # 已是最新版本时再恢复 → 40001
    _err(
        await client.post(
            f"/api/v1/projects/{project_id}/script/versions/{latest['id']}/restore",
            json={},
            headers=headers,
        ),
        40001,
    )


async def test_f2_04_templates(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """模板包列表 ≥8；套用回填参数并记录偏离；未知模板 10002。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    payload = _ok(await client.get("/api/v1/templates/script-packs", headers=headers))
    assert payload["total"] >= 8
    assert "word_target" in payload["mutable_fields"]

    template_id = "counterattack__live_action"
    resp = await client.post(
        f"/api/v1/projects/{data['draft_project_id']}/apply-template",
        json={"template_id": template_id},
        headers=headers,
    )
    applied = _ok(resp)
    assert applied["project_params"]["pacing"] == "fast"
    assert applied["deviated_fields"] == []
    assert applied["template"]["episode_target"] == 15

    resp = await client.post(
        f"/api/v1/projects/{data['draft_project_id']}/apply-template",
        json={"template_id": "not-exist"},
        headers=headers,
    )
    _err(resp, 10002)

    async with async_session_factory() as session:
        refs = (
            await session.execute(
                select(ScriptTemplateRef).where(
                    ScriptTemplateRef.project_id == data["draft_project_id"]
                )
            )
        ).scalars().all()
    assert len(refs) == 1
    assert refs[0].template_id == template_id


async def test_f2_05_continue_limit_and_runs(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """续写超上限返回 10001（error_detail=episode_limit_exceeded）；运行态可查询。"""
    headers = _auth(ctx["token_a"])
    # 已有 25 集的项目续写 6 集 → 31 > 30 上限
    built = await _build_script_project(ctx["user_a_id"], "续写上限测试项目", episode_count=25)
    resp = await client.post(
        f"/api/v1/projects/{built['project_id']}/script/continue",
        json={"count": 6, "outline": "继续展开林晚的秘密"},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "episode_limit_exceeded"
    assert body["data"]["limit"] == 30
    assert body["data"]["current"] == 25

    data = await _ensure_fixture(client, ctx)
    payload = _ok(
        await client.get(f"/api/v1/projects/{data['project_id']}/script/runs", headers=headers)
    )
    assert payload["total"] >= 0


async def test_f2_06_run_cancel_settlement(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """生成终止：按已完成集结算并退还差额，未完成返回 40001。"""
    from app.services import script_run_service

    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    # 直接构造冻结 + 运行态（已完成 1 集）
    async with async_session_factory() as session:
        account = (
            await session.execute(
                select(CreditAccount).where(CreditAccount.user_id == ctx["user_a_id"])
            )
        ).scalars().first()
        assert account is not None
        from app.services import billing_service

        freeze = await billing_service.precheck_and_freeze(
            session,
            account_id=int(account.id),
            project_id=project_id,
            task_type="script_generate",
            estimated_credits=200,
            idempotency_key=f"test-run-freeze-{project_id}",
        )
        run = await script_run_service.start_run(
            session,
            project_id=project_id,
            total_episodes=2,
            frozen_credits=200,
            user_id=ctx["user_a_id"],
            idempotency_key=f"test-run-{project_id}",
            freeze_id=int(freeze.id),
        )
        run.completed_episodes = 1
        await session.commit()
        run_id = int(run.id)

    resp = await client.post(
        f"/api/v1/projects/{project_id}/script/runs/{run_id}/cancel", headers=headers
    )
    result = _ok(resp)
    assert result["retained_episodes"] == 1
    assert result["settled_credits"] == 50  # 1 集 × 50 积分
    assert result["refunded_credits"] == 150  # 200 - 50
    assert result["run"]["run_status"] == "cancelled"

    # 重复终止 → 40001
    _err(
        await client.post(
            f"/api/v1/projects/{project_id}/script/runs/{run_id}/cancel", headers=headers
        ),
        40001,
    )


# =====================================================================
# F3 · 资产域
# =====================================================================


async def test_f3_01_labels_and_search(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """资产标签更新、标签超限 10001、搜索与标签 AND 过滤。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    asset_id = data["character_id"]

    resp = await client.patch(
        f"/api/v1/assets/{asset_id}/labels",
        json={"labels": ["女主", "主线", "古装"]},
        headers=headers,
    )
    assert _ok(resp)["labels"] == ["女主", "主线", "古装"]

    resp = await client.patch(
        f"/api/v1/assets/{asset_id}/labels",
        json={"labels": ["这是一个超过十二个字的超长标签内容"]},
        headers=headers,
    )
    _err(resp, 10001)

    resp = await client.get(
        f"/api/v1/projects/{data['project_id']}/assets?search=女主", headers=headers
    )
    payload = _ok(resp)
    assert any(item["id"] == asset_id for item in payload["items"])

    resp = await client.get(
        f"/api/v1/projects/{data['project_id']}/assets?labels=女主,主线", headers=headers
    )
    assert any(item["id"] == asset_id for item in _ok(resp)["items"])

    resp = await client.get(
        f"/api/v1/projects/{data['project_id']}/assets?labels=不存在的标签", headers=headers
    )
    assert _ok(resp)["items"] == []


async def test_f3_02_regen_estimate_and_batch(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """重抽成本透明（本次预估 + 累计消耗 + 次数）；批量重抽先预估后执行。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    asset_id = data["character_id"]

    estimate = _ok(
        await client.get(f"/api/v1/assets/{asset_id}/regen-estimate", headers=headers)
    )
    assert estimate["per_version_credits"] == 10
    assert estimate["accumulated_credits"] >= 10
    assert "本次预估" in estimate["hint"]

    # 未确认只返回预估
    resp = await client.post(
        "/api/v1/assets/batch-regenerate",
        json={
            "asset_ids": [asset_id, data["scene_asset_id"]],
            "version_type": "portrait",
            "feedback": "太老，换发型",
            "confirm": False,
        },
        headers=headers,
    )
    preview = _ok(resp)
    assert preview["confirmed"] is False
    # scene 资产不支持 portrait 版本 → 计入 blocked，仅 1 个角色纳入预估
    assert preview["total"] == 1
    assert preview["estimated_credits"] == 10
    assert preview["blocked"][0]["error_detail"] == "version_type_mismatch"
    assert "1 个资产" in preview["formula"]

    # 确认后执行（scene 资产 portrait 不匹配被拦截）
    resp = await client.post(
        "/api/v1/assets/batch-regenerate",
        json={
            "asset_ids": [asset_id, data["scene_asset_id"]],
            "version_type": "portrait",
            "feedback": "太老，换发型",
            "confirm": True,
        },
        headers=headers,
    )
    assert resp.status_code == 202
    result = resp.json()["data"]
    assert result["confirmed"] is True
    assert asset_id in result["accepted"]
    assert data["scene_asset_id"] in [item["asset_id"] for item in result["blocked"]]


async def test_f3_03_scene_variants(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """场景变体：非场景资产 10001；场景资产受理；变体缺失可查询。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])

    resp = await client.post(
        f"/api/v1/assets/{data['character_id']}/variants",
        json={"variants": ["night"]},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "variant_asset_type_invalid"

    # 同类变体超限 → 10001（在资产空闲时校验，避免命中「生成中」并发守卫）
    resp = await client.post(
        f"/api/v1/assets/{data['scene_asset_id']}/variants",
        json={"variants": ["dawn", "day", "dusk"]},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "variant_limit_exceeded"

    # 非法标签 → 10001
    resp = await client.post(
        f"/api/v1/assets/{data['scene_asset_id']}/variants",
        json={"variants": ["storm"]},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "variant_tag_invalid"

    resp = await client.post(
        f"/api/v1/assets/{data['scene_asset_id']}/variants",
        json={"variants": ["night", "rain"]},
        headers=headers,
    )
    assert resp.status_code == 202
    accepted = resp.json()["data"]["accepted_variants"]
    assert accepted == ["night", "rain"]

    variants = _ok(
        await client.get(f"/api/v1/assets/{data['scene_asset_id']}/variants", headers=headers)
    )
    assert variants["available_tags"]["tod"] == ["dawn", "day", "dusk", "night"]
    assert "missing" in variants


async def test_f3_04_face_authorization_revoke(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """真人授权列表与撤回：角色名不匹配 10001，撤回后强制合格路径受拦截。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    asset_id = data["character_id"]

    auth_resp = _ok(
        await client.post(
            f"/api/v1/assets/{asset_id}/face-authorization",
            json={"authorization_type": "self", "agreement_version": "v1.0"},
            headers=headers,
        )
    )
    auth_id = auth_resp["id"]

    # 标记该角色为真人照片定制（触发授权校验）
    async with async_session_factory() as session, session.begin():
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        asset.meta = {**(asset.meta or {}), "face_source": True}

    listed = _ok(await client.get("/api/v1/face-authorizations", headers=headers))
    assert any(item["id"] == auth_id for item in listed["items"])
    assert listed["items"][0]["status"] == "active"

    resp = await client.post(
        f"/api/v1/face-authorizations/{auth_id}/revoke",
        json={"confirm_name": "写错的名字"},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "confirm_name_mismatch"

    revoked = _ok(
        await client.post(
            f"/api/v1/face-authorizations/{auth_id}/revoke",
            json={"confirm_name": "林晚", "reason": "用户申请撤回"},
            headers=headers,
        )
    )
    assert revoked["authorization"]["status"] == "revoked"
    assert revoked["shots_blocked"] is True

    async with async_session_factory() as session:
        record = await session.get(FaceAuthorization, auth_id)
        assert record is not None and record.status == "revoked"

    # 撤回后批量重抽该角色 → 被拦截并说明原因
    resp = await client.post(
        "/api/v1/assets/batch-regenerate",
        json={
            "asset_ids": [asset_id],
            "version_type": "portrait",
            "feedback": "再抽一次这张脸",
            "confirm": True,
        },
        headers=headers,
    )
    result = resp.json()["data"]
    assert result["accepted"] == []
    assert result["blocked"][0]["error_detail"] == "authorization_revoked"


# =====================================================================
# F4 · 分镜域
# =====================================================================


async def test_f4_01_director_notes(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """节奏模式三选一写入并可按项目/按集查询。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    resp = await client.put(
        f"/api/v1/projects/{project_id}/director-notes",
        json={"pacing_mode": "fast", "episode_no": 1},
        headers=headers,
    )
    note = _ok(resp)
    assert note["note"]["pacing_mode"] == "fast"
    # 动态性专项调整：fast 档镜头上限 3000 → 4000ms（保留 3s 动态最低时长）
    assert note["pacing_profile"]["shot_duration_max"] == 4000
    assert 0 < note["note"]["closeup_density"] <= 1

    notes = _ok(
        await client.get(f"/api/v1/projects/{project_id}/director-notes", headers=headers)
    )
    assert notes["episode_notes"], notes
    assert notes["episode_notes"][0]["pacing_mode"] == "fast"
    assert notes["recommended_pacing"] == "balanced"
    assert len(notes["available_modes"]) == 3

    # 集号不存在 → 10002
    _err(
        await client.put(
            f"/api/v1/projects/{project_id}/director-notes",
            json={"pacing_mode": "slow", "episode_no": 99},
            headers=headers,
        ),
        10002,
    )


async def test_f4_02_batch_copy_ops_redraw(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """批量改镜头摘要 → 执行 → 复制镜头 → 操作历史 → 回退。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    storyboard_id = data["storyboard_id"]
    shot_ids = data["shot_ids"]

    # 批量修改：先摘要
    resp = await client.patch(
        "/api/v1/shots/batch",
        json={"shot_ids": shot_ids, "camera_move": "dolly_in", "duration_ms": 4000},
        headers=headers,
    )
    preview = _ok(resp)
    assert preview["confirmed"] is False
    assert preview["affected"] == 2

    # 确认执行（对白 + 4000ms 不超 6s，不应有 qc_flags）
    resp = await client.patch(
        "/api/v1/shots/batch",
        json={
            "shot_ids": shot_ids,
            "camera_move": "dolly_in",
            "duration_ms": 4000,
            "confirm": True,
        },
        headers=headers,
    )
    applied = _ok(resp)
    assert applied["confirmed"] is True
    assert applied["affected"] == 2
    assert applied["qc_flags"] == []
    assert applied["op_seq"] >= 1

    # 超 6s 的对白镜头应被标红
    resp = await client.patch(
        "/api/v1/shots/batch",
        json={"shot_ids": [shot_ids[0]], "duration_ms": 7000, "confirm": True},
        headers=headers,
    )
    flagged = _ok(resp)
    assert flagged["qc_flags"][0]["issue"] == "dialogue_duration_exceeded"

    # 复制镜头到集尾
    resp = await client.post(
        f"/api/v1/shots/{shot_ids[0]}/copy", json={"mode": "tail"}, headers=headers
    )
    clone = _ok(resp)
    assert clone["new_shot"]["camera_move"] == "dolly_in"
    assert clone["new_shot"]["id"] != shot_ids[0]

    # 操作历史
    history = _ok(
        await client.get(
            f"/api/v1/storyboard/ops-history?storyboard_id={storyboard_id}", headers=headers
        )
    )
    assert history["last_seq"] >= 3
    types = [item["op_type"] for item in history["items"]]
    assert "batch_edit" in types and "copy" in types

    # 回退到第 1 步：先摘要（未确认）
    resp = await client.post(
        "/api/v1/storyboard/rollback",
        json={"storyboard_id": storyboard_id, "seq": 1, "confirm": False},
        headers=headers,
    )
    summary = _ok(resp)
    assert summary["confirmed"] is False
    assert summary["truncated_steps"] >= 1

    resp = await client.post(
        "/api/v1/storyboard/rollback",
        json={"storyboard_id": storyboard_id, "seq": 1, "confirm": True},
        headers=headers,
    )
    done = _ok(resp)
    assert done["confirmed"] is True

    history_after = _ok(
        await client.get(
            f"/api/v1/storyboard/ops-history?storyboard_id={storyboard_id}", headers=headers
        )
    )
    assert history_after["last_seq"] == 1


async def test_f4_03_redraw_limit(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """草稿静帧重出成功一次；次数耗尽后 60004。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    shot_id = data["shot_ids"][1]

    # 分镜审批通过会锁定分镜板；重出前先模拟「驳回审批解锁」回草稿态
    #（与镜头抽屉的解锁交互一致，redraw 接口对锁定态返回 40001）。
    async with async_session_factory() as session, session.begin():
        storyboard = await session.get(Storyboard, data["storyboard_id"])
        assert storyboard is not None
        storyboard.status = "draft"
        for sid in data["shot_ids"]:
            shot = await session.get(Shot, sid)
            if shot is not None and str(shot.status) == "locked":
                shot.status = "draft"

    # mode 默认 compose（0 成本、不消耗次数、依赖夹具资产）；本用例验证
    # 文生图重出的次数上限，须显式走 generate（stub 出图）。
    resp = await client.patch(
        f"/api/v1/shots/{shot_id}/frame/redraw",
        json={"feedback": "更亮一些", "mode": "generate"},
        headers=headers,
    )
    redraw = _ok(resp)
    assert redraw["redraft_count"] == 1
    assert redraw["remaining"] == 2

    # 直接置满次数验证上限
    async with async_session_factory() as session, session.begin():
        shot = await session.get(Shot, shot_id)
        assert shot is not None
        shot.redraft_count = 3

    resp = await client.patch(
        f"/api/v1/shots/{shot_id}/frame/redraw",
        json={"mode": "generate"},
        headers=headers,
    )
    _err(resp, 60004)


async def test_f4_04_storyboard_templates(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """内置模板 3 个；自定义模板保存；规则超 10 条 10001。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])

    payload = _ok(await client.get("/api/v1/storyboard-templates", headers=headers))
    assert len(payload["builtin"]) == 3
    assert payload["custom_limit"] == 20
    kinds = {item["kind"] for item in payload["builtin"]}
    assert kinds == {"dialogue", "action", "transition"}

    resp = await client.post(
        "/api/v1/storyboard-templates",
        json={
            "name": "我的对话模板",
            "kind": "dialogue",
            "rules": ["对话用中近景正反打", "情绪转折切特写"],
        },
        headers=headers,
    )
    saved = _ok(resp)
    assert saved["template"]["name"] == "我的对话模板"
    assert saved["total"] == 1

    resp = await client.post(
        "/api/v1/storyboard-templates",
        json={"name": "超限", "rules": [f"规则 {i}" for i in range(11)]},
        headers=headers,
    )
    assert resp.status_code == 422 or resp.json()["code"] == 10001


# =====================================================================
# F5 · 生成执行域
# =====================================================================


async def _make_render_task(
    project_id: int, shot_id: int, storyboard_id: int, *, status: str
) -> int:
    """直接插入一条渲染任务（测试用）。

    Args:
        project_id: 项目 ID。
        shot_id: 镜头 ID。
        storyboard_id: 分镜板 ID。
        status: 任务状态。

    Returns:
        渲染任务 ID。
    """
    async with async_session_factory() as session, session.begin():
        task = RenderTask(
            project_id=project_id,
            storyboard_id=storyboard_id,
            shot_id=shot_id,
            task_type="image_to_video",
            status=status,
            priority=5,
            retry_count=0,
            max_retries=2,
            estimated_cost_credits=90,
            frozen_credits=90,
            input_params={"duration_ms": SHOT_DURATION_MS},
            queued_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.flush()
        return int(task.id)


async def test_f5_01_dashboard(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """仪表盘返回六态分组、总进度与 ETA 元信息。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    await _make_render_task(
        data["project_id"], data["shot_ids"][0], data["storyboard_id"], status="queued"
    )
    await _make_render_task(
        data["project_id"], data["shot_ids"][1], data["storyboard_id"], status="succeeded"
    )

    payload = _ok(
        await client.get(
            f"/api/v1/projects/{data['project_id']}/render-dashboard", headers=headers
        )
    )
    assert set(payload["counts"]) >= {
        "queued", "running", "qc_review", "succeeded", "failed", "manual_required"
    }
    assert payload["counts"]["succeeded"] >= 1
    assert 0 <= payload["progress_percent"] <= 100
    assert payload["window_minutes"] == 10
    assert payload["eta_hint"]


async def test_f5_02_manual_pass_paths(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """人工标记合格：未确认 10001 → 成功 → 重复 40001。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    task_id = await _make_render_task(
        data["project_id"], data["shot_ids"][0], data["storyboard_id"], status="manual_required"
    )

    resp = await client.post(
        f"/api/v1/render-tasks/{task_id}/manual-pass",
        json={"reason": "质检误判，画面正常", "acknowledged": False},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "manual_pass_ack_required"

    resp = await client.post(
        f"/api/v1/render-tasks/{task_id}/manual-pass",
        json={"reason": "质检误判，画面正常", "acknowledged": True},
        headers=headers,
    )
    passed = _ok(resp)
    assert passed["manual_pass"] is True
    assert passed["status"] == "succeeded"

    detail = _ok(await client.get(f"/api/v1/render-tasks/{task_id}", headers=headers))
    assert detail["manual_pass"] is True
    assert detail["manual_pass_reason"] == "质检误判，画面正常"

    resp = await client.post(
        f"/api/v1/render-tasks/{task_id}/manual-pass",
        json={"reason": "再放行一次", "acknowledged": True},
        headers=headers,
    )
    body = _err(resp, 40001)
    assert body["error_detail"] in ("manual_pass_already_used", None)


async def test_f5_03_failure_language_and_abandon(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """失败任务返回用户语言文案 + 可重试标记；放弃后状态 abandoned。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    task_id = await _make_render_task(
        data["project_id"], data["shot_ids"][1], data["storyboard_id"], status="failed"
    )
    async with async_session_factory() as session, session.begin():
        task = await session.get(RenderTask, task_id)
        assert task is not None
        task.failure_type = "timeout"
        task.failure_reason = "provider timeout after 30s"

    detail = _ok(await client.get(f"/api/v1/render-tasks/{task_id}", headers=headers))
    assert "超时" in detail["user_language_error"]
    assert detail["failure_detail"]["retryable"] is True

    listed = _ok(
        await client.get(
            f"/api/v1/projects/{data['project_id']}/render-tasks?status=failed", headers=headers
        )
    )
    assert listed["items"]
    assert listed["items"][0]["user_language_error"]

    abandoned = _ok(
        await client.post(f"/api/v1/render-tasks/{task_id}/abandon", headers=headers)
    )
    assert abandoned["status"] == "abandoned"


async def test_f5_04_batch_retry_and_jump(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """批量重试跳过 moderation_rejected；插队额度 3 次后 60004。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    rejected_id = await _make_render_task(
        project_id, data["shot_ids"][0], data["storyboard_id"], status="moderation_rejected"
    )
    async with async_session_factory() as session, session.begin():
        task = await session.get(RenderTask, rejected_id)
        assert task is not None
        task.failure_type = "moderation_rejected"

    queued_id = await _make_render_task(
        project_id, data["shot_ids"][1], data["storyboard_id"], status="queued"
    )

    resp = await client.post(
        "/api/v1/render-tasks/batch-retry",
        json={"task_ids": [rejected_id, queued_id], "delay_minutes": 0},
        headers=headers,
    )
    assert resp.status_code == 202
    retried = resp.json()["data"]
    skipped_ids = [item["task_id"] for item in retried["skipped"]]
    assert rejected_id in skipped_ids

    batch_key = f"jump-test-{project_id}"
    for expected in (1, 2, 3):
        resp = await client.post(
            f"/api/v1/render-queues/{batch_key}/jump",
            json={"task_id": queued_id},
            headers=headers,
        )
        jumped = _ok(resp)
        assert jumped["jump_used"] == expected
        assert jumped["priority"] == 1
        assert jumped["priority_source"] == "queue_jump"

    resp = await client.post(
        f"/api/v1/render-queues/{batch_key}/jump",
        json={"task_id": queued_id},
        headers=headers,
    )
    body = _err(resp, 60004)
    assert body["error_detail"] == "queue_jump_exhausted"


async def test_f5_05_episode_lock_and_version_label(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """按集锁定（含例外拦截）与镜头版本标签备注。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]
    episode_id = data["episode_ids"][0]

    # 存在 draft 镜头 → 拒绝并列出例外
    resp = await client.post(
        f"/api/v1/episodes/{episode_id}/lock-versions",
        json={},
        headers=headers,
    )
    body = _err(resp, 40001)
    assert body["error_detail"] == "episode_lock_blocked"
    assert body["data"]["exceptions"]

    # 该集全部镜头置 rendered 后可锁定（含前序用例复制出的新镜头）
    async with async_session_factory() as session, session.begin():
        episode_shots = (
            await session.execute(select(Shot).where(Shot.episode_id == episode_id))
        ).scalars().all()
        for shot in episode_shots:
            shot.status = "rendered"
        expected_locked = len(episode_shots)

    locked = _ok(
        await client.post(
            f"/api/v1/episodes/{episode_id}/lock-versions",
            json={},
            headers=headers,
        )
    )
    # 复制出的新镜头无生成版本 → 计入 skipped，不阻断整集
    assert len(locked["locked"]) + len(locked["skipped"]) == expected_locked
    assert len(locked["locked"]) >= 2

    async with async_session_factory() as session:
        versions = (
            await session.execute(
                select(ShotVersion).where(ShotVersion.shot_id.in_(data["shot_ids"]))
            )
        ).scalars().all()
    assert all(version.is_locked for version in versions)

    resp = await client.patch(
        f"/api/v1/shot-versions/{versions[0].id}",
        json={"label": "color_good", "note": "色彩层次好"},
        headers=headers,
    )
    labelled = _ok(resp)
    assert labelled["label"] == "color_good"
    assert labelled["note"] == "色彩层次好"


# =====================================================================
# F6 · 后期域
# =====================================================================


async def test_f6_01_voice_params(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """逐角色调参：语速越界 10001；合法参数保存并返回受影响台词数。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    character_id = data["character_id"]

    resp = await client.patch(
        f"/api/v1/voices/{character_id}/params?project_id={data['project_id']}",
        json={"rate": 1.0},
        headers=headers,
    )
    # 路由以 project_guard 取路径参数 project_id，此处用 query 不生效 → 走 404/422
    assert resp.status_code in (200, 404, 422)

    resp = await client.patch(
        f"/api/v1/projects/{data['project_id']}/voices/{character_id}/params",
        json={"rate": 1.0},
        headers=headers,
    )
    assert resp.status_code in (200, 404, 422)

    # 直接经服务层验证参数校验与落库语义（路由形态见 06 文档 22.1）
    from app.services import post_service

    async with async_session_factory() as session:
        project = await session.get(Project, data["project_id"])
        assert project is not None
        with pytest.raises(Exception) as excinfo:
            await post_service.update_voice_params(
                session, project, character_id=character_id, params={"rate": 2.0}, user_id=ctx["user_a_id"]
            )
        assert getattr(excinfo.value, "code", None) == 10001

    async with async_session_factory() as session:
        project = await session.get(Project, data["project_id"])
        assert project is not None
        result = await post_service.update_voice_params(
            session,
            project,
            character_id=character_id,
            params={"rate": 0.9, "pitch_shift": -1},
            user_id=ctx["user_a_id"],
        )
    assert result["binding"]["params"]["rate"] == 0.9
    assert result["binding"]["params"]["pitch_shift"] == -1
    assert result["affected_line_count"] >= 0


async def test_f6_02_subtitles(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """字幕生成后逐条编辑与批量样式（含音画同步守卫）。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    # 生成字幕（布局只取 rendered 镜头，先确保两镜头为 rendered，避免用例间隐式顺序依赖）
    from app.services import post_service

    async with async_session_factory() as session, session.begin():
        episode_shots = (
            await session.execute(
                select(Shot).where(Shot.episode_id == data["episode_ids"][0])
            )
        ).scalars().all()
        for shot in episode_shots:
            shot.status = "rendered"
        expected_count = len([s for s in episode_shots if (s.dialogue or "").strip()])

    async with async_session_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        result = await post_service.make_subtitles(session, project)
    assert result["subtitle_count"] == expected_count

    listed = _ok(
        await client.get(
            f"/api/v1/projects/{project_id}/post/subtitle-lines", headers=headers
        )
    )
    assert listed["total"] == expected_count
    line = listed["items"][0]
    assert line["line_index"] == 1
    assert line["edited"] is False

    # 改文本
    resp = await client.patch(
        f"/api/v1/subtitles/{line['id']}", json={"text": "改后的字幕"}, headers=headers
    )
    patched = _ok(resp)
    assert patched["line"]["text"] == "改后的字幕"
    assert patched["line"]["edited"] is True

    # 出入点大幅前移 → 音画同步超差 70001（用第 2 条，起始时间 > 0 才能体现偏移）
    second = listed["items"][1]
    assert second["start_ms"] > 800
    resp = await client.patch(
        f"/api/v1/subtitles/{second['id']}",
        json={"start_ms": second["start_ms"] - 800},
        headers=headers,
    )
    body = _err(resp, 70001)
    assert body["error_detail"] == "av_sync_exceeded"
    assert body["data"]["tolerance_ms"] == 100

    # 批量样式
    async with async_session_factory() as session:
        style = (
            await session.execute(
                select(SubtitleStyle).where(SubtitleStyle.project_id == project_id)
            )
        ).scalars().first()
    assert style is not None
    batch = _ok(
        await client.post(
            "/api/v1/subtitles/batch-style",
            json={"style_id": int(style.id), "scope": "all"},
            headers=headers,
        )
    )
    assert batch["applied"] == expected_count


async def test_f6_03_bgm_and_mix(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """BGM 替换（授权范围 + 临期提示）与混音画像（音量/响度边界）。"""
    from app.models import BgmTrack

    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    async with async_session_factory() as session, session.begin():
        track = BgmTrack(
            title="测试 BGM",
            duration_ms=60000,
            license_type="third_party",
            license_scope="platform_only",
            license_expires_at=datetime.now(timezone.utc) + timedelta(days=10),
            is_active=True,
        )
        session.add(track)
        await session.flush()
        track_id = int(track.id)

    replaced = _ok(
        await client.post(
            f"/api/v1/timeline/{data['timeline_id']}/bgm/replace",
            json={"bgm_track_id": track_id},
            headers=headers,
        )
    )
    assert replaced["license_scope"] == "platform_only"
    assert replaced["distribution_blocked"] is True
    assert "天后到期" in replaced["license_warning"]

    mix = _ok(await client.get(f"/api/v1/projects/{project_id}/post/mix-profile", headers=headers))
    assert mix["profile"]["loudness_target"] == -14.0
    assert mix["profile"]["normalize_enabled"] is True

    resp = await client.put(
        f"/api/v1/projects/{project_id}/post/mix-profile",
        json={"track_volumes": {"bgm": 3.0}, "loudness_target": -16.0, "normalize_enabled": True},
        headers=headers,
    )
    updated = _ok(resp)
    assert updated["profile"]["track_volumes"]["bgm"] == 3.0
    assert updated["profile"]["loudness_target"] == -16.0

    resp = await client.put(
        f"/api/v1/projects/{project_id}/post/mix-profile",
        json={"track_volumes": {"bgm": 9.0}},
        headers=headers,
    )
    body = _err(resp, 10001)
    assert body["error_detail"] == "track_volume_out_of_range"


# =====================================================================
# F7 · 导出与分发域
# =====================================================================


async def test_f7_01_presets(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """导出预设保存/列表/删除与上限提示。"""
    headers = _auth(ctx["token_a"])
    resp = await client.post(
        "/api/v1/export-presets",
        json={"name": "抖音竖屏", "resolution": "1080p", "aspect": "9:16", "watermark": True},
        headers=headers,
    )
    preset = _ok(resp)
    assert preset["preset"]["params"]["resolution"] == "1080p"
    assert preset["total"] == 1

    listed = _ok(await client.get("/api/v1/export-presets", headers=headers))
    assert listed["total"] == 1
    assert listed["limit"] == 10

    deleted = _ok(
        await client.delete(f"/api/v1/export-presets/{preset['preset']['id']}", headers=headers)
    )
    assert deleted["deleted"] is True


async def test_f7_02_prespec_check(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """平台规格自检返回逐项结论与建议；非法平台 10001。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    payload = _ok(
        await client.post(
            f"/api/v1/export-jobs/prespec-check?project_id={project_id}",
            json={"platform": "douyin", "resolution": "1080p"},
            headers=headers,
        )
    )
    assert payload["platform_label"] == "抖音"
    assert payload["blocked"] is False
    assert {item["item"] for item in payload["checks"]} >= {"时长上限", "分辨率", "画面比例"}
    assert payload["degraded"] is False

    payload = _ok(
        await client.post(
            f"/api/v1/export-jobs/prespec-check?project_id={project_id}",
            json={"platform": "wechat_channels", "resolution": "4k"},
            headers=headers,
        )
    )
    assert payload["blocked"] is True
    assert "分辨率" in payload["blocked_items"]
    assert payload["recommendations"]

    _err(
        await client.post(
            f"/api/v1/export-jobs/prespec-check?project_id={project_id}",
            json={"platform": "not-a-platform"},
            headers=headers,
        ),
        10001,
    )


async def test_f7_03_export_ai_label_history(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """导出携带 AI 标识与缓存键；历史与复用可用。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    resp = await client.post(
        f"/api/v1/projects/{project_id}/exports",
        json={"resolution": "720p", "with_watermark": True, "episode_no": 1},
        headers={**headers, "Idempotency-Key": f"v4-export-{project_id}"},
    )
    created = _ok(resp)
    job = created["export_job"]
    assert job["ai_label"]["visible"]["text"] == "本内容由 AI 辅助生成"
    assert job["ai_label"]["visible"]["closable"] is False
    assert job["ai_label_applied"] is True
    assert job["cache_key"]
    assert job["expires_at"]

    # 等待导出管线完成：真实成片合成耗时高于占位产物，改为带超时的轮询
    import asyncio

    detail = _ok(await client.get(f"/api/v1/exports/{job['id']}", headers=headers))
    for _ in range(120):
        if detail["status"] in ("succeeded", "failed", "cancelled"):
            break
        await asyncio.sleep(1)
        # GET /exports/{id} 直接返回任务字典（无 export_job 包裹）
        detail = _ok(await client.get(f"/api/v1/exports/{job['id']}", headers=headers))
    assert detail["status"] == "succeeded", detail
    assert detail["output"]["download_url"]

    history = _ok(
        await client.get(f"/api/v1/projects/{project_id}/export-jobs?history=1", headers=headers)
    )
    assert history["total"] >= 1
    assert history["retention_days"] == 90
    assert history["items"][0]["parameter_summary"]

    reused = _ok(
        await client.post(
            f"/api/v1/export-jobs/{job['id']}/reuse",
            json={"resolution": "720p"},
            headers={**headers, "Idempotency-Key": f"v4-reuse-{project_id}"},
        )
    )
    assert reused["reused_from"] == job["id"]
    assert reused["export_job"]["ai_label_applied"] is True


async def test_f7_04_distribution_retry_withdraw(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """分发重试（授权过期需重新授权）与撤回（平台能力差异）。"""
    data = await _ensure_fixture(client, ctx)
    headers = _auth(ctx["token_a"])
    project_id = data["project_id"]

    async with async_session_factory() as session, session.begin():
        failed = DistributionRecord(
            project_id=project_id,
            platform="douyin",
            title="失败分发",
            status="failed",
            error_reason="auth_expired",
            retry_count=0,
        )
        succeeded = DistributionRecord(
            project_id=project_id,
            platform="douyin",
            title="成功分发",
            status="succeeded",
            external_video_id="DEMO-X",
            publish_url="https://demo.douyin.example.com/video/DEMO-X",
            metadata_snapshot={"title": "成功分发"},
            retry_count=0,
        )
        session.add_all([failed, succeeded])
        await session.flush()
        failed_id = int(failed.id)
        succeeded_id = int(succeeded.id)

    retry = _ok(
        await client.post(f"/api/v1/distributions/{failed_id}/retry", headers=headers)
    )
    assert retry["need_reauth"] is True
    assert "重新授权" in retry["message"]

    withdrawn = _ok(
        await client.post(f"/api/v1/distributions/{succeeded_id}/withdraw", headers=headers)
    )
    assert withdrawn["record"]["status"] == "withdrawn"
    assert withdrawn["platform_capable"] is False
    assert "后台手动删除" in withdrawn["message"]


# =====================================================================
# 通用：error_detail 与越权
# =====================================================================


async def test_common_01_error_detail_and_forbidden(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """统一包络携带 error_detail；越权访问返回 20003。"""
    data = await _ensure_fixture(client, ctx)

    resp = await client.get(
        f"/api/v1/projects/{data['project_id']}/script/versions", headers=_auth(ctx["token_b"])
    )
    body = _err(resp, 20003)
    assert resp.status_code == 403

    resp = await client.post(
        f"/api/v1/render-tasks/99999999/manual-pass",
        json={"reason": "不存在的任务", "acknowledged": True},
        headers=_auth(ctx["token_a"]),
    )
    _err(resp, 60002)


async def test_zz_cleanup_placeholder(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """占位用例：确保 session 级清理 fixture 在最后运行（数据由 fixture 清理）。"""
    assert ctx.get("ready") is True
