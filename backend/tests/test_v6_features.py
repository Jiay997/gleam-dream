"""V6.4 增量功能集成测试（F9 / F2.12 / F4.13 / F10 / F11 / 5A / F2.13 / F3.14）。

覆盖范围（对齐《17 V6.4 进度评估与偏差分析报告》缺口清单）：

1. **F2.12 台词清单**：清单查询 / 编辑 / 拆分 / 时长预估；
2. **F4.13 台词绑定**：镜头↔台词绑定读写 / 音频驱动时长重算 / 集级时长报告；
3. **F9 配音轨**：角色音色参数确认 / 样音 / 逐句生成 / 轨道详情 / 关口 V 审批；
4. **F10 合成编排**：分镜同步 / 双视图 / 节点边增删 / 结构守卫 / 编译幂等 /
   主路径回写 / 历史计划；
5. **F11 参数中心**：生效参数 / 危险参数保存与审计 / 版本 / 回滚 / 默认恢复 /
   重评估；
6. **5A 与 F2.13**：Story Bible 写入与完整度 / 连续性事实重建 / L1+L2 校验
   降级为 unavailable / 报告 / 逻辑规则 / 豁免 / 结构化反馈与反例库；
7. **F3.14 照片合成角色**：四道闸（静态/去重/内容/合规）/ 批次级联拦截 /
   身份档 / M1 合成 / 确认落资产。

运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_v6_features.py -x -q
"""

from __future__ import annotations

import hashlib
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.db import async_session_factory
from app.main import app
from app.models import (
    Asset,
    AssetVersion,
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    Episode,
    PlatformAccount,
    Project,
    Scene,
    Script,
    ScriptLine,
    Shot,
    Storyboard,
    Subscription,
    TtsVoice,
    User,
)

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")

#: 测试手机号（V6 专用段，避免与 V4 用例冲突）
TEST_PHONES = ("13700003001", "13700003002")


