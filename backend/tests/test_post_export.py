"""后期域 + 导出分发域集成测试（Task 9 · Post & Export API）。

覆盖范围：
1. TTS 试听：tts-preview 返回 audio_url 可下载（GET /files/... 200）；
2. 全剧 TTS：202 → audio clips 落库（与镜头起点对齐）→ post.tts.done；
3. 字幕生成：subtitle clip 与镜头起点对齐（偏差 ≤100ms）；
4. BGM：bgm-recommend 按 emotion 过滤 → PUT bgm 写入 bgm 轨；
5. 合成 + 成片审批：compose → pending_review + 审批单 → decide reject
   （反馈 <10 字 → 10001；≥10 字 → 时间线回 draft）→ 再 compose →
   approve → 时间线 approved + 项目 completed；
6. 导出权益：4k（free）→ 70003（data 携带 current_plan/required_benefit）；
   720p → 202 → succeeded → 下载 200；幂等命中 200；
7. 批量导出：ZIP 可下载且含 2 个占位成片文件；
8. 发行元数据：LLM 生成（失败兜底）字段齐全；
9. 分发：平台 Stub 授权 → distribute → distribution_records 落库；
10. 越权：用户 B 导出 → 20003。

运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_post_export.py -x -q

说明：
- 复用 ``app.main.app``（异常处理器与主控路由已注册），手动挂载本任务
  的 post/export 两个 router（主控聚合器暂未包含）；
- 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）；
- 测试手机号 13500001001（free 500 积分）/13500001002（越权用户），
  fixture 前后自动清理全部业务数据；
- 已渲染镜头直接 DB 插 media_files + shot_versions（跳过渲染链路省时）。
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_session_factory
from app.main import app
from app.models import (
    AiCallLog,
    ApprovalRequest,
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    DistributionRecord,
    Episode,
    ExportJob,
    MediaFile,
    PlatformAccount,
    Project,
    Scene,
    Script,
    ScriptLine,
    Shot,
    ShotVersion,
    Storyboard,
    SubtitleLine,
    Subscription,
    Timeline,
    TimelineClip,
    User,
)

# post / export 路由不在主控聚合器 api_router 内，测试进程内手动挂载
from app.api.export import router as export_router
from app.api.post import router as post_router
from app.api.auth import router as auth_router

from app.infra.event_bus import event_bus

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")

#: 测试手机号
PHONE_A = "13500001001"  # 项目所有者（free，注册赠 500 积分）
PHONE_B = "13500001002"  # 越权用户
TEST_PHONES = (PHONE_A, PHONE_B)

#: 镜头时长（毫秒）
SHOT_DURATION_MS = 3000
#: 两镜头台词（含对白，供 TTS/字幕）
DIALOGUE_1 = "深夜的城市霓虹闪烁，她握紧了手中的钥匙。"
DIALOGUE_2 = "他终于明白了真相背后的沉重代价。"
#: 音画同步容差（毫秒，对齐服务层常量）
AV_SYNC_TOLERANCE_MS = 100


def _ensure_routers() -> None:
    """把本任务的 post/export router 与 auth router 挂载到 app（幂等）。

    主控聚合器 api_router 暂未包含 post/export 路由，测试进程内手动
    include；以各自特征端点路径是否存在判断是否已挂载。
    """
    existing_paths = {getattr(route, "path", "") for route in app.routes}
    markers = {
        "post": f"{settings.API_V1_PREFIX}/subtitle-styles",
        "export": f"{settings.API_V1_PREFIX}/platform-accounts",
        "auth": f"{settings.API_V1_PREFIX}/auth/sms-code",
    }
    if markers["post"] not in existing_paths:
        app.include_router(post_router, prefix=settings.API_V1_PREFIX)
    if markers["export"] not in existing_paths:
        app.include_router(export_router, prefix=settings.API_V1_PREFIX)
    if markers["auth"] not in existing_paths:
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
            await session.execute(delete(DistributionRecord).where(DistributionRecord.project_id.in_(project_ids)))
            await session.execute(delete(ExportJob).where(ExportJob.project_id.in_(project_ids)))
            await session.execute(delete(ApprovalRequest).where(ApprovalRequest.project_id.in_(project_ids)))
            await session.execute(delete(TimelineClip).where(TimelineClip.timeline_id.in_(
                select(Timeline.id).where(Timeline.project_id.in_(project_ids))
            )))
            await session.execute(delete(Timeline).where(Timeline.project_id.in_(project_ids)))
            await session.execute(delete(ShotVersion).where(ShotVersion.shot_id.in_(
                select(Shot.id).where(Shot.project_id.in_(project_ids))
            )))
            await session.execute(delete(Shot).where(Shot.project_id.in_(project_ids)))
            await session.execute(delete(Storyboard).where(Storyboard.project_id.in_(project_ids)))
            await session.execute(delete(Episode).where(Episode.project_id.in_(project_ids)))
            await session.execute(delete(Script).where(Script.project_id.in_(project_ids)))
            await session.execute(delete(AiCallLog).where(AiCallLog.project_id.in_(project_ids)))
            await session.execute(delete(MediaFile).where(MediaFile.project_id.in_(project_ids)))
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
            await session.execute(delete(CreditTransaction).where(CreditTransaction.account_id.in_(account_ids)))
            await session.execute(delete(CreditFreeze).where(CreditFreeze.account_id.in_(account_ids)))
        await session.execute(delete(PlatformAccount).where(PlatformAccount.user_id.in_(user_ids)))
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
    _ensure_routers()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=60.0) as c:
        yield c


@pytest.fixture(scope="session")
def ctx() -> dict[str, Any]:
    """session 级共享上下文：跨用例传递 token / 项目与镜头 ID 等。"""
    return {}


def _auth_headers(token: str) -> dict[str, str]:
    """构造 Bearer 认证请求头。

    Args:
        token: access JWT。

    Returns:
        请求头字典。
    """
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(loop_scope="session", scope="module", autouse=True)
async def realish_tts_stub() -> AsyncIterator[None]:
    """模块级 fixture：把 stub TTS 产物标记为「真实合成」（simulated=False）。

    背景：配音修复后，``sandbox`` 产物（meta.simulated=True）会被
    ``voice_service`` 标记为占位音（billed=False + align_degraded=True），
    不写入 timeline audio 轨，且 compose 被 70005 门禁拦截。本模块的合成 /
    导出 / 分发用例验证的是与配音正交的业务流，需要 TTS 桩产物被视为
    真实合成才能走通，因此在测试进程内把 stub TTS 的 simulated 标记翻转为
    False——不修改 app 代码，也不绕过 voice_service / _sync_voice_track /
    compose 门禁的真实判定逻辑（它们对 simulated=False 的产物走完整链路）。
    占位音门禁行为（不写轨 + 70005 拦截）由 test_12_placeholder_voice_gate
    在临时还原 simulated=True 口径下单独验证。

    Yields:
        无（仅提供测试期间的 TTS「真实化」桩）。
    """
    mp = MonkeyPatch()
    from app.infra.model_gateway.stub import StubProvider

    original_generate = StubProvider.generate

    async def _realish_generate(self: Any, request: Any, model_spec: Any) -> Any:
        """包装 stub 生成：TTS 成功产物把 simulated 翻转为 False。

        Args:
            self: StubProvider 实例。
            request: 统一生成请求。
            model_spec: 模型注册行。

        Returns:
            原始结果（TTS 成功时 meta.simulated=False）。
        """
        result = await original_generate(self, request, model_spec)
        if result.ok and request.capability == "tts":
            result.meta = {**result.meta, "simulated": False}
        return result

    mp.setattr(StubProvider, "generate", _realish_generate)
    try:
        yield
    finally:
        mp.undo()


# ---- 测试数据构造辅助 ----


async def _register_user(client: httpx.AsyncClient, phone: str) -> dict[str, Any]:
    """注册测试用户（赠 500 积分）并返回 {user_id, token}。

    Args:
        client: HTTP 客户端。
        phone: 手机号。

    Returns:
        含 user_id 与 access_token 的字典。
    """
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": phone})
    code = resp.json()["data"]["dev_code"]
    resp = await client.post(
        "/api/v1/auth/register", json={"phone": phone, "sms_code": code, "nickname": f"测试{phone[-2:]}"}
    )
    body = resp.json()
    assert body["code"] == 0, body
    return {"user_id": body["data"]["user"]["id"], "token": body["data"]["access_token"]}


async def _build_fixture(user_id: int) -> dict[str, Any]:
    """构造后期/导出前置数据：项目 + 剧本 + 2 集 + 2 分镜板 + 2 个已渲染镜头。

    每个镜头直接插入 media_files（视频占位，含 poster_key）与
    shot_versions(is_current=True)，跳过渲染链路。

    Args:
        user_id: 项目所有者用户 ID。

    Returns:
        {project_id, shot_ids, episode_ids, storyboard_ids}。
    """
    now = datetime.now(timezone.utc)
    from app.core.ids import new_request_id

    async with async_session_factory() as session, session.begin():
        project = Project(
            owner_user_id=user_id,
            title="后期导出测试项目",
            synopsis="外卖骑手深夜接单，却发现订单地址是十年前被拆掉的老宅",
            genre="urban_romance",
            status="rendering",
        )
        session.add(project)
        await session.flush()

        script = Script(
            project_id=int(project.id),
            version_no=1,
            source_type="generated",
            status="locked",
            title="后期导出测试剧本",
            total_episodes=2,
        )
        session.add(script)
        await session.flush()

        shot_ids: list[int] = []
        episode_ids: list[int] = []
        storyboard_ids: list[int] = []
        dialogues = (DIALOGUE_1, DIALOGUE_2)
        for ep_no in (1, 2):
            episode = Episode(
                script_id=int(script.id),
                project_id=int(project.id),
                episode_no=ep_no,
                title=f"第{ep_no}集：{'钥匙' if ep_no == 1 else '真相'}",
                order_index=ep_no - 1,
                status="locked",
            )
            session.add(episode)
            await session.flush()
            episode_ids.append(int(episode.id))

            # 场次 + 台词行：voice_service.generate_track 为台词驱动（_project_lines
            # 经 Scene→Episode 关联），夹具必须补齐，否则整剧 TTS 无台词可配
            scene = Scene(
                episode_id=int(episode.id),
                project_id=int(project.id),
                scene_no=1,
                title="深夜街头",
                location="城市街道",
                time_of_day="night",
                int_ext="ext",
                order_index=0,
            )
            session.add(scene)
            await session.flush()
            session.add(
                ScriptLine(
                    scene_id=int(scene.id),
                    project_id=int(project.id),
                    line_no=1,
                    line_type="dialogue",
                    speaker_name="主角",
                    content=dialogues[ep_no - 1],
                    emotion_tag="calm",
                    order_index=0,
                )
            )

            storyboard = Storyboard(
                project_id=int(project.id),
                episode_id=int(episode.id),
                version_no=1,
                status="locked",
                shot_count=1,
                locked_at=now,
            )
            session.add(storyboard)
            await session.flush()
            storyboard_ids.append(int(storyboard.id))

            shot = Shot(
                storyboard_id=int(storyboard.id),
                project_id=int(project.id),
                episode_id=int(episode.id),
                shot_no=1,
                order_index=0,
                shot_size="medium",
                camera_move="static",
                emotion_tag="tension",
                duration_ms=SHOT_DURATION_MS,
                dialogue=dialogues[ep_no - 1],
                action_desc=(
                    "女主角在天台握紧钥匙眺望城市" if ep_no == 1 else "男主角在老宅门前明白真相"
                ),
                final_prompt=f"Task9 测试镜头 {ep_no}",
                status="rendered",
            )
            session.add(shot)
            await session.flush()
            shot_ids.append(int(shot.id))

            media = MediaFile(
                project_id=int(project.id),
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=f"test/shots/{new_request_id().lower()}.json",
                url=None,
                media_type="video",
                mime_type="application/json",
                file_size_bytes=0,
                duration_ms=SHOT_DURATION_MS,
                meta={
                    "placeholder": "video",
                    "poster_key": f"test/posters/{new_request_id().lower()}.svg",
                },
            )
            session.add(media)
            await session.flush()
            session.add(
                ShotVersion(
                    shot_id=int(shot.id),
                    version_no=1,
                    media_file_id=int(media.id),
                    is_current=True,
                )
            )

    return {
        "project_id": int(project.id),
        "shot_ids": shot_ids,
        "episode_ids": episode_ids,
        "storyboard_ids": storyboard_ids,
    }


async def _wait_timeline_status(
    client: httpx.AsyncClient,
    token: str,
    project_id: int,
    target_status: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """轮询时间线视图直至 timeline.status 到达目标状态。

    Args:
        client: HTTP 客户端。
        token: 访问令牌。
        project_id: 项目 ID。
        target_status: 目标状态（如 pending_review）。
        timeout: 超时秒数。

    Returns:
        到达目标状态的时间线视图 data。

    Raises:
        TimeoutError: 超时未到达目标状态。
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await client.get(
            f"/api/v1/projects/{project_id}/post/timeline",
            headers=_auth_headers(token),
        )
        body = resp.json()
        if body["code"] == 0:
            last = body["data"]
            if last["timeline"]["status"] == target_status:
                return last
        await asyncio.sleep(0.3)
    raise TimeoutError(f"时间线在 {timeout}s 内未到达状态 {target_status}，最后={last.get('timeline')}")


