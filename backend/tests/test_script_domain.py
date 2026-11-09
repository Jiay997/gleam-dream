"""剧本域 API 与解析/编辑/改写/审核端到端测试模块（Task 7a，真实 LLM）。

覆盖范围：
1. 上传 txt 剧本（2 集 4 场样例）→ 202 → 轮询 parse_status=parsed
   （≤300s）→ 断言 episodes≥2、行类型齐全（dialogue/narration/action）、
   confidence_items 存在；
2. GET current → 树结构层级字段完整；
3. PATCH script-lines → version+1、operation_logs 落 diff 记录；
4. POST rewrite（feedback≥5 字）→ changed_lines 非空、版本推进；
5. GET diff?against=1 → changed 非空；
6. POST submit-review（正常内容）→ result=passed、moderation_records 落库；
7. 越权：用户 B 向用户 A 项目上传 → HTTP 403 / code=20003。

测试账号：手机号 13700000701（A，Owner）/ 13700000702（B，越权用），
用例按定义顺序执行，末尾用例统一清理全部数据。
LLM 用例标注 ``@pytest.mark.llm_live``（默认执行；轮询自带 300s 超时
保护）。运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_script_domain.py -x -q
"""

from __future__ import annotations

import asyncio
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
from app.models.asset import MediaFile
from app.models.billing import CreditAccount
from app.models.iam import AuditLog, OperationLog, ProjectMember, User
from app.models.project import Project
from app.models.quality import ComplianceFlag, ModerationRecord

pytestmark = pytest.mark.asyncio

#: 测试用户 A 手机号（项目 Owner）
PHONE_A = "13700000701"
#: 测试用户 B 手机号（越权测试用）
PHONE_B = "13700000702"

#: 解析轮询超时（秒，LLM 真实调用保护）
PARSE_TIMEOUT_SECONDS = 300
#: 轮询间隔（秒）
POLL_INTERVAL_SECONDS = 3

#: 模块级缓存：跨用例复用已解析上下文（LLM 解析一次，后续用例复用）
_STATE: dict[str, Any] = {}

# ---- 路由手动挂载（主控统一聚合 app/api/__init__.py 前的测试用装配） ----

_MOUNTED = False


def _ensure_routers_mounted() -> None:
    """将剧本/文档路由手动挂载到 main.app（幂等，已挂载则跳过）。"""
    global _MOUNTED
    if _MOUNTED:
        return
    from app.api.documents import router as documents_router
    from app.api.scripts import router as scripts_router

    paths = {getattr(route, "path", "") for route in app.router.routes}
    if f"{settings.API_V1_PREFIX}/documents/{{document_id}}" not in paths:
        app.include_router(scripts_router, prefix=settings.API_V1_PREFIX)
        app.include_router(documents_router, prefix=settings.API_V1_PREFIX)
    _MOUNTED = True


# ---- 测试剧本样例（2 集 4 场，含对白/旁白/动作行） ----

# 注：样例剧本须避开真实 LLM 内容审核（compliance）的 flag 词面
# （跟踪/监视/事故等悬疑元素会被判 flag → verdict=review，
# 导致 test_06 的 verdict=pass 断言失败），故使用治愈向题材。
SAMPLE_SCRIPT = """《巷口猫星人》

第 1 集 巷口的常客

第 1 场 咖啡店 日 内
林晚把刚烤好的司康摆进橱窗，店里飘着咖啡香。
林晚（自言自语）：今天的客人又会是那只橘猫吗？
旁白：这家开在巷口的咖啡店，每天都有一位特殊的客人。
林晚（笑着）：来啦，小橘，今天的司康给你留了一块。

第 2 场 咖啡店门口 黄昏 外
橘猫踩着晚霞跳上窗台，书店老板陈默拎着一袋猫粮走来。
陈默：林小姐，小橘这几天胖了不少，得控制口粮了。
林晚（接过猫粮）：谢谢陈叔，它可是我们店的招牌店员。

第 2 集 一起开的书架

第 1 场 旧书店 日 内
阳光洒在木质书架上，林晚帮忙整理新到的一批旧书。
陈默：这批旧书里有不少绝版 cookbook，你可以挑两本。
林晚（惊喜）：真的吗？我一直想学做苹果派。
旁白：书页翻动的声音里，两个小店主有了新的约定。

第 2 场 咖啡店 日 内
林晚端出试做的苹果派，橘猫趴在窗台上打着盹。
林晚（期待）：陈叔，尝尝我的手艺，提提意见。
陈默（点头）：外酥里嫩，明天我带红茶来配。
林晚把一块苹果派放在小碟子里，热气慢慢散开。
"""


