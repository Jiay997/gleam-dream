"""AI 创作助手回归测试（PRD V6.4 第 6 章）。

覆盖：
1. 上下文在无剧本/无分镜时降级不报错；
2. 会话 get-or-create、清空与重建（软删除 + 部分唯一索引）；
3. 消息发送：用户消息保留、助手回答、动作提议隐藏服务端参数；
4. 消息幂等（相同 client_message_id 不重复调用模型）；
5. 模型不可用 → degraded 消息（HTTP 200，用户问题不丢）；
6. 非法动作（渲染/导出等）被白名单过滤并写入风险提示；
7. 自检闭环：质检不通过时采用修正稿（最多 1 轮）；
8. 动作执行必须显式 confirm；过期动作拒绝；
9. 动作白名单：line_update 成功落库；
10. 预算护栏：预算耗尽只拦高成本动作；
11. 跨用户执行动作被拒（20003）；
12. 归档项目只读（写入 40004、读取放行）；
13. 限流 10004。

LLM 全部通过 monkeypatch 网关替换，不依赖外部网络。
运行：``cd backend && .venv\\Scripts\\python -m pytest tests/test_assistant.py -q``
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.core.db import async_engine, async_session_factory
from app.core.security import create_access_token
from app.infra.model_gateway.base import GenResult
from app.infra.model_gateway.gateway import model_gateway
from app.main import app
from app.models import (
    AssistantAction,
    AssistantConversation,
    AssistantMessage,
    CreditAccount,
    Episode,
    Project,
    ProjectMember,
    Scene,
    Script,
    ScriptLine,
    User,
)
from app.services import assistant_service

pytestmark = pytest.mark.asyncio

#: 测试手机号段（与其他套件隔离）
PHONE_A = "13900000201"
PHONE_B = "13900000202"

API = settings.API_V1_PREFIX


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """每个测试前后丢弃连接池，规避 asyncpg 连接绑定过期事件循环。"""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


async def _ensure_user(phone: str, nickname: str) -> User:
    """确保测试用户与积分账户存在（幂等）。

    Args:
        phone: 手机号。
        nickname: 昵称。

    Returns:
        用户 ORM 实例。
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
        return user


async def _cleanup_users(user_ids: list[int]) -> None:
    """按依赖顺序清理测试数据（项目级联助手表，再删用户）。

    Args:
        user_ids: 用户 ID 列表。

    Returns:
        无。
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
                delete(ProjectMember).where(ProjectMember.project_id.in_(project_ids))
            )
            await session.execute(
                delete(Project)
                .where(Project.id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
        await session.execute(delete(ProjectMember).where(ProjectMember.user_id.in_(user_ids)))
        await session.execute(delete(CreditAccount).where(CreditAccount.user_id.in_(user_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


@pytest_asyncio.fixture
async def users() -> AsyncIterator[tuple[User, User]]:
    """准备 Owner / 成员两个测试用户，用例结束统一清理。"""
    user_a = await _ensure_user(PHONE_A, "助手回归A")
    user_b = await _ensure_user(PHONE_B, "助手回归B")
    try:
        yield user_a, user_b
    finally:
        await _cleanup_users([user_a.id, user_b.id])


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """提供基于 ASGI 传输的 httpx 异步客户端。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


class FakeLLM:
    """可控的 LLM 网关替身：区分回答与自检，支持失败与修正稿。"""

    def __init__(self) -> None:
        """初始化默认回答契约与计数器。"""
        self.calls = 0
        self.fail = False
        self.answer_payload: dict[str, Any] = {
            "agent_key": "screenwriter",
            "answer": "建议调整第 1 集开场节奏，让冲突更早出现。",
            "suggestions": ["补充角色动机"],
            "proposed_actions": [
                {
                    "action_type": "script_rewrite",
                    "title": "改写开场",
                    "summary": "按建议改写第 1 集第 1 场",
                    "params": {
                        "episode_no": 1,
                        "scene_no": 1,
                        "feedback": "加强开场冲突与节奏",
                    },
                    "risk": None,
                }
            ],
            "risk_notes": [],
        }
        self.self_check_payload: dict[str, Any] = {
            "passed": True,
            "issues": [],
            "revised_answer": "",
        }

    async def generate(
        self,
        capability: str,
        task_type: str,
        request: Any,
        project_id: int | None = None,
        task_id: int | None = None,
        session_factory: Any = None,
    ) -> GenResult:
        """模拟网关生成：自检提示走质检契约，其余走回答契约。

        Args:
            capability: 能力（llm）。
            task_type: 任务类型（llm_text）。
            request: GenRequest。
            project_id: 项目 ID（可选）。
            task_id: 任务 ID（可选）。
            session_factory: 会话工厂（可选）。

        Returns:
            GenResult（成功时 text 为 JSON 字符串）。
        """
        self.calls += 1
        system = str(getattr(request, "system_prompt", "") or "")
        payload = self.self_check_payload if "独立质检员" in system else self.answer_payload
        if self.fail:
            return GenResult(ok=False, error="mock model unavailable")
        return GenResult(
            ok=True,
            text=json.dumps(payload, ensure_ascii=False),
            meta={"tokens_in": 12, "tokens_out": 24, "model_key": "fake-llm"},
        )


