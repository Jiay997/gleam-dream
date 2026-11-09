"""编排域 API 与编剧子图端到端测试模块（Task 6a，真实 LLM）。

覆盖范围（E2E 主链路 + 并发/越权）：
1. 注册测试用户 → 建项目（budget 10000）→ invoke script-generate →
   轮询直至 approval_requests 出现 pending（真实 LLM，超时 180s）；
2. 断言：scripts/episodes/scenes/script_lines/project_files 已落库、
   write_todos 可见（agent_runs.input/output）、冻结产生；
3. 重复 invoke → 40001（已有执行中的流程）；
4. 越权：用户 B decide 用户 A 的审批单 → 20003；
5. reject（反馈"主角动机不足，第 1 集结尾要有反转钩子"）→ 再次轮询
   → 新的 pending 审批单（语义：原单置 rejected，局部改写后创建新单）；
6. approve → 项目状态=asset_preparing、script.status=locked、
   冻结已 settle、流水正确（settle + 差额 unfreeze）；
7. 单元级：reject 局部改写范围解析。

测试账号：手机号 13900000201（A）/ 13900000202（B），用例结束统一清理
（含 credit_transactions/freezes、项目级联、checkpoint 数据）。
运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_orchestration.py -x -q
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_engine, async_session_factory
from app.core.security import create_access_token
from app.graphs.checkpointer import delete_project_checkpoints
from app.graphs.nodes.deterministic import (
    affected_episodes_from_feedback,
    finalize_approve,
)
from app.main import app
from app.models.billing import CreditAccount, CreditFreeze, CreditTransaction
from app.models.collab import AgentRun, ApprovalRequest
from app.models.iam import User
from app.models.project import Project, ProjectFile
from app.models.quality import ModerationRecord
from app.models.script import Episode, Scene, Script, ScriptLine
from app.models.render import AiCallLog

pytestmark = pytest.mark.asyncio

#: 测试用户 A 手机号（项目 Owner）
PHONE_A = "13900000201"
#: 测试用户 B 手机号（越权测试用）
PHONE_B = "13900000202"

# ---- 路由手动挂载（主控统一聚合 app/api/__init__.py 前的测试用装配） ----

_MOUNTED = False


def _ensure_routers_mounted() -> None:
    """将编排路由手动挂载到 main.app（幂等，已挂载则跳过）。"""
    global _MOUNTED
    if _MOUNTED:
        return
    from app.api.orchestration import router as orchestration_router

    paths = {getattr(route, "path", "") for route in app.router.routes}
    if f"{settings.API_V1_PREFIX}/orchestration/approvals" not in paths:
        app.include_router(orchestration_router, prefix=settings.API_V1_PREFIX)
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
        balance: 初始余额（积分）。

    Returns:
        用户 ORM 实例（已提交）。
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
                CreditAccount(owner_type="user", user_id=user.id, balance_credits=balance)
            )
        else:
            account.balance_credits = balance
        await session.commit()
        return user


async def _cleanup_users(user_ids: list[int]) -> None:
    """按依赖顺序清理测试产生的全部数据（计费/项目/用户/检查点）。

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
        account_ids = (
            (
                await session.execute(
                    select(CreditAccount.id).where(CreditAccount.user_id.in_(user_ids))
                )
            )
            .scalars()
            .all()
        )
        # 1) checkpoint 数据（thread_id = str(project_id)）
        for pid in project_ids:
            await delete_project_checkpoints(int(pid))
        # 2) 计费流水/冻结（RESTRICT 阻塞删账户，必须先删）
        if account_ids:
            await session.execute(
                delete(CreditTransaction)
                .where(CreditTransaction.account_id.in_(account_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(CreditFreeze)
                .where(CreditFreeze.account_id.in_(account_ids))
                .execution_options(synchronize_session=False)
            )
        # 3) 项目（级联 scripts/episodes/scenes/lines/files/idempotency/
        #    approvals/agent_runs/moderation_records）
        if project_ids:
            await session.execute(
                delete(AiCallLog)
                .where(AiCallLog.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(CreditTransaction)
                .where(CreditTransaction.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(CreditFreeze)
                .where(CreditFreeze.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(Project)
                .where(Project.id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
        # 4) 用户（级联 credit_accounts）
        await session.execute(
            delete(User)
            .where(User.id.in_(user_ids))
            .execution_options(synchronize_session=False)
        )
        await session.commit()


@pytest_asyncio.fixture
async def users() -> AsyncIterator[tuple[User, User]]:
    """准备 A/B 两个测试用户，用例结束后统一清理其全部数据。

    Yields:
        (用户 A（Owner）, 用户 B（越权测试用）) 元组。
    """
    user_a = await _ensure_user(PHONE_A, "编排测试A")
    user_b = await _ensure_user(PHONE_B, "编排测试B")
    try:
        yield user_a, user_b
    finally:
        await _cleanup_users([int(user_a.id), int(user_b.id)])


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """提供基于 ASGI 传输的 httpx 异步客户端（完整中间件/异常处理栈）。"""
    _ensure_routers_mounted()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _auth(user: User) -> dict[str, str]:
    """构造指定用户的 Bearer 认证头。

    Args:
        user: 用户 ORM 实例。

    Returns:
        Authorization 请求头字典。
    """
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _create_project(client: httpx.AsyncClient, user: User) -> dict[str, Any]:
    """以指定用户创建一个编排测试项目（1 集控制 LLM 时长）。

    Args:
        client: httpx 异步客户端。
        user: 创建者用户。

    Returns:
        响应 data（项目详情字典）。
    """
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects",
        json={
            "title": "编排测试短剧",
            "synopsis": "实习医生在深夜急诊室遇到一位声称能预知死亡的女孩",
            "genre": "urban_suspense",
            "target_episode_count": 1,
            "budget_credits": 10000,
        },
        headers=_auth(user),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    return body["data"]


async def _wait_for(
    predicate_and_fetch, *, timeout: float = 180.0, interval: float = 3.0
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


async def _fetch_pending_approval(project_id: int) -> ApprovalRequest | None:
    """查询项目最新 pending 审批单（含惰性过期过滤）。

    Args:
        project_id: 项目 ID。

    Returns:
        pending 审批单；无则 None。
    """
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.project_id == project_id,
                    ApprovalRequest.status == "pending",
                )
                .order_by(ApprovalRequest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _fetch_project(project_id: int) -> Project:
    """按 ID 查询项目（断言用）。

    Args:
        project_id: 项目 ID。

    Returns:
        项目 ORM 实例。
    """
    async with async_session_factory() as session:
        return (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()


# ---- 单元级：局部改写范围解析 ----


async def test_affected_episodes_parsing() -> None:
    """验证 reject 反馈的受影响集号解析（AC-F2-04-1 局部改写）。"""
    assert affected_episodes_from_feedback("主角动机不足，第 1 集结尾要有反转钩子", 3) == {1}
    assert affected_episodes_from_feedback("第 2 集和第 3 集节奏太慢", 5) == {2, 3}
    # 无集号引用 → 全集改写
    assert affected_episodes_from_feedback("整体太拖沓", 3) == {1, 2, 3}
    assert affected_episodes_from_feedback(None, 2) == {1, 2}
    # 越界集号被忽略后回退全集
    assert affected_episodes_from_feedback("第 9 集不行", 2) == {1, 2}


# ---- E2E 主链路 ----


async def test_script_generation_full_pipeline(users: tuple[User, User], client: httpx.AsyncClient) -> None:
    """端到端：invoke → 审批挂起 → reject 局部改写 → approve → 结算锁定。"""
    user_a, user_b = users

    # ---- 1. 建项目 + 发起生成 ----
    project = await _create_project(client, user_a)
    project_id = int(project["id"])
    # 注：idea 须避开真实 LLM 内容审核（compliance）的 block 词面
    # （如死亡/预知死亡等），否则图被 80002 终止、审批单不会产生。
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/orchestration/projects/{project_id}/script-generate",
        json={"idea": "实习医生在深夜急诊室遇到一位走失的女孩，用专业与温情帮她找到家人"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body
    assert body["data"]["status"] == "accepted", body

    # ---- 2. 轮询直至 pending 审批单出现（真实 LLM；LLM 偶发 JSON
    #         截断会触发自修重试链，故超时上限放宽到 300s） ----
    approval = await _wait_for(lambda: _fetch_pending_approval(project_id), timeout=300)
    assert approval is not None
    approval_id = int(approval.id)

    # ---- 3. 断言落库产物 ----
    async with async_session_factory() as session:
        script = (
            await session.execute(
                select(Script).where(Script.project_id == project_id).order_by(Script.version_no.desc())
            )
        ).scalars().first()
        assert script is not None, "剧本未落库"
        assert script.status == "pending_confirm"
        assert script.total_episodes == 1
        assert script.structure is not None and script.structure.get("episodes")

        episodes = (
            await session.execute(select(Episode).where(Episode.script_id == script.id))
        ).scalars().all()
        assert len(episodes) == 1, "分集未落库"
        assert episodes[0].target_duration_seconds is not None
        assert 60 <= episodes[0].target_duration_seconds <= 180
        assert episodes[0].hook_text, "结尾钩子缺失"

        scenes = (
            await session.execute(select(Scene).where(Scene.project_id == project_id))
        ).scalars().all()
        assert len(scenes) >= 1, "场次未落库"

        lines = (
            await session.execute(select(ScriptLine).where(ScriptLine.project_id == project_id))
        ).scalars().all()
        assert len(lines) >= 3, "剧本行未落库"

        files = (
            await session.execute(select(ProjectFile).where(ProjectFile.project_id == project_id))
        ).scalars().all()
        paths = {f.path for f in files}
        assert f"/project/{project_id}/script/ep01.md" in paths, f"虚拟工作区缺 ep01.md: {paths}"
        assert f"/project/{project_id}/script/script.json" in paths

        moderation = (
            await session.execute(
                select(ModerationRecord).where(ModerationRecord.project_id == project_id)
            )
        ).scalars().all()
        assert moderation, "审核记录未写入 moderation_records"
        assert any(m.provider == "llm_compliance" for m in moderation)

        # write_todos 可见（AgentRun 出参或幂等产物）
        runs = (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.project_id == project_id, AgentRun.node_name == "generate_outline"
                )
            )
        ).scalars().all()
        assert runs, "generate_outline 未写 agent_runs"

        # 冻结产生（预估 = 1 集 × 50 积分）
        freeze = (
            await session.execute(
                select(CreditFreeze).where(
                    CreditFreeze.project_id == project_id, CreditFreeze.status == "frozen"
                )
            )
        ).scalars().first()
        assert freeze is not None, "冻结未产生"
        freeze_id = int(freeze.id)
        assert int(freeze.frozen_credits) == 50

    # 项目进入 script_review（等待人工确认）
    proj = await _fetch_project(project_id)
    assert proj.status == "script_review", f"项目状态={proj.status}"

    # ---- 4. 幂等/并发：重复 invoke → 40001 ----
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/orchestration/projects/{project_id}/script-generate",
        json={},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == 40001, f"期望 40001，实际 {body}"

    # ---- 5. 越权：用户 B 决策用户 A 的审批单 → 20003 ----
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/orchestration/approvals/{approval_id}/decide",
        json={"action": "reject", "feedback": "hack"},
        headers=_auth(user_b),
    )
    body = resp.json()
    assert resp.status_code == 403
    assert body["code"] == 20003, f"期望 20003，实际 {body}"

    # ---- 6. reject（带反馈）→ 轮询新的 pending 审批单 ----
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/orchestration/approvals/{approval_id}/decide",
        json={
            "action": "reject",
            "feedback": "主角动机不足，第 1 集结尾要有反转钩子",
        },
        headers=_auth(user_a),
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body

    first_approval_id = approval_id
    new_approval = await _wait_for(
        lambda: _next_pending_after(project_id, first_approval_id), timeout=300
    )
    assert new_approval is not None, "reject 后未产生新的 pending 审批单"
    assert int(new_approval.id) != first_approval_id

    async with async_session_factory() as session:
        first_row = await session.get(ApprovalRequest, first_approval_id)
        assert first_row is not None and first_row.status == "rejected"
        assert first_row.decision == "reject"
        assert first_row.feedback and "反转钩子" in first_row.feedback
    # reject 回写后项目回到 scripting 重新生成
    proj = await _fetch_project(project_id)
    assert proj.status in ("scripting", "script_review")

    # ---- 7. approve → asset_preparing + locked + settled ----
    approval_id = int(new_approval.id)
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/orchestration/approvals/{approval_id}/decide",
        json={"action": "approve"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body

    async def _project_ready() -> Project | None:
        """轮询条件：项目已推进到 asset_preparing。"""
        p = await _fetch_project(project_id)
        return p if p.status == "asset_preparing" else None

    proj = await _wait_for(_project_ready, timeout=60, interval=2)
    assert proj.status == "asset_preparing"
    assert proj.spent_credits > 0, "项目消耗积分未更新"

    async with async_session_factory() as session:
        # 剧本锁定
        script = (
            await session.execute(
                select(Script).where(Script.project_id == project_id).order_by(Script.version_no.desc())
            )
        ).scalars().first()
        assert script is not None
        assert script.status == "locked", f"剧本状态={script.status}"
        assert script.locked_at is not None
        proj_row = await session.get(Project, project_id)
        assert proj_row is not None and proj_row.current_script_id == script.id

        # 冻结已结算
        freeze = await session.get(CreditFreeze, freeze_id)
        assert freeze is not None
        assert freeze.status == "settled", f"冻结状态={freeze.status}"
        assert (freeze.settled_credits or 0) > 0

        # 流水正确：freeze（负）→ settle（负）→ unfreeze（差额，若 >0）
        txs = (
            await session.execute(
                select(CreditTransaction)
                .where(CreditTransaction.related_freeze_id == freeze_id)
                .order_by(CreditTransaction.id)
            )
        ).scalars().all()
        types = [t.transaction_type for t in txs]
        assert "freeze" in types, f"缺 freeze 流水: {types}"
        assert "settle" in types, f"缺 settle 流水: {types}"
        settle_tx = next(t for t in txs if t.transaction_type == "settle")
        assert settle_tx.amount_credits == -int(freeze.settled_credits)

        # 重复决策同一审批单 → 40001（幂等防护）
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/orchestration/approvals/{approval_id}/decide",
        json={"action": "approve"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 40001, f"重复决策应 40001，实际 {body}"


async def _next_pending_after(project_id: int, before_id: int) -> ApprovalRequest | None:
    """查询比指定审批单更新的 pending 审批单（reject 语义断言用）。

    Args:
        project_id: 项目 ID。
        before_id: 已决策的审批单 ID。

    Returns:
        新的 pending 审批单；无则 None。
    """
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.project_id == project_id,
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.id > before_id,
                )
                .order_by(ApprovalRequest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


# ---- 单元级：审批单重指后 approve 锁定正确版本（遗留 D 回归） ----


async def _seed_repointed_approval(
    client: httpx.AsyncClient, user: User
) -> tuple[int, int, int]:
    """构造「审批单重指」现场（复刻 1421 事故的数据形态）。

    动作序列：建项目 → 项目置 script_review → 插入旧稿（v1）与重生成
    新稿（v2，均 pending_confirm）→ 创建 target=旧稿 的 pending 审批单
    → 模拟 _ensure_script_approval 复用重指（target 改为新稿 + 刷新
    requested_at/expires_at + feedback_scope 置 manual_review）。

    Args:
        client: httpx 异步客户端。
        user: 项目 Owner。

    Returns:
        (project_id, 旧稿 ID, 新稿 ID) 元组。

    Raises:
        AssertionError: 项目创建失败。
    """
    project = await _create_project(client, user)
    project_id = int(project["id"])
    async with async_session_factory() as session:
        proj = await session.get(Project, project_id)
        assert proj is not None
        old_script = Script(
            project_id=project_id,
            version_no=1,
            source_type="generated",
            status="pending_confirm",
            title="旧稿",
            logline="首次生成版本",
            total_episodes=1,
        )
        session.add(old_script)
        await session.flush()
        new_script = Script(
            project_id=project_id,
            version_no=2,
            parent_script_id=old_script.id,
            source_type="generated",
            status="pending_confirm",
            title="新稿",
            logline="重新生成版本",
            total_episodes=1,
        )
        session.add(new_script)
        await session.flush()
        # persist_script_version 语义：首次生成完成时指向旧稿
        proj.current_script_id = old_script.id
        proj.status = "script_review"
        now = datetime.now(timezone.utc)
        approval = ApprovalRequest(
            project_id=project_id,
            approval_type="script",
            target_type="script",
            target_id=old_script.id,
            status="pending",
            requested_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(approval)
        await session.flush()
        # _ensure_script_approval 复用重指语义：指向最新版本并刷新有效期
        approval.target_id = new_script.id
        approval.requested_at = now
        approval.expires_at = now + timedelta(days=7)
        approval.feedback_scope = {"source": "manual_review"}
        await session.commit()
    return project_id, int(old_script.id), int(new_script.id)


async def test_finalize_approve_locks_decision_target_script(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """遗留 D 回归：审批单重指后 approve，必须锁定决策载荷指向的新稿并更新 current_script_id。

    场景：checkpoint state.script_id 为旧稿，decision.target_script_id 为
    审批单重指后的新稿。期望：新稿 locked、旧稿保持 pending_confirm、
    projects.current_script_id 指向新稿、项目推进到 asset_preparing。

    Args:
        users: A/B 测试用户夹具。
        client: httpx 异步客户端。

    Returns:
        无。

    Raises:
        AssertionError: 断言失败（锁定目标或项目指针不正确）。
    """
    user_a, _ = users
    project_id, old_id, new_id = await _seed_repointed_approval(client, user_a)

    # 模拟图 resume 后 finalize_approve 收到的 state（旧 script_id + 重指 target）
    await finalize_approve(
        {
            "project_id": project_id,
            "script_id": old_id,
            "decision": {"action": "approve", "target_script_id": new_id},
        }
    )

    async with async_session_factory() as session:
        proj = await session.get(Project, project_id)
        assert proj is not None
        assert proj.current_script_id == new_id, (
            f"current_script_id 应指向新稿 {new_id}，实际 {proj.current_script_id}"
        )
        assert proj.status == "asset_preparing", f"项目状态={proj.status}"
        new_script = await session.get(Script, new_id)
        assert new_script is not None
        assert new_script.status == "locked", f"新稿状态={new_script.status}"
        assert new_script.locked_at is not None
        old_script = await session.get(Script, old_id)
        assert old_script is not None
        assert old_script.status == "pending_confirm", f"旧稿不应被锁定，实际={old_script.status}"


async def test_finalize_approve_without_target_keeps_state_script(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """兼容性回归：决策载荷缺 target_script_id 时维持原行为（锁定 state.script_id）。

    Args:
        users: A/B 测试用户夹具。
        client: httpx 异步客户端。

    Returns:
        无。

    Raises:
        AssertionError: 断言失败（原行为被破坏）。
    """
    user_a, _ = users
    project_id, old_id, _new_id = await _seed_repointed_approval(client, user_a)

    await finalize_approve({"project_id": project_id, "script_id": old_id})

    async with async_session_factory() as session:
        proj = await session.get(Project, project_id)
        assert proj is not None
        assert proj.current_script_id == old_id, (
            f"无 target 时应锁 state.script_id={old_id}，实际 {proj.current_script_id}"
        )
        old_script = await session.get(Script, old_id)
        assert old_script is not None and old_script.status == "locked"