# ---- 工具与夹具 ----


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """每个测试前后丢弃引擎连接池，规避 asyncpg 连接绑定过期事件循环。"""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """提供基于 ASGI 传输的 httpx 异步客户端（完整中间件/异常处理栈）。"""
    _ensure_routers_mounted()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _auth(user_id: int) -> dict[str, str]:
    """构造指定用户的 Bearer 认证头。

    Args:
        user_id: 用户 ID。

    Returns:
        Authorization 请求头字典。
    """
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def _ensure_user(phone: str, nickname: str) -> int:
    """确保测试用户与积分账户存在（幂等），返回用户 ID。

    Args:
        phone: 用户手机号。
        nickname: 用户昵称。

    Returns:
        用户 ID。
    """
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
                CreditAccount(owner_type="user", user_id=user.id, balance_credits=1_000_000)
            )
        await session.commit()
        return int(user.id)


async def _create_project(client: httpx.AsyncClient, user_id: int) -> int:
    """以指定用户创建一个测试项目并返回项目 ID。

    Args:
        client: httpx 异步客户端。
        user_id: 用户 ID。

    Returns:
        项目 ID。
    """
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects",
        json={"title": f"剧本域测试项目-{int(time.time())}", "synopsis": "上传解析链路测试"},
        headers=_auth(user_id),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    return int(body["data"]["id"])


async def _upload_script(
    client: httpx.AsyncClient, user_id: int, project_id: int
) -> int:
    """上传样例剧本并返回 document_id。

    Args:
        client: httpx 异步客户端。
        user_id: 上传者用户 ID。
        project_id: 目标项目 ID。

    Returns:
        文档 ID。
    """
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{project_id}/scripts/upload",
        files={"file": ("sample_script.txt", SAMPLE_SCRIPT.encode("utf-8"), "text/plain")},
        headers=_auth(user_id),
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body
    assert body["data"]["status"] == "pending"
    return int(body["data"]["document_id"])


async def _wait_parsed(
    client: httpx.AsyncClient, user_id: int, document_id: int
) -> dict[str, Any]:
    """轮询文档解析状态直至 parsed/failed 或超时。

    Args:
        client: httpx 异步客户端。
        user_id: 用户 ID。
        document_id: 文档 ID。

    Returns:
        最后一次文档状态 data。
    """
    deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await client.get(
            f"{settings.API_V1_PREFIX}/documents/{document_id}", headers=_auth(user_id)
        )
        body = resp.json()
        assert resp.status_code == 200, body
        assert body["code"] == 0, body
        last = body["data"]
        if last["parse_status"] in ("parsed", "failed"):
            return last
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return last


async def _get_current_tree(client: httpx.AsyncClient, user_id: int, project_id: int) -> dict[str, Any]:
    """获取项目当前剧本树（断言成功）。

    Args:
        client: httpx 异步客户端。
        user_id: 用户 ID。
        project_id: 项目 ID。

    Returns:
        剧本树 data。
    """
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/projects/{project_id}/scripts/current",
        headers=_auth(user_id),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    return body["data"]


async def _ensure_context(client: httpx.AsyncClient) -> dict[str, Any]:
    """确保「用户 A + 项目 + 上传解析完成」上下文就绪（幂等，模块级复用）。

    首次调用执行完整上传 + 轮询解析链路；后续用例直接复用缓存，
    避免重复等待真实 LLM 解析。

    Args:
        client: httpx 异步客户端。

    Returns:
        上下文字典（user_a_id/user_b_id/project_id/document_id/script_id/
        version_no/tree）。

    Raises:
        AssertionError: 解析失败或超时。
    """
    if _STATE.get("script_id"):
        return _STATE
    user_a_id = await _ensure_user(PHONE_A, "剧本域测试A")
    user_b_id = await _ensure_user(PHONE_B, "剧本域测试B")
    project_id = await _create_project(client, user_a_id)
    document_id = await _upload_script(client, user_a_id, project_id)
    doc = await _wait_parsed(client, user_a_id, document_id)
    assert doc.get("parse_status") == "parsed", f"解析未成功: {doc}"
    tree = await _get_current_tree(client, user_a_id, project_id)
    _STATE.update(
        {
            "user_a_id": user_a_id,
            "user_b_id": user_b_id,
            "project_id": project_id,
            "document_id": document_id,
            "script_id": int(tree["script"]["id"]),
            "version_no": int(tree["script"]["version_no"]),
            "tree": tree,
            "overall_confidence": doc.get("overall_confidence"),
        }
    )
    return _STATE


