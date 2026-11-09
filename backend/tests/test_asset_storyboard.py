"""资产域 + 分镜域 API 端到端测试模块（Task 7b）。

覆盖范围（真实 LLM 用例超时 300s）：
0. 单元级：consistency_service 伪向量（维度/归一化/确定性/余弦/阈值判定）；
1. 资产 plan（真实 LLM）→ characters/scenes/props 创建 + 二次幂等复用；
2. generate portrait → 202 → 轮询 pending_review → versions/media_files/
   qc_reports/face_embeddings 落库断言；
3. generate turnaround_front → QC 相似度分数区间 [0.70, 0.95] 断言；
4. reject（feedback ≥10 字）→ regenerate 新版本（旧版本保留）；
5. lock：缺三视图 → 60003；补齐 side/back → locked；
6. 锚点绑定未锁资产 → 50001（分镜镜头 + 未锁场景资产）；
7. storyboard generate（真实 LLM）→ 轮询 pending_review → shots 字段
   齐全（shot_size/camera_move 枚举合法、duration_ms 2000-6000）+ 幂等；
8. reorder → order_index 重算 + 钩子位移提示；
9. lock 无锚点 → 60005；绑锚点后 lock → 审批单产生；decide reject →
   解锁；再 lock → decide approve → 项目状态 rendering；
10. draft-preview 结构 + layout 保存。

测试账号：手机号 13700000801，用例结束统一清理（资产/媒体/项目级联/
授权留痕/用户）。运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_asset_storyboard.py -x -q
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_engine, async_session_factory
from app.core.security import create_access_token
from app.main import app
from app.models.asset import Asset, FaceAuthorization, FaceEmbedding, MediaFile
from app.models.collab import ApprovalRequest
from app.models.iam import User
from app.models.project import Project
from app.models.render import AiCallLog
from app.models.script import Episode, Scene, Script, ScriptLine
from app.models.storyboard import Shot, Storyboard, StoryboardLayout
from app.services.consistency_service import (
    EMBEDDING_DIM,
    cosine_similarity,
    make_deterministic_embedding,
    qc_face_check,
)

pytestmark = pytest.mark.asyncio

#: 测试用户手机号
PHONE = "13700000801"

# ---- 路由手动挂载（主控统一聚合 app/api/__init__.py 前的测试用装配） ----

_MOUNTED = False


def _ensure_routers_mounted() -> None:
    """将资产/分镜路由手动挂载到 main.app（幂等，已挂载则跳过）。"""
    global _MOUNTED
    if _MOUNTED:
        return
    from app.api.assets import router as assets_router
    from app.api.storyboards import router as storyboards_router

    paths = {getattr(route, "path", "") for route in app.router.routes}
    if f"{settings.API_V1_PREFIX}/tts-voices" not in paths:
        app.include_router(assets_router, prefix=settings.API_V1_PREFIX)
        app.include_router(storyboards_router, prefix=settings.API_V1_PREFIX)
    _MOUNTED = True


# ---- 工具与夹具 ----


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """每个测试前后丢弃引擎连接池，规避 asyncpg 连接绑定过期事件循环。"""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


async def _ensure_user(phone: str, nickname: str, balance: int = 1_000_000) -> User:
    """确保测试用户与积分账户存在（幂等）。

    Args:
        phone: 用户手机号。
        nickname: 用户昵称。
        balance: 初始积分余额。

    Returns:
        用户 ORM 实例（已提交）。
    """
    from app.models.billing import CreditAccount

    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.phone == phone))
        ).scalar_one_or_none()
        if user is None:
            user = User(phone=phone, nickname=nickname, register_source="web")
            session.add(user)
            await session.flush()
        account = (
            await session.execute(
                select(CreditAccount).where(
                    CreditAccount.owner_type == "user", CreditAccount.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if account is None:
            session.add(
                CreditAccount(owner_type="user", user_id=user.id, balance_credits=balance)
            )
        else:
            account.balance_credits = balance
        await session.commit()
        await session.refresh(user)
        return user


async def _cleanup_users(user_ids: list[int]) -> None:
    """按依赖顺序清理测试产生的全部数据。

    顺序：真人授权（user RESTRICT）→ 资产（项目 SET NULL，级联版本/向量）
    → 媒体（项目 SET NULL）→ AI 调用日志 → 项目（级联剧本/分镜/审批等）
    → 用户。

    Args:
        user_ids: 待清理的用户 ID 列表。
    """
    if not user_ids:
        return
    async with async_session_factory() as session:
        project_ids = (
            (
                await session.execute(
                    select(Project.id).where(Project.owner_user_id.in_(user_ids))
                )
            )
            .scalars()
            .all()
        )
        if project_ids:
            await session.execute(
                delete(Asset)
                .where(Asset.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(MediaFile)
                .where(MediaFile.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(AiCallLog)
                .where(AiCallLog.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(Project)
                .where(Project.id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
        await session.execute(
            delete(FaceAuthorization)
            .where(FaceAuthorization.user_id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(User)
            .where(User.id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.commit()


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[User, httpx.AsyncClient]]:
    """提供测试用户与 ASGI 客户端，用例结束统一清理用户全部数据。

    Yields:
        (测试用户, httpx 异步客户端) 元组。
    """
    _ensure_routers_mounted()
    user = await _ensure_user(PHONE, "资产分镜测试")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        try:
            yield user, http
        finally:
            await _cleanup_users([int(user.id)])


def _auth(user: User) -> dict[str, str]:
    """构造指定用户的 Bearer 认证头。

    Args:
        user: 用户 ORM 实例。

    Returns:
        Authorization 请求头字典。
    """
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _wait_for(
    predicate_and_fetch, *, timeout: float = 300.0, interval: float = 2.0
) -> Any:
    """轮询等待条件成立（asyncio sleep + 查库）。

    Args:
        predicate_and_fetch: 异步无参函数，条件成立返回非 None 值。
        timeout: 超时秒数。
        interval: 轮询间隔秒数。

    Returns:
        条件成立的返回值。

    Raises:
        TimeoutError: 超时仍未满足。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await predicate_and_fetch()
        if result is not None:
            return result
        await asyncio.sleep(interval)
    raise TimeoutError("轮询超时")