@pytest_asyncio.fixture
async def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    """把网关 generate 替换为可控替身。"""
    fake = FakeLLM()
    monkeypatch.setattr(model_gateway, "generate", fake.generate)
    return fake


def _auth(user: User) -> dict[str, str]:
    """构造 Bearer 认证头。"""
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _create_project(
    client: httpx.AsyncClient,
    user: User,
    *,
    title: str = "助手回归项目",
    budget: int = 0,
) -> dict[str, Any]:
    """创建 draft 项目并返回详情。"""
    resp = await client.post(
        f"{API}/projects",
        json={"title": title, "target_episode_count": 3, "budget_credits": budget},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


async def _seed_script(project_id: int) -> int:
    """直接落库一套最小剧本（1 集 / 1 场 / 1 行），返回台词 ID。"""
    async with async_session_factory() as session:
        script = Script(
            project_id=project_id,
            version_no=1,
            source_type="generated",
            status="draft",
            title="测试剧本",
            logline="测试梗概",
            total_episodes=1,
            total_words=20,
            structure={"episodes": [{"episode_no": 1, "scenes": []}]},
        )
        session.add(script)
        await session.flush()
        episode = Episode(
            script_id=script.id,
            project_id=project_id,
            episode_no=1,
            title="第 1 集",
            status="confirmed",
            order_index=0,
        )
        session.add(episode)
        await session.flush()
        scene = Scene(
            episode_id=episode.id,
            project_id=project_id,
            scene_no=1,
            title="开场",
            location="咖啡厅",
            time_of_day="day",
            summary="男女主初遇",
            order_index=0,
        )
        session.add(scene)
        await session.flush()
        line = ScriptLine(
            scene_id=scene.id,
            project_id=project_id,
            line_no=1,
            line_type="dialogue",
            content="你来晚了。",
            speaker_name="林婉",
            emotion_tag="calm",
            order_index=0,
            est_ms=3000,
        )
        session.add(line)
        await session.commit()
        return int(line.id)


async def _send(
    client: httpx.AsyncClient,
    user: User,
    project_id: int,
    content: str = "帮我优化第 1 集开场",
    client_message_id: str = "client-msg-1",
) -> dict[str, Any]:
    """发送一条助手消息并返回响应包络。"""
    resp = await client.post(
        f"{API}/projects/{project_id}/assistant/messages",
        json={"content": content, "episode_no": 1, "client_message_id": client_message_id},
        headers=_auth(user),
    )
    return resp.json()


# ---- 1. 上下文降级 ----


async def test_context_degrades_without_script(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """无剧本/无分镜/无 Bible 时上下文接口仍成功，并返回场景清单。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    resp = await client.get(
        f"{API}/projects/{project['id']}/assistant/context",
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0, body
    data = body["data"]
    assert data["context"]["script"] is None
    assert data["context"]["episodes"] == []
    assert data["context"]["budget"]["budget_credits"] == 0
    assert len(data["scenarios"]) == 10
    assert data["context"]["context_version"]


# ---- 2. 会话 get-or-create 与清空 ----


async def test_conversation_get_create_and_clear(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """同一项目/用户复用会话；清空后重建新会话。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    first = (
        await client.get(
            f"{API}/projects/{project['id']}/assistant/conversation",
            headers=_auth(user_a),
        )
    ).json()
    assert first["code"] == 0
    conversation_id = first["data"]["conversation"]["id"]

    again = (
        await client.get(
            f"{API}/projects/{project['id']}/assistant/conversation",
            headers=_auth(user_a),
        )
    ).json()
    assert again["data"]["conversation"]["id"] == conversation_id

    cleared = await client.delete(
        f"{API}/projects/{project['id']}/assistant/conversation",
        headers=_auth(user_a),
    )
    assert cleared.json()["data"]["deleted"] is True

    recreated = (
        await client.get(
            f"{API}/projects/{project['id']}/assistant/conversation",
            headers=_auth(user_a),
        )
    ).json()
    assert recreated["data"]["conversation"]["id"] != conversation_id


# ---- 3. 发送消息与动作快照 ----


async def test_send_message_with_action(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """发送消息返回助手回答与动作提议，动作不泄露服务端原始参数。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    await _seed_script(project["id"])

    body = await _send(client, user_a, project["id"])
    assert body["code"] == 0, body
    data = body["data"]
    assert data["message"]["role"] == "assistant"
    assert data["message"]["agent_key"] == "screenwriter"
    assert len(data["actions"]) == 1
    action = data["actions"][0]
    assert action["action_type"] == "script_rewrite"
    assert action["status"] == "pending"
    assert "params" not in action
    assert action["detail"]
    assert fake_llm.calls >= 2  # 回答 + 自检


# ---- 4. 消息幂等 ----


async def test_message_idempotency(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """相同 client_message_id 重放不重复调用模型、不重复落消息。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    await _seed_script(project["id"])

    first = await _send(client, user_a, project["id"], client_message_id="same-1")
    calls_after_first = fake_llm.calls
    second = await _send(client, user_a, project["id"], client_message_id="same-1")

    assert first["code"] == 0 and second["code"] == 0
    assert second["data"]["message"]["id"] == first["data"]["message"]["id"]
    assert fake_llm.calls == calls_after_first

    detail = (
        await client.get(
            f"{API}/projects/{project['id']}/assistant/conversation",
            headers=_auth(user_a),
        )
    ).json()["data"]
    assert len([item for item in detail["messages"] if item["role"] == "user"]) == 1


# ---- 5. 模型不可用降级 ----


async def test_llm_failure_degrades(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """模型不可用返回 degraded 消息，用户问题仍保留。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    fake_llm.fail = True

    body = await _send(client, user_a, project["id"])
    assert body["code"] == 0, body
    assert body["data"]["degraded"] is True
    assert body["data"]["message"]["status"] == "degraded"
    assert body["data"]["actions"] == []

    detail = (
        await client.get(
            f"{API}/projects/{project['id']}/assistant/conversation",
            headers=_auth(user_a),
        )
    ).json()["data"]
    roles = [item["role"] for item in detail["messages"]]
    assert "user" in roles and "assistant" in roles


# ---- 6. 非法动作过滤 ----


async def test_invalid_action_is_dropped(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """渲染/导出等越权动作被白名单过滤，并写入风险提示。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    fake_llm.answer_payload = {
        "agent_key": "director",
        "answer": "可以先生成渲染任务。",
        "suggestions": [],
        "proposed_actions": [
            {
                "action_type": "render_video",
                "title": "直接渲染",
                "summary": "绕过确认",
                "params": {"episode_no": 1},
            }
        ],
        "risk_notes": [],
    }
    body = await _send(client, user_a, project["id"])
    assert body["code"] == 0
    assert body["data"]["actions"] == []
    issues = (body["data"]["message"]["self_check"] or {}).get("issues") or []
    risks = (body["data"]["message"]["self_check"] or {}).get("risk_notes") or []
    assert any("已忽略" in str(item) for item in list(issues) + list(risks))


# ---- 7. 自检闭环 ----


async def test_self_check_corrects_answer(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """质检不通过时采用修正稿，并标记 corrected / rounds。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    await _seed_script(project["id"])
    fake_llm.self_check_payload = {
        "passed": False,
        "issues": ["建议不够具体"],
        "revised_answer": "修正后的具体建议：开场 10 秒内让女主摔碎杯子。",
    }
    body = await _send(client, user_a, project["id"])
    message = body["data"]["message"]
    assert "修正后的具体建议" in message["content"]
    assert message["self_check"]["corrected"] is True
    assert message["self_check"]["rounds"] == 1


# ---- 8. 确认与过期 ----


async def test_execute_requires_confirm_and_rejects_expired(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """动作执行必须 confirm=true；过期动作返回 10001。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    await _seed_script(project["id"])
    action_id = (await _send(client, user_a, project["id"]))["data"]["actions"][0]["id"]

    missing = await client.post(
        f"{API}/projects/{project['id']}/assistant/actions/{action_id}/execute",
        json={},
        headers=_auth(user_a),
    )
    assert missing.json()["code"] == 10001

    async with async_session_factory() as session:
        await session.execute(
            update(AssistantAction)
            .where(AssistantAction.id == action_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()

    expired = await client.post(
        f"{API}/projects/{project['id']}/assistant/actions/{action_id}/execute",
        json={"confirm": True},
        headers=_auth(user_a),
    )
    payload = expired.json()
    assert payload["code"] == 10001
    assert payload.get("error_detail") == "action_expired"


# ---- 9. 动作执行（line_update 落库） ----


async def test_execute_line_update_succeeds(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """确认执行台词修改动作后，台词文本落库且动作状态为 succeeded。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    line_id = await _seed_script(project["id"])
    fake_llm.answer_payload = {
        "agent_key": "voice",
        "answer": "建议把台词改得更口语化。",
        "suggestions": [],
        "proposed_actions": [
            {
                "action_type": "line_update",
                "title": "润色台词",
                "summary": "把台词改得更自然",
                "params": {"line_id": line_id, "content": "你怎么才来呀。"},
            }
        ],
        "risk_notes": [],
    }
    send_body = await _send(client, user_a, project["id"], content="帮我润色第 1 句台词")
    action_id = send_body["data"]["actions"][0]["id"]

    executed = await client.post(
        f"{API}/projects/{project['id']}/assistant/actions/{action_id}/execute",
        json={"confirm": True},
        headers=_auth(user_a),
    )
    payload = executed.json()
    assert payload["code"] == 0, payload
    assert payload["data"]["action"]["status"] == "succeeded"
    assert payload["data"]["message"]["role"] == "system"

    async with async_session_factory() as session:
        line = await session.get(ScriptLine, line_id)
        assert line is not None and line.content == "你怎么才来呀。"


# ---- 10. 预算护栏 ----


async def test_budget_blocks_hard_cost_action(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """预算耗尽时高成本动作返回 30002，但对话本身不受影响。"""
    user_a, _ = users
    project = await _create_project(client, user_a, budget=100)
    fake_llm.answer_payload = {
        "agent_key": "screenwriter",
        "answer": "可以先生成剧本初稿。",
        "suggestions": [],
        "proposed_actions": [
            {
                "action_type": "script_generate",
                "title": "生成剧本",
                "summary": "按一句话创意生成",
                "params": {"idea": "都市悬疑短剧"},
            }
        ],
        "risk_notes": [],
    }
    send_body = await _send(client, user_a, project["id"])
    assert send_body["code"] == 0
    action_id = send_body["data"]["actions"][0]["id"]

    async with async_session_factory() as session:
        await session.execute(
            update(Project).where(Project.id == project["id"]).values(spent_credits=100)
        )
        await session.commit()

    executed = await client.post(
        f"{API}/projects/{project['id']}/assistant/actions/{action_id}/execute",
        json={"confirm": True},
        headers=_auth(user_a),
    )
    payload = executed.json()
    assert payload["code"] == 30002
    assert payload["data"]["exhausted"] is True


# ---- 11. 跨用户执行拒绝 ----


async def test_cross_user_cannot_execute_action(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
) -> None:
    """成员 B 即使有 edit 权限，也不能执行 A 会话中的动作。"""
    user_a, user_b = users
    project = await _create_project(client, user_a)
    await _seed_script(project["id"])
    action_id = (await _send(client, user_a, project["id"]))["data"]["actions"][0]["id"]

    async with async_session_factory() as session:
        session.add(
            ProjectMember(
                project_id=project["id"], user_id=user_b.id, permission="edit", granted_by=user_a.id
            )
        )
        await session.commit()

    executed = await client.post(
        f"{API}/projects/{project['id']}/assistant/actions/{action_id}/execute",
        json={"confirm": True},
        headers=_auth(user_b),
    )
    assert executed.json()["code"] == 20003


# ---- 12. 归档只读 ----


async def test_archived_project_read_only(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """归档项目：读取上下文放行，发送消息 40004。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    transition = await client.post(
        f"{API}/projects/{project['id']}/transition",
        json={"target_status": "archived", "reason": "助手回归"},
        headers=_auth(user_a),
    )
    assert transition.json()["code"] == 0

    read = await client.get(
        f"{API}/projects/{project['id']}/assistant/context",
        headers=_auth(user_a),
    )
    assert read.json()["code"] == 0

    write = await _send(client, user_a, project["id"])
    assert write["code"] == 40004


# ---- 13. 限流 ----


async def test_rate_limit(
    users: tuple[User, User],
    client: httpx.AsyncClient,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """触发滑动窗口上限后返回 10004。"""
    user_a, _ = users
    project = await _create_project(client, user_a)
    monkeypatch.setattr(assistant_service, "RATE_LIMIT_MAX", 2)

    first = await _send(client, user_a, project["id"], client_message_id="rate-1")
    second = await _send(client, user_a, project["id"], client_message_id="rate-2")
    third = await _send(client, user_a, project["id"], client_message_id="rate-3")
    assert first["code"] == 0
    assert second["code"] == 0
    assert third["code"] == 10004
