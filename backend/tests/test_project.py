"""项目域 API 与 SSE 事件流测试模块（Task 4）。

覆盖范围：
1. 创建项目 -> status=draft -> project_members 含 owner 记录；
2. 非法流转 draft->rendering -> BizError 40001（data 含 from/to/allowed）；
3. 合法状态机全链流转（draft -> ... -> completed，途经 asset_review）；
4. paused -> resume 状态还原（config._paused_before）；
5. 快照创建 -> 列表 -> 回滚 -> 生成 rollback 快照 -> 幂等重复回滚；
6. 越权访问 -> 20003 且 audit_logs 留痕（action=acl_denied）；
7. SSE 事件流：asyncio 任务发布 3 个事件，httpx 流式读取收到 3 条 JSON。

测试账号：手机号 13900000101（A）/ 13900000102（B），用例结束统一清理。
运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_project.py -x -q
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_engine, async_session_factory
from app.core.security import create_access_token
from app.infra.event_bus import event_bus
from app.main import app
from app.models.billing import CreditAccount
from app.models.iam import AuditLog, OperationLog, ProjectMember, Team, TeamMember, User
from app.models.project import Project, ProjectSnapshot

pytestmark = pytest.mark.asyncio

#: 测试用户 A 手机号（项目 Owner）
PHONE_A = "13900000101"
#: 测试用户 B 手机号（越权测试用）
PHONE_B = "13900000102"

#: 合法状态机全链（draft 出发；asset_preparing -> storyboarding 须途经 asset_review）
FULL_CHAIN: list[str] = [
    "scripting",
    "script_review",
    "asset_preparing",
    "asset_review",
    "storyboarding",
    "storyboard_review",
    "rendering",
    "post_producing",
    "final_review",
    "completed",
]

# ---- 路由手动挂载（主控统一聚合 app/api/__init__.py 前的测试用装配） ----

_MOUNTED = False


def _ensure_routers_mounted() -> None:
    """将项目/团队路由手动挂载到 main.app（幂等，已挂载则跳过）。

    说明：主控后续在 app/api/__init__.py 统一聚合后，本函数的路径检查
    会自动跳过，避免重复注册。
    """
    global _MOUNTED
    if _MOUNTED:
        return
    from app.api.projects import router as projects_router
    from app.api.teams import router as teams_router

    paths = {getattr(route, "path", "") for route in app.router.routes}
    if f"{settings.API_V1_PREFIX}/projects" not in paths:
        app.include_router(projects_router, prefix=settings.API_V1_PREFIX)
    if f"{settings.API_V1_PREFIX}/teams" not in paths:
        app.include_router(teams_router, prefix=settings.API_V1_PREFIX)
    _MOUNTED = True


# ---- 工具与夹具 ----


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """每个测试前后丢弃引擎连接池，规避 asyncpg 连接绑定过期事件循环。"""
    await async_engine.dispose()
    yield
    await async_engine.dispose()


async def _ensure_user(phone: str, nickname: str) -> User:
    """确保测试用户与积分账户存在（幂等）。

    Args:
        phone: 用户手机号。
        nickname: 用户昵称。

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
                CreditAccount(owner_type="user", user_id=user.id, balance_credits=1_000_000)
            )
        await session.commit()
        return user


async def _cleanup_users(user_ids: list[int]) -> None:
    """按依赖顺序清理测试产生的全部数据（项目/团队/日志/用户）。

    Args:
        user_ids: 待清理的用户 ID 列表。
    """
    if not user_ids:
        return
    async with async_session_factory() as session:
        # 1) 项目及其子表
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
                delete(ProjectSnapshot)
                .where(ProjectSnapshot.project_id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(ProjectMember)
                .where(ProjectMember.project_id.in_(project_ids))
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
                delete(Project)
                .where(Project.id.in_(project_ids))
                .execution_options(synchronize_session=False)
            )
        # 2) 团队及其子表
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
                delete(TeamMember)
                .where(TeamMember.team_id.in_(team_ids))
                .execution_options(synchronize_session=False)
            )
            await session.execute(
                delete(Team)
                .where(Team.id.in_(team_ids))
                .execution_options(synchronize_session=False)
            )
        # 3) 团队操作产生的无 project_id 审计/操作日志
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
        # 4) 用户（级联清理 credit_accounts / user_identities 等）
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
    user_a = await _ensure_user(PHONE_A, "测试用户A")
    user_b = await _ensure_user(PHONE_B, "测试用户B")
    try:
        yield user_a, user_b
    finally:
        await _cleanup_users([user_a.id, user_b.id])


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


async def _create_project(client: httpx.AsyncClient, user: User, **overrides: Any) -> dict[str, Any]:
    """以指定用户创建一个测试项目并返回 data 字典。

    Args:
        client: httpx 异步客户端。
        user: 创建者用户。
        **overrides: 覆盖默认请求体的字段。

    Returns:
        响应 data（项目详情字典）。
    """
    payload: dict[str, Any] = {"title": "测试短剧项目", "synopsis": "v1"}
    payload.update(overrides)
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects", json=payload, headers=_auth(user)
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    return body["data"]