async def _wait_export_terminal(
    client: httpx.AsyncClient,
    token: str,
    export_job_id: int,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """轮询导出任务直至终态。

    Args:
        client: HTTP 客户端。
        token: 访问令牌。
        export_job_id: 导出任务 ID。
        timeout: 超时秒数。

    Returns:
        终态任务详情。

    Raises:
        TimeoutError: 超时未进入终态。
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/v1/exports/{export_job_id}", headers=_auth_headers(token))
        body = resp.json()
        assert body["code"] == 0, body
        last = body["data"]
        if last["status"] in ("succeeded", "failed"):
            return last
        await asyncio.sleep(0.3)
    raise TimeoutError(f"导出任务 {export_job_id} 在 {timeout}s 内未进入终态：{last}")


async def _events_of(project_id: int) -> list[str]:
    """读取事件总线中指定项目的历史事件类型列表。

    Args:
        project_id: 项目 ID。

    Returns:
        事件类型列表。
    """
    return [r["event"] for r in event_bus._history.get(project_id, [])]  # noqa: SLF001


# ---------------------------------------------------------------------------
# 用例 1：TTS 试听
# ---------------------------------------------------------------------------


async def test_01_tts_preview(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """注册用户 A + 构造项目 → tts-preview 返回 audio_url 可下载。"""
    user = await _register_user(client, PHONE_A)
    ctx["user_a_id"] = int(user["user_id"])
    ctx["token_a"] = user["token"]
    ctx.update(await _build_fixture(ctx["user_a_id"]))
    pid = ctx["project_id"]

    resp = await client.post(
        f"/api/v1/projects/{pid}/post/tts-preview",
        json={},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["audio_url"].startswith("/files/"), body
    assert len(data["lines"]) >= 1, body
    assert data["duration_ms"] > 0, body
    ctx["preview_lines"] = data["lines"]

    # audio_url 可下载（stub WAV 真实落盘）
    dl = await client.get(data["audio_url"])
    assert dl.status_code == 200, dl.status_code
    assert dl.content[:4] == b"RIFF", "下载内容应为 WAV 文件"


# ---------------------------------------------------------------------------
# 用例 2：全剧 TTS → audio clips 落库
# ---------------------------------------------------------------------------


async def test_02_tts_full(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """POST tts → 202 → 轮询 audio clips 落库（2 镜）→ post.tts.done 事件。"""
    pid = ctx["project_id"]
    resp = await client.post(
        f"/api/v1/projects/{pid}/post/tts",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body
    assert body["data"]["status"] == "processing", body
    assert body["data"]["shot_count"] == 2, body

    deadline = time.monotonic() + 30.0
    audio_clips: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        resp = await client.get(
            f"/api/v1/projects/{pid}/post/timeline",
            headers=_auth_headers(ctx["token_a"]),
        )
        body = resp.json()
        if body["code"] == 0:
            audio_clips = [c for c in body["data"]["clips"] if c["track_type"] == "audio"]
            if len(audio_clips) >= 2:
                break
        await asyncio.sleep(0.3)
    assert len(audio_clips) >= 2, body
    ctx["audio_clips"] = audio_clips

    # 音驱布局（_sync_voice_track）：镜头跨度 = max(镜头时长, 配音时长)，
    # 第二镜起点 = 第一镜配音时长（沙箱 TTS 时长由文本估算决定）
    first_clip = min(audio_clips, key=lambda c: c["start_ms"])
    first_span = max(
        SHOT_DURATION_MS, int(first_clip["end_ms"]) - int(first_clip["start_ms"])
    )
    starts = sorted(c["start_ms"] for c in audio_clips)
    assert starts == sorted({0, first_span}), starts

    # audio 源为 voice_clip 片段（voice_service 整集配音 → _sync_voice_track）
    assert all(c["source_type"] == "voice_clip" and c["source_id"] for c in audio_clips), audio_clips

    events = await _events_of(pid)
    assert "post.tts.done" in events, events


# ---------------------------------------------------------------------------
# 用例 3：字幕生成 → 与镜头起点对齐
# ---------------------------------------------------------------------------


async def test_03_subtitles(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """POST subtitles → subtitle clips 起点与镜头起点偏差 ≤100ms。"""
    pid = ctx["project_id"]
    resp = await client.post(
        f"/api/v1/projects/{pid}/post/subtitles",
        json={},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    assert body["data"]["subtitle_count"] == 2, body

    resp = await client.get(
        f"/api/v1/projects/{pid}/post/timeline",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    clips = body["data"]["clips"]
    subtitles = [c for c in clips if c["track_type"] == "subtitle"]
    assert len(subtitles) == 2, clips

    # 字幕逐句对齐配音片段：起点集合 = audio 轨起点集合（音驱布局）
    audio_starts = {c["start_ms"] for c in ctx["audio_clips"]}
    for clip in subtitles:
        deviation = min(abs(clip["start_ms"] - s) for s in audio_starts)
        assert deviation <= AV_SYNC_TOLERANCE_MS, (clip, audio_starts)
        assert isinstance(clip["content"], dict), clip
        assert clip["content"].get("text") in (DIALOGUE_1, DIALOGUE_2), clip
    ctx["subtitle_clips"] = subtitles


# ---------------------------------------------------------------------------
# 用例 4：BGM 推荐与设置
# ---------------------------------------------------------------------------


async def test_04_bgm_recommend_and_set(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """bgm-recommend 按 emotion 过滤（含版权信息）→ PUT bgm 写入 bgm 轨。"""
    pid = ctx["project_id"]
    resp = await client.get(
        f"/api/v1/projects/{pid}/post/bgm-recommend",
        params={"emotion": "tension"},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    items = body["data"]["items"]
    assert 0 < len(items) <= 5, body
    for item in items:
        assert "tension" in (item["mood_tags"] or []), item
        assert item["license_type"] in ("self", "third_party"), item
        assert "license_expires_at" in item, item
    ctx["bgm_track_id"] = items[0]["id"]

    resp = await client.put(
        f"/api/v1/projects/{pid}/post/bgm",
        json={"bgm_track_id": items[0]["id"], "volume_db": -6, "sidechain": True},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    assert body["data"]["bgm"]["id"] == items[0]["id"], body

    resp = await client.get(
        f"/api/v1/projects/{pid}/post/timeline",
        headers=_auth_headers(ctx["token_a"]),
    )
    clips = resp.json()["data"]["clips"]
    bgm_clips = [c for c in clips if c["track_type"] == "bgm"]
    assert len(bgm_clips) == 1, clips
    assert bgm_clips[0]["source_type"] == "bgm", bgm_clips
    assert float(bgm_clips[0]["volume_db"]) == -6.0, bgm_clips
    assert body["data"]["bgm"] is not None, resp.json()


# ---------------------------------------------------------------------------
# 用例 5：合成 + 成片审批（reject → 再 compose → approve → completed）
# ---------------------------------------------------------------------------


async def test_05_compose_and_decide(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """compose → pending_review + 审批单 → reject 回 draft → 再 compose →
    approve → 时间线 approved + 项目 completed。"""
    pid = ctx["project_id"]

    # P1 回归：先把字幕轨与字幕行整体推到错误时间（模拟配音重生成后
    # 未重建字幕），合成阶段必须按最新 audio 轨把字幕拉回对齐。
    async with async_session_factory() as session:
        timeline = (
            (
                await session.execute(
                    select(Timeline).where(
                        Timeline.project_id == pid,
                        Timeline.cut_type == "preview",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert timeline is not None
        stale_clips = (
            (
                await session.execute(
                    select(TimelineClip).where(
                        TimelineClip.timeline_id == int(timeline.id),
                        TimelineClip.track_type == "subtitle",
                    )
                )
            )
            .scalars()
            .all()
        )
        stale_rows = (
            (
                await session.execute(
                    select(SubtitleLine).where(
                        SubtitleLine.timeline_id == int(timeline.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert stale_clips and stale_rows
        for clip in stale_clips:
            clip.start_ms += 1155
            clip.end_ms += 1155
        for row in stale_rows:
            row.start_ms += 1155
            row.end_ms += 1155
            row.edited = False
        await session.commit()

    # 第一次合成
    resp = await client.post(
        f"/api/v1/projects/{pid}/post/compose",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 202, body
    # 时长由配音实测时长驱动，可能长于 Σ镜头时长（TTS 占位音频超长）
    assert body["data"]["duration_ms"] >= SHOT_DURATION_MS * 2, body

    view = await _wait_timeline_status(client, ctx["token_a"], pid, "pending_review")
    assert view["timeline"]["duration_ms"] >= SHOT_DURATION_MS * 2, view
    ctx["timeline_id"] = view["timeline"]["id"]

    async with async_session_factory() as session:
        audio = (
            (
                await session.execute(
                    select(TimelineClip)
                    .where(
                        TimelineClip.timeline_id == ctx["timeline_id"],
                        TimelineClip.track_type == "audio",
                        # 仅对比配音片段（voice_service 整集配音 → source_type=
                        # voice_clip）；compose 阶段会追加 media_file 的 SFX 片段
                        TimelineClip.source_type == "voice_clip",
                    )
                    .order_by(TimelineClip.start_ms.asc())
                )
            )
            .scalars()
            .all()
        )
        subs = (
            (
                await session.execute(
                    select(TimelineClip)
                    .where(
                        TimelineClip.timeline_id == ctx["timeline_id"],
                        TimelineClip.track_type == "subtitle",
                    )
                    .order_by(TimelineClip.start_ms.asc())
                )
            )
            .scalars()
            .all()
        )
        rows = (
            (
                await session.execute(
                    select(SubtitleLine)
                    .where(SubtitleLine.timeline_id == ctx["timeline_id"])
                    .order_by(SubtitleLine.start_ms.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(audio) == len(subs) == len(rows) == 2, (audio, subs, rows)
        for audio_clip, sub_clip, sub_row in zip(audio, subs, rows):
            assert sub_clip.start_ms == audio_clip.start_ms, (audio_clip, sub_clip)
            assert sub_row.start_ms == audio_clip.start_ms, (audio_clip, sub_row)
            assert sub_row.end_ms == audio_clip.end_ms, (audio_clip, sub_row)

    async with async_session_factory() as session:
        approvals = (
            (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.project_id == pid,
                        ApprovalRequest.approval_type == "final_cut",
                        ApprovalRequest.target_type == "timeline",
                        ApprovalRequest.target_id == ctx["timeline_id"],
                        ApprovalRequest.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(approvals) == 1, approvals
    events = await _events_of(pid)
    assert "post.composed" in events, events
    assert "approval.created" in events, events

    # reject：反馈不足 10 字 → 10001
    resp = await client.post(
        f"/api/v1/projects/{pid}/post/decide",
        json={"action": "reject", "feedback": "太短"},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 10001, body

    # reject：反馈 ≥10 字 → 时间线回 draft
    resp = await client.post(
        f"/api/v1/projects/{pid}/post/decide",
        json={"action": "reject", "feedback": "第二集开头节奏偏慢，建议压缩铺垫镜头强化冲突"},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["timeline_status"] == "draft", body

    async with async_session_factory() as session:
        timeline = await session.get(Timeline, ctx["timeline_id"])
        assert timeline is not None and timeline.status == "draft"

    # 第二次合成 → approve
    resp = await client.post(
        f"/api/v1/projects/{pid}/post/compose",
        headers=_auth_headers(ctx["token_a"]),
    )
    assert resp.status_code == 202, resp.json()
    await _wait_timeline_status(client, ctx["token_a"], pid, "pending_review")

    resp = await client.post(
        f"/api/v1/projects/{pid}/post/decide",
        json={"action": "approve"},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["timeline_status"] == "approved", body
    assert body["data"]["project_status"] == "completed", body

    async with async_session_factory() as session:
        timeline = await session.get(Timeline, ctx["timeline_id"])
        assert timeline is not None and timeline.status == "approved"
        project = await session.get(Project, pid)
        assert project is not None and project.status == "completed"
    ctx["project_completed"] = True


# ---------------------------------------------------------------------------
# 用例 6：导出权益校验 + 单集导出全流程
# ---------------------------------------------------------------------------


async def test_06_export_entitlement_and_flow(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """free 导 4k → 70003（data 携带权益）；720p → 202 → succeeded → 下载 200。"""
    pid = ctx["project_id"]

    # 权益不足：free 套餐导 4k → 70003
    resp = await client.post(
        f"/api/v1/projects/{pid}/exports",
        json={"resolution": "4k"},
        headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": "test-export-4k"},
    )
    body = resp.json()
    assert body["code"] == 70003, body
    assert body["data"]["current_plan"] == "free", body
    assert "max_resolution" in body["data"]["required_benefit"], body

    # 合法导出：720p（free 允许，默认带水印）
    idem_key = "test-export-720p-001"
    resp = await client.post(
        f"/api/v1/projects/{pid}/exports",
        json={"resolution": "720p", "with_watermark": True},
        headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": idem_key},
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body
    assert body["data"]["idempotent"] is False, body
    job = body["data"]["export_job"]
    assert job["status"] == "pending", body
    ctx["export_job_id"] = job["id"]

    # 幂等命中：同 key → 200 + 相同任务
    resp = await client.post(
        f"/api/v1/projects/{pid}/exports",
        json={"resolution": "720p"},
        headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": idem_key},
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["data"]["idempotent"] is True, body
    assert body["data"]["export_job"]["id"] == ctx["export_job_id"], body

    # 轮询至 succeeded → 下载 200
    detail = await _wait_export_terminal(client, ctx["token_a"], ctx["export_job_id"])
    assert detail["status"] == "succeeded", detail
    assert detail["resolution"] == "720p" and detail["has_watermark"] is True, detail
    assert detail["output"] and detail["output"]["download_url"].startswith("/files/"), detail

    dl = await client.get(detail["output"]["download_url"])
    assert dl.status_code == 200, dl.status_code
    # P0-1 修复后导出产物是真实合成的 MP4（含 ftyp/moov 结构），不再是占位 JSON
    assert dl.content[4:8] == b"ftyp", "导出产物应为有效 MP4"
    assert len(dl.content) > 1024, "导出 MP4 体积异常"
    ctx["export_download_url"] = detail["output"]["download_url"]

    # 导出列表含该任务
    resp = await client.get(
        f"/api/v1/projects/{pid}/exports",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert any(i["id"] == ctx["export_job_id"] for i in body["data"]["items"]), body

    events = await _events_of(pid)
    assert "export.done" in events, events


# ---------------------------------------------------------------------------
# 用例 7：批量导出 ZIP
# ---------------------------------------------------------------------------


async def test_07_batch_export_zip(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """batch 导出 → zip 可下载且含 2 个可播放 MP4 成片。"""
    pid = ctx["project_id"]
    resp = await client.post(
        f"/api/v1/projects/{pid}/exports/batch",
        json={"resolution": "720p", "with_watermark": True},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["file_count"] == 2, body
    assert data["zip_object_key"].startswith("exports/batch/"), body
    assert len(data["jobs"]) == 2, body

    dl = await client.get(data["download_url"])
    assert dl.status_code == 200, dl.status_code
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        names = zf.namelist()
        assert len(names) == 2, names
        for name in names:
            assert name.startswith("后期导出测试项目-EP"), name
            assert name.endswith(".mp4"), name
            payload = zf.read(name)
            # P0-1/P0-2 修复后批量导出条目为真实合成的 MP4
            assert payload[4:8] == b"ftyp", name
            assert len(payload) > 1024, name


# ---------------------------------------------------------------------------
# 用例 8：发行元数据生成（真实 LLM，失败兜底字段齐全）
# ---------------------------------------------------------------------------


async def test_08_generate_metadata(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """POST exports/metadata → {title, description, tags, cover_suggestions} 字段齐全。"""
    pid = ctx["project_id"]
    resp = await client.post(
        f"/api/v1/projects/{pid}/exports/metadata",
        json={},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert isinstance(data["title"], str) and 0 < len(data["title"]) <= 20, data
    assert isinstance(data["description"], str) and 0 < len(data["description"]) <= 80, data
    assert isinstance(data["tags"], list) and len(data["tags"]) >= 5, data
    assert all(tag.startswith("#") for tag in data["tags"][:5]), data
    assert isinstance(data["cover_suggestions"], list) and len(data["cover_suggestions"]) >= 2, data
    assert data["source"] in ("llm", "fallback"), data


# ---------------------------------------------------------------------------
# 用例 9：平台授权 + 分发
# ---------------------------------------------------------------------------


async def test_09_platform_authorize_and_distribute(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """Stub OAuth 授权 → distribute（2s 模拟上传）→ distribution_records 落库。"""
    pid = ctx["project_id"]

    # 未授权先分发 → 40001
    resp = await client.post(
        f"/api/v1/projects/{pid}/distribute",
        json={
            "platform": "douyin",
            "metadata": {"title": "测试标题", "description": "测试简介", "tags": ["#短剧"]},
        },
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 40001, body

    # Stub 授权
    resp = await client.post(
        "/api/v1/platform-accounts",
        json={"platform": "douyin", "mock_authorize": True},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    account = body["data"]
    assert account["status"] == "active", body
    assert account["platform"] == "douyin", body
    assert account["token_expires_at"], body
    ctx["platform_account_id"] = account["id"]

    resp = await client.get("/api/v1/platform-accounts", headers=_auth_headers(ctx["token_a"]))
    body = resp.json()
    assert body["code"] == 0 and body["data"]["total"] >= 1, body

    # 分发（含 2s 模拟上传）
    resp = await client.post(
        f"/api/v1/projects/{pid}/distribute",
        json={
            "platform": "douyin",
            "metadata": {"title": "深夜钥匙", "description": "一单深夜订单引出十年谜团", "tags": ["#短剧"]},
            "export_job_id": ctx["export_job_id"],
        },
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["status"] == "succeeded", body
    assert data["external_video_id"].startswith("DEMO-"), body
    assert data["publish_url"].startswith("https://demo.douyin."), body

    # 分发记录落库 + 列表
    async with async_session_factory() as session:
        records = (
            (
                await session.execute(
                    select(DistributionRecord).where(DistributionRecord.project_id == pid)
                )
            )
            .scalars()
            .all()
        )
        assert len(records) == 1, records
        assert records[0].status == "succeeded"
        assert int(records[0].export_job_id) == ctx["export_job_id"]
    resp = await client.get(
        f"/api/v1/projects/{pid}/distributions",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["total"] == 1, body

    # 删除平台授权
    resp = await client.delete(
        f"/api/v1/platform-accounts/{ctx['platform_account_id']}",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["deleted"] is True, body


# ---------------------------------------------------------------------------
# 用例 10：越权 → 20003
# ---------------------------------------------------------------------------


async def test_10_forbidden_cross_user(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """用户 B 对用户 A 项目发起导出 → 20003（HTTP 403）。"""
    user_b = await _register_user(client, PHONE_B)
    resp = await client.post(
        f"/api/v1/projects/{ctx['project_id']}/exports",
        json={"resolution": "720p"},
        headers={**_auth_headers(user_b["token"]), "Idempotency-Key": "test-export-b"},
    )
    body = resp.json()
    assert resp.status_code == 403, body
    assert body["code"] == 20003, body


# ---------------------------------------------------------------------------
# 用例 11（补充）：预览地址与字幕样式列表
# ---------------------------------------------------------------------------


async def test_11_preview_url_and_subtitle_styles(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """preview-url 返回水印标记与时间线状态；subtitle-styles 列表可用。"""
    pid = ctx["project_id"]
    resp = await client.get(
        f"/api/v1/projects/{pid}/post/preview-url",
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    data = body["data"]
    assert data["url"].startswith("/files/"), body
    assert data["watermark"] is True, body
    assert data["timeline_status"] == "approved", body

    resp = await client.get(
        "/api/v1/subtitle-styles",
        params={"project_id": pid},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] >= 1, body


# ---------------------------------------------------------------------------
# 用例 12：占位音门禁（配音修复专项回归）
# ---------------------------------------------------------------------------


async def test_12_placeholder_voice_gate(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """占位音门禁：sandbox 配音不写 audio 轨 + compose 被 70005 拦截。

    在临时还原 ``simulated=True``（占位音口径）的独立项目上验证：
    1. 整集 TTS 202 → voice-coverage 返回 placeholder_lines 非空；
    2. timeline audio 轨 0 条（占位音禁止进入混流）；
    3. compose 返回业务码 70005 且 data.missing_shots 指明缺失镜头。
    """
    reg = await _register_user(
        client, f"135{(int(time.time() * 1000) % 100_000_000):08d}"
    )
    fx = await _build_fixture(reg["user_id"])
    pid = fx["project_id"]
    headers = _auth_headers(reg["token"])

    # 在 realish_tts_stub 之上再包一层：把 TTS 产物恢复为占位口径
    from app.infra.model_gateway.stub import StubProvider

    realish_generate = StubProvider.generate

    async def _placeholder_generate(self: Any, request: Any, model_spec: Any) -> Any:
        """包装 realish 桩：TTS 成功产物把 simulated 还原为 True。

        Args:
            self: StubProvider 实例。
            request: 统一生成请求。
            model_spec: 模型注册行。

        Returns:
            原始结果（TTS 成功时 meta.simulated=True，即占位音口径）。
        """
        result = await realish_generate(self, request, model_spec)
        if result.ok and request.capability == "tts":
            result.meta = {**result.meta, "simulated": True}
        return result

    mp = MonkeyPatch()
    mp.setattr(StubProvider, "generate", _placeholder_generate)
    try:
        resp = await client.post(f"/api/v1/projects/{pid}/post/tts", headers=headers)
        body = resp.json()
        assert resp.status_code == 202, body

        # 轮询 voice-coverage 直至 placeholder_lines 非空（TTS 管线落库）
        deadline = time.monotonic() + 30.0
        cov: dict[str, Any] = {}
        while time.monotonic() < deadline:
            r = await client.get(
                f"/api/v1/projects/{pid}/post/voice-coverage", headers=headers
            )
            if r.status_code == 200:
                cov = r.json().get("data") or {}
                if cov.get("placeholder_lines"):
                    break
            await asyncio.sleep(0.3)
        assert cov.get("placeholder_lines"), cov
        # 占位音按修复口径不计入 voiced 统计：expected 全部为占位、voiced=0
        assert int(cov.get("expected_line_count") or 0) >= 1, cov
        assert int(cov.get("voiced_line_count") or 0) == 0, cov
        assert int(cov.get("voiced_shots") or 0) == 0, cov

        # 占位音不得进入 timeline audio 轨
        resp = await client.get(
            f"/api/v1/projects/{pid}/post/timeline", headers=headers
        )
        body = resp.json()
        assert body["code"] == 0, body
        audio_clips = [
            c for c in body["data"]["clips"] if c["track_type"] == "audio"
        ]
        assert audio_clips == [], audio_clips

        # compose 被门禁拦截（业务码 70005，HTTP 200 包装）
        resp = await client.post(
            f"/api/v1/projects/{pid}/post/compose", headers=headers
        )
        body = resp.json()
        assert body["code"] == 70005, body
        assert body["data"]["missing_shots"], body
        assert body["data"]["placeholder_lines"], body
    finally:
        mp.undo()
