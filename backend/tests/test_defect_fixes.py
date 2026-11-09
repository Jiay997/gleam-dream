"""缺陷回归测试（第二轮测试报告 D13/D14/D15/D16）。

覆盖（对齐《GleamDream_全面模拟测试报告_第二轮_20261015》缺陷清单）：
- **D13**：归档项目新建导出 → 40004（归档只读守卫，ACL 层统一拦截）；
- **D14**：成员授权撤销端点 `DELETE /projects/{id}/members/{uid}`——授予后
  撤销、撤销后访问失效（20003）、重复撤销 10002、Owner 不可移除 20003、
  view 成员不可撤销他人 20003；
- **D15**：归档项目发起分镜生成 → 40004（不再"受理成功但静默丢弃"）；
  paused 项目发起分镜生成 → 40001（服务层前置状态校验兜底）；
- **D16**：归档恢复 `POST /projects/{id}/unarchive`——Owner 恢复到归档前
  状态、archived_at 清空、重复恢复 40001、非 Owner 恢复 20003；
- **守卫不误伤**：归档项目 view 读取放行（GET 详情/导出列表 200）。

说明：D12（前端 401 静默续期）与 D17（Modal ARIA）为前端修复，由
`npm run build`（vue-tsc strict）验证，不在本后端测试范围。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_engine, async_session_factory
from app.core.security import create_access_token
from app.main import app
from app.models.billing import CreditAccount
from app.models.iam import AuditLog, OperationLog, ProjectMember, Team, TeamMember, User
from app.models.project import Project, ProjectSnapshot

pytestmark = pytest.mark.asyncio

#: 测试手机号段（与既有套件隔离：101/102 已被 test_project 占用）
PHONE_A = "13900000105"
PHONE_B = "13900000106"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """每个测试前后丢弃引擎连接池，规避 asyncpg 连接绑定过期事件循环。"""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


async def _ensure_user(phone: str, nickname: str) -> User:
    """确保测试用户与积分账户存在（幂等）。"""
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
    """按依赖顺序清理测试产生的全部数据（项目/团队/日志/用户）。"""
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
        for model in (ProjectSnapshot, ProjectMember, OperationLog, AuditLog):
            await session.execute(
                delete(model)
                .where(model.project_id.in_(project_ids))  # type: ignore[attr-defined]
                .execution_options(synchronize_session=False)
            )
        await session.execute(
            delete(Project)
            .where(Project.id.in_(project_ids))
            .execution_options(synchronize_session=False)
        )
        team_ids = (
            (
                await session.execute(
                    select(Team.id).where(Team.owner_user_id.in_(user_ids))
                )
            )
            .scalars()
            .all()
        )
        if team_ids:
            await session.execute(
                delete(TeamMember).where(TeamMember.team_id.in_(team_ids))
            )
            await session.execute(delete(Team).where(Team.id.in_(team_ids)))
        await session.execute(
            delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids))
        )
        await session.execute(
            delete(OperationLog).where(OperationLog.actor_user_id.in_(user_ids))
        )
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


@pytest_asyncio.fixture
async def users() -> AsyncIterator[tuple[User, User]]:
    """准备 A（Owner）/ B（成员）两个测试用户，用例结束统一清理。"""
    user_a = await _ensure_user(PHONE_A, "缺陷回归A")
    user_b = await _ensure_user(PHONE_B, "缺陷回归B")
    try:
        yield user_a, user_b
    finally:
        await _cleanup_users([user_a.id, user_b.id])


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """提供基于 ASGI 传输的 httpx 异步客户端（完整中间件/异常处理栈）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


def _auth(user: User) -> dict[str, str]:
    """构造指定用户的 Bearer 认证头。"""
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def _create_project(client: httpx.AsyncClient, user: User) -> dict[str, Any]:
    """以指定用户创建一个 draft 项目并返回详情 dict。"""
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects",
        json={"title": "缺陷回归项目", "target_episode_count": 3, "budget_credits": 10000},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


async def _transition(
    client: httpx.AsyncClient, user: User, project_id: int, target: str
) -> dict[str, Any]:
    """按状态机白名单流转项目并返回流转后详情。"""
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{project_id}/transition",
        json={"target_status": target, "reason": "缺陷回归"},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


# ---- D13：归档项目导出写守卫 ----