async def _transition(
    client: httpx.AsyncClient, user: User, project_id: int, target: str
) -> dict[str, Any]:
    """执行一次状态流转并断言业务成功。

    Args:
        client: httpx 异步客户端。
        user: 操作者用户。
        project_id: 项目 ID。
        target: 目标状态。

    Returns:
        响应 data（流转后的项目详情）。
    """
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{project_id}/transition",
        json={"target_status": target, "reason": "test"},
        headers=_auth(user),
    )
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


# ---- 用例 1：创建项目 ----


async def test_create_project(users: tuple[User, User], client: httpx.AsyncClient) -> None:
    """创建项目：status=draft，project_members 含 owner（export）记录。"""
    user_a, _ = users
    data = await _create_project(client, user_a)

    assert data["status"] == "draft"
    assert data["owner_user_id"] == user_a.id
    assert data["title"] == "测试短剧项目"
    assert data["lock_version"] == 0

    async with async_session_factory() as session:
        member = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == data["id"],
                    ProjectMember.user_id == user_a.id,
                )
            )
        ).scalar_one_or_none()
        assert member is not None, "创建项目后必须存在 owner 授权记录"
        assert member.permission == "export"


# ---- 用例 2：非法流转 ----


async def test_invalid_transition(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """draft 直接流转到 rendering 应返回 40001，data 携带 allowed 白名单。"""
    user_a, _ = users
    data = await _create_project(client, user_a)

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/transition",
        json={"target_status": "rendering", "reason": "跳步流转"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert resp.status_code == 200  # 业务失败不映射 5xx
    assert body["code"] == 40001
    assert body["data"]["from"] == "draft"
    assert body["data"]["to"] == "rendering"
    assert body["data"]["allowed"] == ["archived", "scripting"]


# ---- 用例 3：状态机全链 ----


async def test_full_status_chain(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """按白名单依次流转 draft -> ... -> completed（途经 asset_review）。"""
    user_a, _ = users
    data = await _create_project(client, user_a)

    for target in FULL_CHAIN:
        updated = await _transition(client, user_a, data["id"], target)
        assert updated["status"] == target

    # completed 可归档
    archived = await _transition(client, user_a, data["id"], "archived")
    assert archived["status"] == "archived"
    # 终态无出口：已归档项目再做流转 -> 40004（优先归档语义）
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/transition",
        json={"target_status": "scripting"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 40004


# ---- 用例 4：暂停/恢复 ----


async def test_pause_and_resume(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """pause 后状态为 paused 且记录原状态，resume 后还原并清除记录。"""
    user_a, _ = users
    data = await _create_project(client, user_a)
    await _transition(client, user_a, data["id"], "scripting")

    # 暂停
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/pause", headers=_auth(user_a)
    )
    body = resp.json()
    assert body["code"] == 0, body
    paused = body["data"]
    assert paused["status"] == "paused"
    assert paused["config"]["_paused_before"] == "scripting"

    # 恢复
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/resume", headers=_auth(user_a)
    )
    body = resp.json()
    assert body["code"] == 0, body
    resumed = body["data"]
    assert resumed["status"] == "scripting"
    assert "_paused_before" not in (resumed["config"] or {})

    # 非暂停状态下 resume 应 40001
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}/resume", headers=_auth(user_a)
    )
    assert resp.json()["code"] == 40001


# ---- 用例 5：快照与回滚 ----


async def test_snapshot_rollback(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """快照创建 -> 列表 -> 修改 -> 回滚恢复 -> rollback 快照 -> 幂等。"""
    user_a, _ = users
    data = await _create_project(client, user_a)  # synopsis="v1"
    pid = data["id"]

    # 1) 手动快照
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{pid}/snapshots",
        json={"name": "v1 检查点"},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0, body
    snap = body["data"]
    assert snap["label"] == "v1 检查点"
    assert snap["trigger"] == "manual"
    assert snap["state"]["status"] == "draft"
    assert snap["state"]["synopsis"] == "v1"

    # 2) 快照列表（倒序，摘要）
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/projects/{pid}/snapshots", headers=_auth(user_a)
    )
    listing = resp.json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["status"] == "draft"

    # 3) 修改项目 synopsis -> 回滚
    resp = await client.patch(
        f"{settings.API_V1_PREFIX}/projects/{pid}",
        json={"synopsis": "v2", "lock_version": data["lock_version"]},
        headers=_auth(user_a),
    )
    assert resp.json()["data"]["synopsis"] == "v2"

    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{pid}/snapshots/{snap['id']}/rollback",
        json={},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["rolled_back"] is True
    assert body["data"]["project"]["synopsis"] == "v1"

    # 4) 回滚动作生成一条 rollback 快照（列表倒序第一条）
    resp = await client.get(
        f"{settings.API_V1_PREFIX}/projects/{pid}/snapshots", headers=_auth(user_a)
    )
    items = resp.json()["data"]["items"]
    assert len(items) == 2
    assert items[0]["trigger"] == "rollback"
    assert items[0]["label"] is None

    # 5) 重复回滚到相同状态 -> 幂等提示
    resp = await client.post(
        f"{settings.API_V1_PREFIX}/projects/{pid}/snapshots/{snap['id']}/rollback",
        json={},
        headers=_auth(user_a),
    )
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["rolled_back"] is False
    assert "无需回滚" in body["data"]["message"]


# ---- 用例 6：越权访问 ----


async def test_acl_denied_with_audit(
    users: tuple[User, User], client: httpx.AsyncClient
) -> None:
    """用户 B 访问用户 A 的项目 -> 403/20003，且 audit_logs 留痕。"""
    user_a, user_b = users
    data = await _create_project(client, user_a)

    resp = await client.get(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}", headers=_auth(user_b)
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == 20003

    # B 携带 edit 意图写操作同样被拒（并再次留痕）
    resp = await client.patch(
        f"{settings.API_V1_PREFIX}/projects/{data['id']}",
        json={"title": "越权改名"},
        headers=_auth(user_b),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 20003

    async with async_session_factory() as session:
        logs = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "acl_denied",
                        AuditLog.actor_user_id == user_b.id,
                        AuditLog.project_id == data["id"],
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) >= 2, "两次越权都应写审计日志"
        needs = {log.detail.get("need") for log in logs if log.detail}
        assert {"view", "edit"} <= needs


# ---- 用例 7：SSE 事件流 ----


async def _asgi_sse_lines(
    asgi_app: Any, path: str, headers: dict[str, str]
) -> AsyncIterator[str]:
    """手写 ASGI 流式客户端：真实流式消费 SSE 响应（逐行产出）。

    httpx 的 ASGITransport 为全缓冲实现（等待 app 调用结束才返回
    response），无法消费无限长的 SSE 流，故此处直接以 ASGI 协议调用
    应用（走完整中间件栈），收到 body 分块即实时产出。

    Args:
        asgi_app: ASGI 应用（main.app）。
        path: 请求路径（含 API 前缀）。
        headers: 请求头（Authorization / Last-Event-ID）。

    Yields:
        响应体行（去除行尾换行符）。

    Raises:
        AssertionError: 响应状态码或 Content-Type 不符。
        TimeoutError: 响应起始或分块等待超时。
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 123),
        "root_path": "",
    }
    chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
    started = asyncio.Event()
    meta: dict[str, Any] = {}
    request_forwarded = False

    async def receive() -> dict[str, Any]:
        """ASGI receive：先回空请求体，此后保持连接等待（模拟不断开）。

        Returns:
            ASGI receive 消息。
        """
        nonlocal request_forwarded
        if not request_forwarded:
            request_forwarded = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # 永久挂起，直到外层取消任务
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        """ASGI send：捕获响应起始元数据，body 分块实时入队。

        Args:
            message: ASGI send 消息。
        """
        if message["type"] == "http.response.start":
            meta["status"] = message["status"]
            meta["headers"] = {
                (k.decode() if isinstance(k, bytes) else k).lower(): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in message.get("headers", [])
            }
            started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await chunks.put(body)
            if not message.get("more_body", False):
                await chunks.put(None)

    task = asyncio.create_task(asgi_app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=10.0)
        assert meta["status"] == 200, meta
        assert meta["headers"].get("content-type", "").startswith("text/event-stream")
        buf = b""
        while True:
            chunk = await asyncio.wait_for(chunks.get(), timeout=15.0)
            if chunk is None:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8").rstrip("\r")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_sse_events(users: tuple[User, User], client: httpx.AsyncClient) -> None:
    """asyncio 任务发布 3 个事件，SSE 流式读取应恰好收到 3 条 JSON。"""
    user_a, _ = users
    data = await _create_project(client, user_a)
    pid = data["id"]
    headers = _auth(user_a)

    # probe 事件提供基线序号：Last-Event-ID=baseline 时，probe 本身被过滤，
    # 之后发布的 3 条事件无论走回放还是实时推送都恰好 3 条（不丢不重）
    baseline = await event_bus.publish(pid, "probe", {"n": -1})

    seqs: list[int] = []

    async def publish_three() -> None:
        """后台协程：延迟 0.3s 后连续发布 3 个测试事件。"""
        await asyncio.sleep(0.3)
        for i in range(3):
            seqs.append(await event_bus.publish(pid, "test_event", {"n": i}))

    task = asyncio.create_task(publish_three())

    async def read_three() -> list[str]:
        """流式读取 SSE 直到收满 3 条 data 帧。

        Returns:
            data 负载字符串列表（JSON 文本）。
        """
        got: list[str] = []
        stream_headers = {**headers, "Last-Event-ID": str(baseline)}
        async for line in _asgi_sse_lines(
            app, f"{settings.API_V1_PREFIX}/projects/{pid}/events", stream_headers
        ):
            if line.startswith("data:"):
                got.append(line[len("data:") :].strip())
                if len(got) == 3:
                    break
        return got

    try:
        frames = await asyncio.wait_for(read_three(), timeout=20.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(frames) == 3
    parsed = [json.loads(frame) for frame in frames]
    assert [item["n"] for item in parsed] == [0, 1, 2]