async def _cleanup() -> None:
    """清理测试用户及其项目数据（Project 级联删除 V6 增量表）。

    Returns:
        无返回值。

    Raises:
        无（数据库异常向上抛出，由测试框架处理）。
    """
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
                delete(AssetVersion).where(
                    AssetVersion.asset_id.in_(
                        select(Asset.id).where(Asset.project_id.in_(project_ids))
                    )
                )
            )
            await session.execute(delete(Asset).where(Asset.project_id.in_(project_ids)))
            await session.execute(delete(Project).where(Project.id.in_(project_ids)))
        await session.execute(delete(PlatformAccount).where(PlatformAccount.user_id.in_(user_ids)))
        await session.execute(delete(Subscription).where(Subscription.user_id.in_(user_ids)))
        await session.execute(delete(CreditFreeze).where(CreditFreeze.account_id.in_(
            select(CreditAccount.id).where(CreditAccount.user_id.in_(user_ids))
        )))
        await session.execute(delete(CreditTransaction).where(CreditTransaction.account_id.in_(
            select(CreditAccount.id).where(CreditAccount.user_id.in_(user_ids))
        )))
        await session.execute(delete(CreditAccount).where(CreditAccount.user_id.in_(user_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))


@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    """session 级清理 fixture：跑前清残留、跑后清数据。"""
    await _cleanup()
    yield
    await _cleanup()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def ctx() -> AsyncIterator[dict[str, Any]]:
    """session 级共享上下文：承载首个用例构造的项目与登录令牌。

    Yields:
        空字典（首个用例注册用户并填充资源 ID）。
    """
    data: dict[str, Any] = {}
    yield data


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """session 级 HTTP 客户端：ASGITransport 直连 app。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=120.0
    ) as http_client:
        yield http_client


async def _register(client: httpx.AsyncClient, phone: str) -> dict[str, Any]:
    """注册测试用户并返回会话信息。

    Args:
        client: HTTP 客户端。
        phone: 测试手机号。

    Returns:
        含 ``user_id`` 与 ``token`` 的字典。
    """
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": phone})
    code = resp.json()["data"]["dev_code"]
    resp = await client.post(
        "/api/v1/auth/register",
        json={"phone": phone, "sms_code": code, "nickname": "V6测试"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    return {"user_id": body["data"]["user"]["id"], "token": body["data"]["access_token"]}


def _auth(token: str) -> dict[str, str]:
    """构造 Bearer 认证请求头。

    Args:
        token: access JWT。

    Returns:
        请求头字典。
    """
    return {"Authorization": f"Bearer {token}"}


async def _build_fixture(user_id: int) -> dict[str, Any]:
    """构造 V6 测试前置数据（项目/剧本/台词/分镜/角色/音色）。

    Args:
        user_id: 项目所有者用户 ID。

    Returns:
        资源 ID 字典（project_id/episode_id/line_ids/shot_ids/asset_id/voice_id）。
    """
    async with async_session_factory() as session, session.begin():
        project = Project(
            owner_user_id=user_id,
            title="V6 增量测试项目",
            synopsis="配音与编排增量验证",
            genre="urban_romance",
            status="storyboarding",
            aspect_ratio="9:16",
            target_episode_count=1,
        )
        session.add(project)
        await session.flush()
        project_id = int(project.id)

        script = Script(
            project_id=project_id,
            version_no=1,
            source_type="generated",
            status="locked",
            title="V6 测试剧本",
            logline="一句话梗概",
            genre="urban_romance",
            total_episodes=1,
            total_words=60,
            structure={"episodes": []},
        )
        session.add(script)
        await session.flush()
        project.current_script_id = int(script.id)

        episode = Episode(
            script_id=int(script.id),
            project_id=project_id,
            episode_no=1,
            title="第1集：试探",
            synopsis="天台试探",
            target_duration_seconds=90,
            est_duration_sec=90,
            order_index=0,
            status="confirmed",
        )
        session.add(episode)
        await session.flush()
        episode_id = int(episode.id)

        scene = Scene(
            episode_id=episode_id,
            project_id=project_id,
            scene_no=1,
            title="天台夜谈",
            location="天台",
            time_of_day="night",
            int_ext="ext",
            summary="两人对峙",
            order_index=0,
        )
        session.add(scene)
        await session.flush()
        scene_id = int(scene.id)

        asset = Asset(
            project_id=project_id,
            owner_user_id=user_id,
            asset_type="character",
            name="林晚",
            description="女主",
            status="approved",
            source="ai_generated",
            labels=["女主"],
        )
        session.add(asset)
        await session.flush()
        asset_id = int(asset.id)

        voice = TtsVoice(
            voice_key=f"v6-voice-{project_id}",
            name="V6 测试音色",
            gender="female",
            language="zh-CN",
            status="available",
        )
        session.add(voice)
        await session.flush()
        voice_id = int(voice.id)
        asset.voice_id = voice_id

        line_ids: list[int] = []
        for index, (line_type, speaker, content) in enumerate(
            [
                ("dialogue", "林晚", "你到底在那天晚上看见了什么？"),
                ("dialogue", "林晚", "我不明白你在说什么，我什么都不知道。"),
                ("action", None, "林晚转身看向远处的城市灯火，手指微微发抖。"),
            ]
        ):
            line = ScriptLine(
                scene_id=scene_id,
                project_id=project_id,
                line_no=index + 1,
                line_type=line_type,
                speaker_name=speaker,
                character_asset_id=asset_id if speaker else None,
                content=content,
                emotion_tag="tension",
                order_index=index,
            )
            session.add(line)
            await session.flush()
            line_ids.append(int(line.id))

        storyboard = Storyboard(
            project_id=project_id,
            episode_id=episode_id,
            version_no=1,
            status="approved",
            shot_count=2,
            estimated_duration_ms=6000,
        )
        session.add(storyboard)
        await session.flush()

        shot_ids: list[int] = []
        for shot_no in (1, 2):
            shot = Shot(
                storyboard_id=int(storyboard.id),
                project_id=project_id,
                episode_id=episode_id,
                scene_id=scene_id,
                shot_no=shot_no,
                order_index=shot_no - 1,
                shot_size="medium",
                camera_move="static",
                emotion_tag="tension",
                duration_ms=3000,
                dialogue="对白",
                image_prompt="关键帧 prompt",
                final_prompt="最终 prompt",
                character_asset_ids=[asset_id],
                asset_anchor_ids=[],
                scene_asset_id=None,
                status="rendered",
            )
            session.add(shot)
            await session.flush()
            shot_ids.append(int(shot.id))

        return {
            "project_id": project_id,
            "episode_id": episode_id,
            "scene_id": scene_id,
            "line_ids": line_ids,
            "shot_ids": shot_ids,
            "asset_id": asset_id,
            "voice_id": voice_id,
        }


def _photo_payload(
    batch_key: str,
    index: int,
    *,
    mime: str = "image/jpeg",
    sha256: str | None = None,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造照片上传登记请求体。

    Args:
        batch_key: 批次键。
        index: 照片序号（用于构造唯一文件名与指纹）。
        mime: MIME 类型。
        sha256: 内容指纹（空则按序号生成）。
        analysis: 模型分析结果（空则使用可通过内容闸的默认值）。

    Returns:
        请求体字典。
    """
    default_analysis = {
        "face_count": 1,
        "blur_score": 0.9,
        "lighting_score": 0.85,
        "pose_yaw": 5,
        "pose_pitch": 2,
        "is_public_figure": False,
        "minor_confidence": 0.1,
        "is_sensitive": False,
    }
    return {
        "batch_key": batch_key,
        "file_name": f"photo_{index}.jpg",
        "mime": mime,
        "size_bytes": 1024 * 1024,
        "width": 1200,
        "height": 1600,
        "sha256": sha256 or hashlib.sha256(f"{batch_key}-{index}".encode()).hexdigest(),
        "declaration": "self",
        "authorization_confirmed": True,
        "analysis": analysis or default_analysis,
    }


async def test_f212_lines_and_f413_bindings(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """验证 F2.12 台词清单与 F4.13 绑定 + 音频驱动时长。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值（断言失败即测试失败）。
    """
    user = await _register(client, TEST_PHONES[0])
    ctx.update(await _build_fixture(int(user["user_id"])))
    ctx["token"] = user["token"]
    headers = _auth(user["token"])
    pid = ctx["project_id"]
    ep_no = 1

    # —— 台词清单 ——
    resp = await client.get(f"/api/v1/projects/{pid}/episodes/{ep_no}/lines", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    items = body["data"]["items"]
    assert len(items) == 3
    assert all(item.get("line_key") for item in items)
    assert all(item.get("est_ms") for item in items)

    line_id = ctx["line_ids"][0]
    # —— 编辑台词 ——
    resp = await client.patch(
        f"/api/v1/projects/{pid}/lines/{line_id}",
        headers=headers,
        json={"content": "你那天晚上到底看见了什么？", "emotion_tag": "fear"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["content"].startswith("你那天晚上")

    # —— 拆分超长台词 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/lines/{line_id}/split",
        headers=headers,
        json={"position": 5},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["source"]["content"] and body["data"]["created"]["content"]

    # —— 时长预估 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/lines/{line_id}/estimate",
        headers=headers,
        json={"content": "这是一句用于试算的较长台词内容。", "rate": 1.0},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["est_ms"] > 0

    # —— 镜头绑定 ——
    shot_id = ctx["shot_ids"][0]
    resp = await client.put(
        f"/api/v1/projects/{pid}/shots/{shot_id}/line-bindings",
        headers=headers,
        json={
            "bindings": [
                {"line_id": ctx["line_ids"][0], "offset_ms": 0, "order_index": 0},
                {"line_id": ctx["line_ids"][1], "offset_ms": 600, "order_index": 1},
            ]
        },
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert len(body["data"]["bindings"]) == 2

    resp = await client.get(
        f"/api/v1/projects/{pid}/shots/{shot_id}/line-bindings", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["line_coverage_pct"] > 0

    # —— 音频驱动时长重算（未配音时按预估） ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/shots/{shot_id}/recalculate-duration",
        headers=headers,
        json={"force_manual": False, "max_shot_ms": 15000},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["duration_ms"] >= 1000

    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/recalculate-duration",
        headers=headers,
        json={"force_manual": False, "max_shot_ms": 15000},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert "changed_shot_ids" in body["data"] and "report" in body["data"]

    resp = await client.get(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/duration-report", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert "unbound_lines" in body["data"]


async def test_f9_voice_track(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """验证 F9 配音轨：参数确认、样音、逐句生成、轨迹与关口 V。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值。
    """
    headers = _auth(ctx["token"])
    pid = ctx["project_id"]
    asset_id = ctx["asset_id"]
    ep_no = 1

    # —— 确认音色参数 ——
    resp = await client.put(
        f"/api/v1/projects/{pid}/characters/{asset_id}/voice-params",
        headers=headers,
        json={"voice_id": ctx["voice_id"], "rate": 1.0, "pitch": 0, "emotion_default": "tension"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["status"] == "active"

    resp = await client.get(
        f"/api/v1/projects/{pid}/characters/{asset_id}/voice-params", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["voice_id"] == ctx["voice_id"]

    # —— 样音 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/characters/{asset_id}/voice/sample",
        headers=headers,
        json={"character_asset_id": asset_id, "text": "试听一句台词。", "rate": 1.0, "pitch": 0},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["duration_ms"] > 0

    # —— 逐句生成整集 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/voice/generate",
        headers=headers,
        json={"line_ids": [], "force": False},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["generated"] >= 3
    assert body["data"]["track"]["status"] in ("voice_ready", "voice_drift")

    # —— 轨道详情 ——
    resp = await client.get(f"/api/v1/projects/{pid}/episodes/{ep_no}/voice/track", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert len(body["data"]["clips"]) >= 3
    assert all(clip["audio_url"] for clip in body["data"]["clips"])
    assert body["data"]["clips"][0]["words"]

    # —— 气口调整 ——
    resp = await client.patch(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/voice/track",
        headers=headers,
        json={"gap_ms": 150},
    )
    body = resp.json()
    assert body["code"] == 0, body

    # —— 报告 ——
    resp = await client.get(f"/api/v1/projects/{pid}/episodes/{ep_no}/voice/report", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["line_count"] >= 3

    # —— 关口 V 审批 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/voice/approve",
        headers=headers,
        json={"decision": "approve", "comment": "配音整体可用"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["track"]["status"] == "voice_locked"


async def test_f10_composition(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """验证 F10 合成编排：同步、双视图、节点边、校验、编译幂等与回写。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值。
    """
    headers = _auth(ctx["token"])
    pid = ctx["project_id"]
    ep_no = 1

    # —— 从分镜同步编排图 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/sync", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert len(body["data"]["nodes"]) >= 2

    # —— 双视图详情 ——
    resp = await client.get(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition?view=track", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["view"] == "track"
    assert body["data"]["validation"]["valid"] is True

    # —— 切换视图 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/view",
        headers=headers,
        json={"to_view": "node"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert "lossless" in body["data"]

    # —— 新增标题节点与边 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/nodes",
        headers=headers,
        json={"node_type": "title", "label": "片头标题", "params": {"text": "第1集"}},
    )
    body = resp.json()
    assert body["code"] == 0, body
    title_node_id = int(body["data"]["id"])

    node_list = (await client.get(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition?view=node", headers=headers
    )).json()["data"]["nodes"]
    clip_nodes = [n for n in node_list if n["node_type"] == "clip"]
    assert clip_nodes
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/edges",
        headers=headers,
        json={
            "from_node_id": title_node_id,
            "to_node_id": int(clip_nodes[0]["id"]),
            "transition": "dissolve",
            "duration_ms": 300,
        },
    )
    body = resp.json()
    assert body["code"] == 0, body
    edge_id = int(body["data"]["id"])

    # —— 更新节点 / 删除边 ——
    resp = await client.patch(
        f"/api/v1/projects/{pid}/composition/nodes/{title_node_id}",
        headers=headers,
        json={"label": "片头标题（已调）", "params": {"text": "第1集 试探"}},
    )
    assert resp.json()["code"] == 0, resp.json()

    resp = await client.delete(
        f"/api/v1/projects/{pid}/composition/edges/{edge_id}", headers=headers
    )
    assert resp.json()["code"] == 0, resp.json()

    resp = await client.delete(
        f"/api/v1/projects/{pid}/composition/nodes/{title_node_id}", headers=headers
    )
    assert resp.json()["code"] == 0, resp.json()

    # —— 编译（幂等） ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/compile",
        headers=headers,
        json={"preview": False, "force": False},
    )
    body = resp.json()
    assert body["code"] == 0, body
    plan_hash = body["data"]["plan_hash"]
    assert body["data"]["plan"]["segments"]

    # —— 双视图等价性（Spike A）：切换视图不改变编译结果 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/view",
        headers=headers,
        json={"to_view": "track"},
    )
    body = resp.json()
    assert body["code"] == 0, body

    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/compile",
        headers=headers,
        json={"preview": False, "force": False},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["plan_hash"] == plan_hash
    assert body["data"]["reused"] is True

    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/compile",
        headers=headers,
        json={"preview": False, "force": False},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["plan_hash"] == plan_hash
    assert body["data"]["reused"] is True

    # —— 主路径回写（未确认时返回影响清单） ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/apply-order",
        headers=headers,
        json={"confirm_impacts": False},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert "requires_confirmation" in body["data"]

    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/apply-order",
        headers=headers,
        json={"confirm_impacts": True},
    )
    body = resp.json()
    assert body["code"] == 0, body

    # —— 历史计划 ——
    resp = await client.get(
        f"/api/v1/projects/{pid}/episodes/{ep_no}/composition/plans", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] >= 1


async def test_f11_params(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """验证 F11 参数中心：定义、保存、审计、版本、回滚与恢复默认。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值。
    """
    headers = _auth(ctx["token"])
    pid = ctx["project_id"]

    # —— 生效参数（自动种子 40 项定义） ——
    resp = await client.get(f"/api/v1/projects/{pid}/params?include_defs=true", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert len(body["data"]["groups"]) == 8
    assert body["data"]["values"]["storyboard.max_shot_ms"] == 15000

    # —— 危险参数缺少理由 → 拒绝 ——
    resp = await client.put(
        f"/api/v1/projects/{pid}/params",
        headers=headers,
        json={"changes": {"generation.concurrent_tasks": 6}, "reason": "太短", "scope": "project", "scope_id": "0"},
    )
    body = resp.json()
    assert body["code"] != 0, body

    # —— 危险参数保存（≥10 字理由） ——
    resp = await client.put(
        f"/api/v1/projects/{pid}/params",
        headers=headers,
        json={
            "changes": {"generation.concurrent_tasks": 6},
            "reason": "本机资源充足，提高并发生成数以缩短整体出片时间",
            "scope": "project",
            "scope_id": "0",
        },
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["version"] >= 1
    assert body["data"]["changed"]["generation.concurrent_tasks"] == 6

    # —— 合规锁定参数不可改 ——
    resp = await client.put(
        f"/api/v1/projects/{pid}/params",
        headers=headers,
        json={
            "changes": {"compliance.watermark_required": False},
            "reason": "尝试关闭水印用于内部测试，验证合规锁定是否生效",
            "scope": "project",
            "scope_id": "0",
        },
    )
    body = resp.json()
    assert body["code"] == 20003, body

    # —— 版本与审计 ——
    resp = await client.get(f"/api/v1/projects/{pid}/params/versions", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] >= 1
    version_no = body["data"]["items"][-1]["version_no"]

    resp = await client.get(f"/api/v1/projects/{pid}/params/audits", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] >= 1

    # —— 回滚 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/params/rollback",
        headers=headers,
        json={"version_no": version_no, "reason": "回滚到并发数调整前的稳定配置"},
    )
    body = resp.json()
    assert body["code"] == 0, body

    # —— 恢复默认 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/params/restore",
        headers=headers,
        json={"param_keys": ["generation.concurrent_tasks"], "reason": "恢复默认并发数"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    resp = await client.get(f"/api/v1/projects/{pid}/params", headers=headers)
    body = resp.json()
    assert body["data"]["values"]["generation.concurrent_tasks"] == 4

    # —— 重评估（只读对照） ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/params/reassess",
        headers=headers,
        json={"ep_no": 1, "target": "shot"},
    )
    body = resp.json()
    assert body["code"] == 0, body


async def test_5a_bible_continuity_feedback(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """验证 5A Story Bible、连续性校验降级与结构化反馈。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值。
    """
    headers = _auth(ctx["token"])
    pid = ctx["project_id"]

    # —— Story Bible 角色/场景/道具 ——
    resp = await client.put(
        f"/api/v1/projects/{pid}/story-bible/characters",
        headers=headers,
        json={
            "char_key": "lin_wan",
            "name": "林晚",
            "char_asset_id": ctx["asset_id"],
            "base_look": "黑色长风衣，齐肩短发",
            "body_desc": "偏瘦，身高 168",
            "current_state": "alive",
            "outfits": [{"scene": 1, "desc": "黑色长风衣"}],
        },
    )
    body = resp.json()
    assert body["code"] == 0, body

    resp = await client.put(
        f"/api/v1/projects/{pid}/episodes/1/scenes/1/story-bible",
        headers=headers,
        json={
            "location": "天台",
            "key_points": [{"name": "水塔"}, {"name": "护栏"}],
            "time_of_day": "night",
            "elapsed_minutes": 0,
            "weather": "晴",
            "lighting_base": "冷蓝夜景",
        },
    )
    body = resp.json()
    assert body["code"] == 0, body

    resp = await client.put(
        f"/api/v1/projects/{pid}/story-bible/props",
        headers=headers,
        json={
            "prop_key": "letter",
            "name": "旧信",
            "state_machine": [{"state": "sealed"}, {"state": "opened"}],
            "holder_by_scene": {"1": "lin_wan"},
        },
    )
    body = resp.json()
    assert body["code"] == 0, body

    resp = await client.get(f"/api/v1/projects/{pid}/story-bible", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert len(body["data"]["characters"]) >= 1
    assert "completeness" in body["data"]

    # —— 连续性事实重建 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/continuity/facts/rebuild",
        headers=headers,
        json={"ep_no": 1, "overwrite": True},
    )
    body = resp.json()
    assert body["code"] == 0, body

    resp = await client.get(f"/api/v1/projects/{pid}/continuity/facts?ep_no=1", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] >= 1

    # —— L1 + L2 校验（无最终产出时记 unavailable，不冒充通过） ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/continuity/checks",
        headers=headers,
        json={"ep_no": 1, "shot_ids": [], "use_bible": True},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["summary"]["unavailable"] >= 0
    assert body["data"]["degraded"] is True

    resp = await client.get(f"/api/v1/projects/{pid}/continuity/report?ep_no=1", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert "dimensions" in body["data"] or "summary" in body["data"]

    # —— F2.13 逻辑规则 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/episodes/1/logic-check",
        headers=headers,
        json={"ep_no": 1, "rule_codes": []},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert "issues" in body["data"]

    # —— 豁免 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/logic-waivers",
        headers=headers,
        json={"rule_code": "LR-2", "target_ref": "ep1:scene1", "reason": "该处为刻意的场景跳切，用于表现时间流逝"},
    )
    body = resp.json()
    assert body["code"] == 0, body

    resp = await client.get(f"/api/v1/projects/{pid}/logic-waivers", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] == 1

    # —— 结构化反馈（同维度 3 次自动沉淀反例） ——
    for round_no in range(3):
        resp = await client.post(
            f"/api/v1/projects/{pid}/feedback",
            headers=headers,
            json={
                "target_type": "shot",
                "target_id": ctx["shot_ids"][0],
                "dimensions": ["face"],
                "scope": {"episode": 1},
                "expectation": "keep_identity",
                "expectation_text": "保持同一张脸",
                "keep_items": ["构图"],
                "text": f"第 {round_no + 1} 次反馈：脸型偏差明显",
                "source_gate": "gate1",
            },
        )
        body = resp.json()
        assert body["code"] == 0, body

    resp = await client.get(
        f"/api/v1/projects/{pid}/feedback?target_type=shot&target_id={ctx['shot_ids'][0]}",
        headers=headers,
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] == 3

    resp = await client.get(f"/api/v1/projects/{pid}/negative-examples", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] >= 1


async def test_f314_photo_composition(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """验证 F3.14 照片四道闸、批次级联拦截、身份档与 M1 合成确认。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值。
    """
    headers = _auth(ctx["token"])
    pid = ctx["project_id"]
    batch_key = "v6-batch-ok"

    # —— 合法照片 4 张 ——
    photo_ids: list[int] = []
    for index in range(1, 5):
        resp = await client.post(
            f"/api/v1/projects/{pid}/photo-batches/uploads",
            headers=headers,
            json=_photo_payload(batch_key, index),
        )
        body = resp.json()
        assert body["code"] == 0, body
        assert body["data"]["photo"]["status"] in ("usable", "degraded")
        photo_ids.append(int(body["data"]["photo"]["id"]))

    # —— 去重闸 ——
    duplicate_payload = _photo_payload(batch_key, 1)
    resp = await client.post(
        f"/api/v1/projects/{pid}/photo-batches/uploads",
        headers=headers,
        json=duplicate_payload,
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["photo"]["reject_code"] == "F314_DUPLICATE"
    assert body["data"]["photo"]["is_duplicate"] is True

    # —— 静态闸：非法格式 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/photo-batches/uploads",
        headers=headers,
        json=_photo_payload("v6-batch-bad", 1, mime="image/gif"),
    )
    body = resp.json()
    assert body["code"] == 50004, body

    # —— 合规闸：公众人物 → 整批阻断 ——
    risky = _photo_payload("v6-batch-risk", 1)
    risky["analysis"] = {**risky["analysis"], "is_public_figure": True}
    resp = await client.post(
        f"/api/v1/projects/{pid}/photo-batches/uploads", headers=headers, json=risky
    )
    body = resp.json()
    assert body["code"] == 50003, body

    resp = await client.get(f"/api/v1/projects/{pid}/photo-batches/v6-batch-risk", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["blocked"] is True

    # —— 批次详情 ——
    resp = await client.get(f"/api/v1/projects/{pid}/photo-batches/{batch_key}", headers=headers)
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["blocked"] is False
    assert body["data"]["usable"] >= 3

    # —— 身份档 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/identity-profiles",
        headers=headers,
        json={"batch_key": batch_key, "name": "林晚（照片合成）", "photo_ids": photo_ids},
    )
    body = resp.json()
    assert body["code"] == 0, body
    profile_id = int(body["data"]["id"])
    assert body["data"]["main_photo_id"]

    # —— M1 合成 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/character-compositions",
        headers=headers,
        json={"profile_id": profile_id, "stylization": 45},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["status"] == "preview_ready"
    assert body["data"]["self_check"]["passed"] is True
    composition_id = int(body["data"]["id"])

    # —— 确认落资产 ——
    resp = await client.post(
        f"/api/v1/projects/{pid}/character-compositions/{composition_id}/confirm?name=照片合成角色",
        headers=headers,
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["asset_id"]
    assert body["data"]["composition"]["status"] == "confirmed"


async def test_f313_anchor_pack(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """验证 F3.13 多维锚点包组装与渲染入参哈希包含锚点包。

    Args:
        client: HTTP 客户端。
        ctx: session 级共享上下文。

    Returns:
        无返回值。
    """
    headers = _auth(ctx["token"])
    pid = ctx["project_id"]
    shot_id = ctx["shot_ids"][0]

    resp = await client.get(
        f"/api/v1/projects/{pid}/shots/{shot_id}/anchor-pack", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    pack = body["data"]
    assert pack["shot_id"] == shot_id
    assert pack["pack_hash"]
    # 测试夹具未建人脸 embedding / 场景资产 / 服装设定，缺项应显式标注
    assert pack["missing"]
    assert pack["degradation"]["level"] in ("face", "outfit", "scene", "prop")
    assert "人脸" in pack["degradation"]["note"]

    resp = await client.get(
        f"/api/v1/projects/{pid}/episodes/1/anchor-packs", headers=headers
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] == len(ctx["shot_ids"])

    # 渲染入参哈希必须随锚点包变化，保证幂等键能感知生成约束变化
    from app.services.render_service import build_input_hash

    base = build_input_hash("prompt", 3000, [1], [2], "pack-a")
    changed = build_input_hash("prompt", 3000, [1], [2], "pack-b")
    assert base != changed