async def test_d13_archived_project_export_blocked(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """归档项目新建导出/批量导出 → 40004；view 读取导出列表仍放行。"""
    user_a, _ = users
    data = await _create_project(client, user_a)
    await _transition(client, user_a, data["id"], "archived")

    # 单集导出 → 40004（归档只读守卫，不再"code=0 且任务创建"）
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/exports",
        json={"resolution": "720p"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 40004, body

    # 批量导出 → 40004
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/exports/batch",
        json={"resolution": "720p"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 40004, body

    # 归档只读不误伤：view 读取导出列表放行
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/exports",
        headers=_auth(user_a),
    )
    assert resp.json()["code"] == 0


# ---- D15：归档/暂停项目分镜生成受理前置校验 ----


async def test_d15_archived_storyboard_generate_blocked(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """归档项目发起分镜生成 → 40004（不再 code=0 受理后静默丢弃）。"""
    user_a, _ = users
    data = await _create_project(client, user_a)
    await _transition(client, user_a, data["id"], "archived")

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/storyboards/generate",
        json={"episode_no": 2},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 40004, body


async def test_d15_paused_storyboard_generate_blocked(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """暂停项目发起分镜生成 → 40001（服务层前置校验，不再静默丢弃）。"""
    user_a, _ = users
    data = await _create_project(client, user_a)
    pause_resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/pause",
        headers=_auth(user_a),
    )
    assert pause_resp.json()["code"] == 0

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/storyboards/generate",
        json={"episode_no": 1},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 40001, body


# ---- D14：成员授权撤销 ----


async def test_d14_member_revoke_lifecycle(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """授予→访问→撤销→访问失效→重复撤销 10002；Owner 不可移除；view 不可撤销。"""
    user_a, user_b = users
    data = await _create_project(client, user_a)
    pid = data["id"]

    # Owner 授予 B view（B 随后可读）
    resp = await client.put(
        f"{settings.API_V1_PREFIX}/projects/{pid}/members/{user_b.id}",
        json={"permission": "view"},
        headers=_auth(user_a),
    )
    assert resp.json()["code"] == 0, resp.json()
    resp = await client.get(f"{settings.API_V1_PREFIX}/projects/{pid}", headers=_auth(user_b))
    assert resp.json()["code"] == 0

    # view 成员不可撤销他人（edit 档 ACL 拦截）→ 20003
    resp = await client.delete(
        f"{settings.API_V1_PREFIX}/projects/{pid}/members/{user_b.id}",
        headers=_auth(user_b),
    )
    assert resp.json()["code"] == 20003, resp.json()

    # Owner 撤销 B → 200（撤销成功）
    resp = await client.delete(
        f"{settings.API_V1_PREFIX}/projects/{pid}/members/{user_b.id}",
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["revoked"] is True, body
    assert body["data"]["removed_permission"] == "view"

    # 撤销后 B 访问失效 → 20003
    resp = await client.get(f"{settings.API_V1_PREFIX}/projects/{pid}", headers=_auth(user_b))
    assert resp.json()["code"] == 20003, resp.json()

    # 重复撤销 → 10002（非项目成员）
    resp = await client.delete(
        f"{settings.API_V1_PREFIX}/projects/{pid}/members/{user_b.id}",
        headers=_auth(user_a),
    )
    assert resp.json()["code"] == 10002, resp.json()

    # Owner 不可被移除 → 20003
    resp = await client.delete(
        f"{settings.API_V1_PREFIX}/projects/{pid}/members/{user_a.id}",
        headers=_auth(user_a),
    )
    assert resp.json()["code"] == 20003, resp.json()


# ---- D16：归档恢复 ----


async def test_d16_unarchive_restores_state(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """Owner 恢复归档项目到归档前状态；重复恢复 40001；非 Owner 恢复 20003。"""
    user_a, user_b = users
    data = await _create_project(client, user_a)  # draft

    # draft → archived（白名单直达）
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/archive",
        headers=_auth(user_a),
    )
    assert resp.json()["data"]["status"] == "archived"

    # 恢复（仅 Owner；归档态下 view 档 ACL 放行 Owner）
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/unarchive",
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["status"] == "draft", "应还原归档前状态"
    assert body["data"]["archived_at"] is None, "恢复后 archived_at 应清空"

    # 非归档状态再恢复 → 40001
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/unarchive",
        headers=_auth(user_a),
    )
    assert resp.json()["code"] == 40001, resp.json()

    # 非 Owner（未授权用户 B）恢复 → 20003
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/unarchive",
        headers=_auth(user_b),
    )
    assert resp.json()["code"] == 20003, resp.json()

    # 恢复后项目重新可写（归档守卫解除）：再归档 → 再恢复 幂等往返
    await _transition(client, user_a, data["id"], "archived")
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/unarchive",
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["status"] == "draft", body