# =====================================================================
# 0. 单元级：consistency_service
# =====================================================================


async def test_consistency_service_unit() -> None:
    """伪向量单元断言：维度/归一化/确定性/余弦/阈值判定。"""
    emb1 = make_deterministic_embedding("浮光造梦：林晚，25-30，黑长直发，白色护士服")
    emb2 = make_deterministic_embedding("浮光造梦：林晚，25-30，黑长直发，白色护士服")
    assert len(emb1) == EMBEDDING_DIM
    assert emb1 == emb2, "同输入必须产出同向量（确定性）"
    norm = math.sqrt(sum(v * v for v in emb1))
    assert abs(norm - 1.0) < 1e-9, "向量必须 L2 归一化"
    assert cosine_similarity(emb1, emb2) > 0.999
    assert cosine_similarity(emb1, emb1) == 1.0
    # QC 判定阈值
    score, result = qc_face_check(emb1, emb2, {})
    assert result == "passed" and score > 0.99
    # 人为构造低相似度对：正交向量的判定
    half = EMBEDDING_DIM // 2
    ortho = [0.0] * EMBEDDING_DIM
    for i in range(half):
        ortho[i] = emb1[half + i]
        ortho[half + i] = -emb1[i]
    _, result_low = qc_face_check(emb1, ortho, {})
    assert result_low == "failed"
    # warning 区间（构造 score=0 落在 [-0.1, 0.1) 的场景）
    _, result_warn = qc_face_check(emb1, ortho, {"face_pass_threshold": 0.1, "face_warn_low": -0.1})
    assert result_warn == "warning"


# =====================================================================
# 1-10. 端到端主链路（真实 LLM）
# =====================================================================