# ---- 用例 1：上传解析链路 ----


@pytest.mark.llm_live
async def test_01_upload_and_parse_flow(client: httpx.AsyncClient) -> None:
    """上传 txt → 轮询 parsed → 断言树/行类型/置信项齐全。"""
    ctx = await _ensure_context(client)

    # 解析状态与整体置信度
    assert ctx["overall_confidence"] is not None, "应存在整体置信度"
    assert float(ctx["overall_confidence"]) > 0.5, f"置信度过低: {ctx['overall_confidence']}"

    # 字段级置信项存在
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/documents/{ctx['document_id']}/confidence-items",
        headers=_auth(ctx["user_a_id"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["total"] > 0, "应存在字段级置信度项"
    first_item = body["data"]["items"][0]
    assert first_item["target_ref"], first_item
    assert 0 <= first_item["confidence"] <= 1, first_item

    # 树断言：episodes≥2，行类型齐全
    tree = ctx["tree"]
    assert len(tree["episodes"]) >= 2, f"应解析出至少 2 集，实际 {len(tree['episodes'])}"
    line_types: set[str] = set()
    for ep in tree["episodes"]:
        assert ep["scenes"], f"第 {ep['episode_no']} 集应有场次"
        for scene in ep["scenes"]:
            assert scene["lines"], f"场 {scene['scene_no']} 应有行"
            for line in scene["lines"]:
                line_types.add(line["line_type"])
    assert {"dialogue", "narration", "action"} <= line_types, f"行类型缺失: {line_types}"


# ---- 用例 2：current 树结构 ----


async def test_02_get_current_tree(client: httpx.AsyncClient) -> None:
    """GET current 返回 script/episodes/scenes/lines 完整层级字段。"""
    ctx = await _ensure_context(client)
    tree = await _get_current_tree(client, ctx["user_a_id"], ctx["project_id"])

    script = tree["script"]
    for key in ("id", "version_no", "status", "title", "total_episodes"):
        assert key in script, script
    assert script["status"] in ("needs_manual_fix", "pending_confirm", "draft"), script

    first_ep = tree["episodes"][0]
    for key in ("episode_no", "title", "target_duration_seconds", "scenes"):
        assert key in first_ep, first_ep
    first_scene = first_ep["scenes"][0]
    for key in ("scene_no", "location", "time_of_day", "int_ext", "lines"):
        assert key in first_scene, first_scene
    first_line = first_scene["lines"][0]
    for key in ("id", "line_type", "content", "order_index"):
        assert key in first_line, first_line

    # script-outline 只读大纲（每集时长与钩子）
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/projects/{ctx['project_id']}/script-outline",
        headers=_auth(ctx["user_a_id"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    outline_ep = body["data"]["episodes"][0]
    assert "target_duration_seconds" in outline_ep
    assert "hook" in outline_ep
    assert "scenes" in outline_ep


# ---- 用例 3：PATCH 行 → 版本+1 + 操作日志 ----


async def test_03_patch_line_version(client: httpx.AsyncClient) -> None:
    """编辑台词行：version+1、行内容更新、operation_logs 落 diff。"""
    ctx = await _ensure_context(client)
    old_version = int(ctx["tree"]["script"]["version_no"])

    # 取第一个 dialogue 行
    target: dict[str, Any] | None = None
    for ep in ctx["tree"]["episodes"]:
        for scene in ep["scenes"]:
            for line in scene["lines"]:
                if line["line_type"] == "dialogue":
                    target = line
                    break
    assert target is not None, "样例剧本应含对白行"

    new_content = str(target["content"]) + "（改动：语气更坚决）"
    resp = await client.patch(
        f"{settings.API_V1_PREFIX}/script-lines/{target['id']}",
        json={"content": new_content},
        headers=_auth(ctx["user_a_id"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["line"]["content"] == new_content, data
    assert data["new_version"] == old_version + 1, data

    # operation_logs 落 diff 记录
    async with async_session_factory() as session:
        logs = (
            await session.execute(
                select(OperationLog).where(
                    OperationLog.entity_type == "script_line",
                    OperationLog.entity_id == int(target["id"]),
                )
            )
        ).scalars().all()
        assert logs, "应存在操作日志"
        diff = logs[-1].diff or {}
        assert "content" in diff.get("changes", {}), diff

    # 刷新缓存树（行内容/版本已变化，供后续用例使用）
    ctx["tree"] = await _get_current_tree(client, ctx["user_a_id"], ctx["project_id"])
    ctx["version_no"] = int(ctx["tree"]["script"]["version_no"])


# ---- 用例 4：AI 局部改写 ----


@pytest.mark.llm_live
async def test_04_rewrite(client: httpx.AsyncClient) -> None:
    """rewrite：changed_lines 非空且产生新版本。"""
    ctx = await _ensure_context(client)
    old_version = int(ctx["tree"]["script"]["version_no"])

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{ctx['project_id']}/scripts/rewrite",
        json={
            "episode_no": 1,
            "scene_no": 1,
            "feedback": "开场节奏拖沓，第一场要更有悬念感，结尾留一个钩子",
        },
        headers=_auth(ctx["user_a_id"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["changed_lines"], "changed_lines 不应为空"
    assert len(data["changed_lines"]) > 0
    assert data["new_version"] > old_version, data

    ctx["tree"] = await _get_current_tree(client, ctx["user_a_id"], ctx["project_id"])
    ctx["version_no"] = int(ctx["tree"]["script"]["version_no"])


# ---- 用例 5：版本 diff ----


async def test_05_diff(client: httpx.AsyncClient) -> None:
    """diff?against=1：与初始版本比对 changed 非空。"""
    ctx = await _ensure_context(client)
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/scripts/{ctx['tree']['script']['id']}/diff",
        params={"against": 1},
        headers=_auth(ctx["user_a_id"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["against"] == 1
    assert data["changed"], f"与 v1 比对应有变更行: {data}"


# ---- 用例 6：合规送审 ----


@pytest.mark.llm_live
async def test_06_submit_review(client: httpx.AsyncClient) -> None:
    """submit-review（正常内容）→ passed + moderation_records 落库。"""
    ctx = await _ensure_context(client)
    script_id = int(ctx["tree"]["script"]["id"])

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/scripts/{script_id}/submit-review",
        headers=_auth(ctx["user_a_id"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["result"] == "passed", f"正常内容应审核通过: {data}"
    assert data["script_status"] == "pending_confirm", data

    async with async_session_factory() as session:
        records = (
            await session.execute(
                select(ModerationRecord).where(
                    ModerationRecord.project_id == ctx["project_id"]
                )
            )
        ).scalars().all()
        assert records, "moderation_records 应落库"
        assert any(r.verdict == "pass" for r in records), [r.verdict for r in records]


# ---- 用例 7：越权 + 清理 ----


async def test_07_upload_forbidden_and_cleanup(client: httpx.AsyncClient) -> None:
    """用户 B 上传到用户 A 项目 → HTTP 403 / code=20003；随后清理全部数据。"""
    ctx = await _ensure_context(client)
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{ctx['project_id']}/scripts/upload",
        files={"file": ("evil.txt", "第 1 集\n第 1 场 酒吧 夜 内\n甲：你好。\n".encode("utf-8"))},
        headers=_auth(ctx["user_b_id"]),
    )
    body = resp.json()
    assert resp.status_code == 403, body
    assert body["code"] == 20003, body

    await _cleanup_users([ctx["user_a_id"], ctx["user_b_id"]])
    _STATE.clear()


async def _cleanup_users(user_ids: list[int]) -> None:
    """按依赖顺序清理测试产生的全部数据（项目级联/日志/媒体/用户）。

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
            # 1) 无物理级联的项目级日志/媒体先删
            await session.execute(
                delete(ModerationRecord)
                .where(ModerationRecord.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(ComplianceFlag)
                .where(ComplianceFlag.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(OperationLog)
                .where(OperationLog.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(AuditLog)
                .where(AuditLog.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(MediaFile)
                .where(MediaFile.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(ProjectMember)
                .where(ProjectMember.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            # 2) 项目（级联 documents/scripts/episodes/scenes/script_lines）
            await session.execute(
                delete(Project)
                .where(Project.id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
        # 3) 兜底：用户相关无 project_id 的日志与用户本体
        await session.execute(
            delete(AuditLog)
            .where(AuditLog.actor_user_id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(OperationLog)
            .where(OperationLog.actor_user_id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(MediaFile)
            .where(MediaFile.owner_user_id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            delete(User)
            .where(User.id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