async def test_asset_storyboard_full_pipeline(env: tuple[User, httpx.AsyncClient]) -> None:
    """端到端：资产 plan → 生成/QC/审核/锁定 → 分镜生成/重排/锚点/审批。"""
    user, client = env
    base = settings.API_V1_PREFIX

    # ---- 建项目 ----
    resp = await client.post(
        f"{base}/projects",
        json={
            "title": "资产分镜测试短剧",
            "synopsis": "实习医生在深夜急诊室遇到一位声称能预知死亡的女孩",
            "genre": "urban_suspense",
            "target_episode_count": 1,
            "budget_credits": 100000,
        },
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    project_id = int(body["data"]["id"])

    # ---- 直插剧本（真实 LLM 仅用于 plan 与分镜导演） ----
    async with async_session_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        script = Script(
            project_id=project_id, version_no=1, source_type="generated",
            status="locked", title="死亡预知者", logline="能预知死亡的女孩与实习医生的深夜对峙",
            total_episodes=1,
        )
        session.add(script)
        await session.flush()
        project.current_script_id = script.id
        episode = Episode(
            script_id=script.id, project_id=project_id, episode_no=1,
            title="第一集：午夜急诊室", order_index=0,
            synopsis="林晚在深夜急诊室遇见自称能预知死亡的苏晴", hook_text="走廊尽头的手表停了",
        )
        session.add(episode)
        await session.flush()
        scene = Scene(
            episode_id=episode.id, project_id=project_id, scene_no=1,
            title="急诊室走廊", location="市一院急诊科", time_of_day="night", int_ext="int",
            order_index=0,
        )
        session.add(scene)
        await session.flush()
        line_rows = [
            ScriptLine(scene_id=scene.id, project_id=project_id, line_no=1, line_type="narration",
                       content="深夜的急诊室走廊，灯光惨白，一位实习医生抱着病历本匆匆走过。", order_index=0),
            ScriptLine(scene_id=scene.id, project_id=project_id, line_no=2, line_type="dialogue",
                       speaker_name="林晚", content="你真的能看见死亡吗？", order_index=1, emotion_tag="fear"),
            ScriptLine(scene_id=scene.id, project_id=project_id, line_no=3, line_type="dialogue",
                       speaker_name="苏晴", content="从七岁那年开始，我能看见每个人头顶的倒计时。", order_index=2),
            ScriptLine(scene_id=scene.id, project_id=project_id, line_no=4, line_type="action",
                       content="苏晴抬起手腕，指着桌上一块停在三点整的旧怀表。", order_index=3),
            ScriptLine(scene_id=scene.id, project_id=project_id, line_no=5, line_type="dialogue",
                       speaker_name="林晚", content="那……我的倒计时还剩多久？", order_index=4, emotion_tag="tension"),
        ]
        session.add_all(line_rows)
        await session.commit()

    # ---- 1. 资产 plan（真实 LLM）→ characters/scenes/props 创建 ----
    resp = await client.post(f"{base}/projects/{project_id}/assets/plan", headers=_auth(user))
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0 and body["data"]["status"] == "accepted", body

    from app.infra.event_bus import event_bus

    async def _plan_done() -> dict[str, Any] | None:
        """轮询条件：assets.planned 事件出现且资产已落库。"""
        for record in list(event_bus._history.get(project_id, [])):  # noqa: SLF001
            if record["event"] == "assets.planned":
                return record["data"]
        return None

    planned = await _wait_for(_plan_done, timeout=300, interval=3)
    assert planned is not None, "assets.planned 事件未发布"

    async def _fetch_assets(asset_type: str) -> list[dict[str, Any]]:
        """按类型拉取资产列表 data。

        Args:
            asset_type: 资产类型。

        Returns:
            列表 items。
        """
        resp = await client.get(
            f"{base}/projects/{project_id}/assets",
            params={"asset_type": asset_type, "page_size": 50},
            headers=_auth(user),
        )
        body = resp.json()
        assert body["code"] == 0, body
        return body["data"]["items"]

    characters = await _fetch_assets("character")
    scenes_assets = await _fetch_assets("scene")
    props = await _fetch_assets("prop")
    assert len(characters) >= 1, "角色资产未创建"
    assert len(scenes_assets) >= 1, "场景资产未创建"
    assert len(props) >= 1, "道具资产未创建（剧本中已埋怀表道具）"
    hero = max(characters, key=lambda a: int((a.get("meta") or {}).get("weight") or 0))
    hero_id = int(hero["id"])
    assert hero["status"] == "planning"
    scene_asset_id = int(scenes_assets[0]["id"])

    # 幂等：重复 plan 复用同名资产不重复创建
    before_total = len(characters) + len(scenes_assets) + len(props)
    resp = await client.post(f"{base}/projects/{project_id}/assets/plan", headers=_auth(user))
    assert resp.status_code == 202, resp.json()

    async def _plan_idempotent_done() -> dict[str, Any] | None:
        """轮询条件：第二次 assets.planned 事件出现且 reused > 0。"""
        events = [
            r["data"] for r in list(event_bus._history.get(project_id, []))  # noqa: SLF001
            if r["event"] == "assets.planned"
        ]
        if len(events) >= 2 and int(events[-1].get("reused") or 0) > 0:
            return events[-1]
        return None

    second = await _wait_for(_plan_idempotent_done, timeout=300, interval=3)
    assert second is not None, "二次 plan 未复用已有资产"
    after_total = len(await _fetch_assets("character")) + len(await _fetch_assets("scene")) + len(await _fetch_assets("prop"))
    assert after_total == before_total, f"幂等 plan 重复创建资产：{before_total} → {after_total}"

    # ---- 2. generate portrait → 202 → 轮询 pending_review ----
    resp = await client.post(
        f"{base}/assets/{hero_id}/generate",
        json={"version_type": "portrait"},
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 202 and body["code"] == 0, body

    async def _asset_status(target: str) -> dict[str, Any] | None:
        """轮询条件：资产详情状态达到 target。"""
        resp = await client.get(f"{base}/assets/{hero_id}", headers=_auth(user))
        body = resp.json()
        assert body["code"] == 0, body
        return body["data"] if body["data"]["status"] == target else None

    detail = await _wait_for(lambda: _asset_status("pending_review"), timeout=120, interval=2)
    assert detail is not None
    portrait_versions = [v for v in detail["versions"] if v["version_type"] == "portrait"]
    assert portrait_versions, "portrait 版本未落库"
    assert portrait_versions[0]["is_current"] is True
    assert portrait_versions[0]["media_url"], "portrait 媒体 URL 缺失"
    assert detail["qc_reports"], "QC 报告未落库"

    async with async_session_factory() as session:
        media = await session.get(MediaFile, portrait_versions[0]["media_file_id"])
        assert media is not None and media.media_type == "image"
        assert (media.object_key or "").startswith("stub/")
        emb_rows = (
            await session.execute(select(FaceEmbedding).where(FaceEmbedding.asset_id == hero_id))
        ).scalars().all()
        assert emb_rows, "face_embeddings 未落库"
        vec = list(emb_rows[-1].embedding)
        assert len(vec) == EMBEDDING_DIM
        assert abs(math.sqrt(sum(v * v for v in vec)) - 1.0) < 1e-6

    # ---- 3. generate turnaround_front → QC 分数区间 [0.70, 0.95] ----
    resp = await client.post(
        f"{base}/assets/{hero_id}/generate",
        json={"version_type": "turnaround_front"},
        headers=_auth(user),
    )
    assert resp.status_code == 202, resp.json()
    detail = await _wait_for(lambda: _asset_status("pending_review"), timeout=120, interval=2)
    front_versions = [v for v in detail["versions"] if v["version_type"] == "turnaround_front"]
    assert front_versions, "turnaround_front 版本未落库"
    front_qc_id = front_versions[0]["qc_report_id"]
    front_qc = next(q for q in detail["qc_reports"] if q["id"] == front_qc_id)
    assert front_qc["result"] in ("passed", "warning")
    assert front_qc["consistency_score"] is not None
    assert 0.70 <= float(front_qc["consistency_score"]) <= 0.95, (
        f"伪相似度分数超出 0.70-0.95 区间：{front_qc['consistency_score']}"
    )

    # ---- 4. reject 后 regenerate 新版本（旧版本保留） ----
    resp = await client.post(
        f"{base}/assets/{hero_id}/reject",
        json={"feedback": "表情僵硬，眼神要更柔和一些"},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["status"] == "rejected", body
    assert (body["data"]["meta"] or {}).get("reject_feedback"), "驳回反馈未存 meta"

    resp = await client.post(
        f"{base}/assets/{hero_id}/regenerate",
        json={"version_type": "turnaround_front", "feedback": "表情更自然，肩膀放松"},
        headers=_auth(user),
    )
    assert resp.status_code == 202, resp.json()
    detail = await _wait_for(lambda: _asset_status("pending_review"), timeout=120, interval=2)
    front_v2 = [v for v in detail["versions"] if v["version_type"] == "turnaround_front"]
    assert len(front_v2) == 2, f"重生成应产生 v2 且保留 v1，实际 {len(front_v2)} 个版本"
    assert front_v2[0]["version_no"] == 2 and front_v2[1]["version_no"] == 1

    # ---- 5. lock：缺三视图 → 60003；补齐 → locked ----
    resp = await client.post(f"{base}/assets/{hero_id}/lock", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 60003, f"缺三视图应 60003，实际 {body}"

    for vt in ("turnaround_side", "turnaround_back"):
        resp = await client.post(
            f"{base}/assets/{hero_id}/generate", json={"version_type": vt}, headers=_auth(user),
        )
        assert resp.status_code == 202, resp.json()
        detail = await _wait_for(lambda: _asset_status("pending_review"), timeout=120, interval=2)
    assert detail is not None and detail["status"] == "pending_review"

    resp = await client.post(f"{base}/assets/{hero_id}/lock", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0 and body["data"]["status"] == "locked", body
    assert body["data"]["locked_at"], "locked_at 未写入"

    # ---- 场景资产：generate scene_ref → approve → lock ----
    resp = await client.post(
        f"{base}/assets/{scene_asset_id}/generate",
        json={"version_type": "scene_ref"},
        headers=_auth(user),
    )
    assert resp.status_code == 202, resp.json()

    async def _scene_ready() -> dict[str, Any] | None:
        """轮询条件：场景资产进入 pending_review。"""
        resp = await client.get(f"{base}/assets/{scene_asset_id}", headers=_auth(user))
        body = resp.json()
        assert body["code"] == 0, body
        return body["data"] if body["data"]["status"] == "pending_review" else None

    scene_detail = await _wait_for(_scene_ready, timeout=120, interval=2)
    resp = await client.post(f"{base}/assets/{scene_asset_id}/approve", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0 and body["data"]["status"] == "approved", body
    resp = await client.post(f"{base}/assets/{scene_asset_id}/lock", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0 and body["data"]["status"] == "locked", body

    # 音色绑定与授权留痕
    resp = await client.get(f"{base}/tts-voices", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0 and body["data"]["total"] >= 1, body
    voice_id = int(body["data"]["items"][0]["id"])
    resp = await client.put(
        f"{base}/assets/{hero_id}/voice", json={"voice_id": voice_id}, headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["voice_id"] == voice_id, body
    resp = await client.post(
        f"{base}/assets/{hero_id}/face-authorization",
        json={"authorization_type": "self", "agreement_version": "v1.0"},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body

    # ---- 7. storyboard generate（真实 LLM）→ pending_review → shots 校验 ----
    resp = await client.post(
        f"{base}/projects/{project_id}/storyboards/generate",
        json={"episode_no": 1},
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 202 and body["code"] == 0, body

    async def _storyboard_pending() -> Storyboard | None:
        """轮询条件：该集分镜板进入 pending_review。"""
        async with async_session_factory() as session:
            return (
                await session.execute(
                    select(Storyboard)
                    .where(Storyboard.project_id == project_id, Storyboard.status == "pending_review")
                    .order_by(Storyboard.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    sb = await _wait_for(_storyboard_pending, timeout=300, interval=3)
    sb_id = int(sb.id)
    assert sb.shot_count >= 1, "镜头未落库"

    resp = await client.get(f"{base}/storyboards/{sb_id}", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0, body
    shots = body["data"]["shots"]
    assert len(shots) == sb.shot_count
    _valid_sizes = {"extreme_wide", "wide", "medium", "close_up", "extreme_close_up", "over_shoulder"}
    _valid_moves = {"static", "pan", "tilt", "dolly_in", "dolly_out", "tracking", "orbit", "handheld", "crane"}
    for index, shot in enumerate(shots):
        assert shot["shot_size"] in _valid_sizes, f"景别非法：{shot['shot_size']}"
        assert shot["camera_move"] in _valid_moves, f"运镜非法：{shot['camera_move']}"
        assert 2000 <= int(shot["duration_ms"]) <= 6000, f"时长越界：{shot['duration_ms']}"
        assert shot["image_prompt"], "image_prompt 缺失"
        assert int(shot["order_index"]) == index, "order_index 未按序排列"

    # 幂等：重复 generate 同集 → 200 + idempotent
    resp = await client.post(
        f"{base}/projects/{project_id}/storyboards/generate",
        json={"episode_no": 1},
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 200 and body["data"]["idempotent"] is True, body

    # ---- PATCH 镜头：合法与非法 duration ----
    first_shot_id = int(shots[0]["id"])
    resp = await client.patch(
        f"{base}/shots/{first_shot_id}", json={"duration_ms": 2500, "camera_move": "pan"},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["duration_ms"] == 2500, body
    resp = await client.patch(f"{base}/shots/{first_shot_id}", json={"duration_ms": 999}, headers=_auth(user))
    body = resp.json()
    assert body["code"] == 10001, f"duration_ms < 1000 应 10001，实际 {body}"
    resp = await client.patch(f"{base}/shots/{first_shot_id}", json={"duration_ms": 20000}, headers=_auth(user))
    body = resp.json()
    assert body["code"] == 10001, f"duration_ms > 15000 应 10001，实际 {body}"

    # ---- 8. reorder → order_index 重算 ----
    resp = await client.post(
        f"{base}/storyboards/{sb_id}/shots/reorder",
        json={"shot_id": first_shot_id, "insert_after_shot_id": int(shots[1]["id"])},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    new_order = body["data"]["shots"]
    assert [item["order_index"] for item in new_order] == list(range(len(new_order))), "order_index 未重算"
    assert new_order[1]["shot_id"] == first_shot_id, "镜头未移动到目标位置"
    assert isinstance(body["data"]["hook_position_changed"], bool)

    # ---- 6. 锚点绑定未锁资产 → 50001（未锁角色资产） ----
    unlocked_character = next(a for a in characters if int(a["id"]) != hero_id)
    resp = await client.post(
        f"{base}/shots/{first_shot_id}/anchors",
        json={"character_asset_ids": [int(unlocked_character["id"])]},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 50001, f"未锁资产绑锚点应 50001，实际 {body}"

    # ---- 9a. lock 缺锚点镜头 → 60005（含缺锚点清单） ----
    # 注：分镜生成管线会自动绑定角色/场景锚点（P0 修复），此处先清空
    # 首镜锚点，再验证锁定门禁对缺锚点镜头的拦截。
    async with async_session_factory() as session, session.begin():
        shot_row = await session.get(Shot, first_shot_id)
        assert shot_row is not None
        shot_row.character_asset_ids = None
        shot_row.scene_asset_id = None
    resp = await client.post(f"{base}/storyboards/{sb_id}/lock", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 60005, f"无锚点锁定应 60005，实际 {body}"
    assert body["data"]["missing_shots"], "60005 未携带缺锚点镜头清单"

    # ---- 9b. 绑锚点（主角 + 已锁场景）→ 全部镜头至少 1 锚点 ----
    for index, shot in enumerate(shots):
        payload: dict[str, Any] = {"scene_asset_id": scene_asset_id}
        if index == 0:
            payload = {"character_asset_ids": [hero_id], "scene_asset_id": scene_asset_id}
        resp = await client.post(f"{base}/shots/{int(shot['id'])}/anchors", json=payload, headers=_auth(user))
        body = resp.json()
        assert body["code"] == 0, body
    resp = await client.post(
        f"{base}/shots/{first_shot_id}/anchors",
        json={"character_asset_ids": [hero_id], "scene_asset_id": scene_asset_id},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert str(hero["name"]) in (body["data"]["final_prompt"] or ""), (
        f"final_prompt 未注入主角外观：{body['data']['final_prompt']}"
    )

    # 锚点影响面
    resp = await client.get(f"{base}/assets/{hero_id}/anchor-impact", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0 and int(body["data"]["affected_shots"]) >= 1, body

    # ---- 9c. lock → 审批单产生 ----
    resp = await client.post(f"{base}/storyboards/{sb_id}/lock", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0, body
    approval_id = int(body["data"]["approval_id"])

    async with async_session_factory() as session:
        sb_row = await session.get(Storyboard, sb_id)
        assert sb_row is not None and sb_row.status == "locked" and sb_row.locked_at is not None
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.status == "pending"
        assert approval.approval_type == "storyboard" and int(approval.target_id) == sb_id

    # ---- 10. draft-preview 结构断言（锁定态仍可预览） ----
    resp = await client.get(f"{base}/storyboards/{sb_id}/draft-preview", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0, body
    preview = body["data"]
    assert preview["shots"], "预览镜头列表为空"
    total_ms = 0
    for item in preview["shots"]:
        assert {"shot_id", "order_index", "keyframe_url", "subtitle", "audio_url", "duration_ms", "emotion_tag"} <= set(item)
        assert item["audio_url"] is None, "音频应由后续任务填充"
        assert item["subtitle"], "subtitle 缺失"
        total_ms += int(item["duration_ms"])
    assert int(preview["total_duration_ms"]) == total_ms

    # ---- 9d. decide reject（feedback ≥10 字）→ 解锁 ----
    resp = await client.post(
        f"{base}/storyboards/{sb_id}/decide",
        json={"action": "reject", "feedback": "后半段节奏太赶，补两个过渡镜头"},
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 202 and body["code"] == 0, body
    async with async_session_factory() as session:
        sb_row = await session.get(Storyboard, sb_id)
        assert sb_row is not None and sb_row.status == "pending_review" and sb_row.locked_at is None
        approval = await session.get(ApprovalRequest, approval_id)
        assert approval is not None and approval.status == "rejected"
        assert approval.feedback and "过渡镜头" in approval.feedback

    # ---- 9e. 再 lock → decide approve → 项目状态 rendering ----
    resp = await client.post(f"{base}/storyboards/{sb_id}/lock", headers=_auth(user))
    body = resp.json()
    assert body["code"] == 0, body
    second_approval_id = int(body["data"]["approval_id"])
    assert second_approval_id != approval_id, "应产生新的审批单"

    async with async_session_factory() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        project.status = "storyboard_review"  # 模拟前置流程已推进到分镜审核
        await session.commit()

    resp = await client.post(
        f"{base}/storyboards/{sb_id}/decide",
        json={"action": "approve"},
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 202 and body["code"] == 0, body

    async def _project_rendering() -> Project | None:
        """轮询条件：项目状态推进到 rendering。"""
        async with async_session_factory() as session:
            row = await session.get(Project, project_id)
            return row if row is not None and row.status == "rendering" else None

    project = await _wait_for(_project_rendering, timeout=60, interval=2)
    async with async_session_factory() as session:
        sb_row = await session.get(Storyboard, sb_id)
        assert sb_row is not None and sb_row.status == "locked", "approve 后分镜应保持 locked"
        approval = await session.get(ApprovalRequest, second_approval_id)
        assert approval is not None and approval.status == "approved"

    # ---- 布局保存（upsert） ----
    resp = await client.get(f"{base}/storyboards/{sb_id}", headers=_auth(user))
    shots = resp.json()["data"]["shots"]
    nodes = [
        {"shot_id": int(s["id"]), "x": float(i % 4), "y": float(i // 4), "w": 1.0, "h": 2.2}
        for i, s in enumerate(shots)
    ]
    resp = await client.post(
        f"{base}/storyboards/{sb_id}/layout",
        json={"nodes": nodes, "viewport": {"x": 0, "y": 0, "zoom": 0.8}},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["saved"] is True, body
    resp = await client.post(
        f"{base}/storyboards/{sb_id}/layout",
        json={"nodes": nodes},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    async with async_session_factory() as session:
        layout_rows = (
            await session.execute(
                select(StoryboardLayout).where(StoryboardLayout.storyboard_id == sb_id)
            )
        ).scalars().all()
        assert len(layout_rows) == 1, "布局应按用户维度 upsert（唯一一行）"
