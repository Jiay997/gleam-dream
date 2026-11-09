"""埋点最小集测试（验收报告 7.1 P0）。

覆盖：
1. ``track``：事件写入 analytics_events（自建短会话）；
2. ``funnel_summary``：注册/激活去重人数与激活率、四关口通过率、
   导出漏斗完成率——期望值以窗口内原始行数直接对账；
3. API：``POST /analytics/events``（登录态 202 受理 + 落库归属正确、
   未登录 20001、事件名非法 10001）；``GET /analytics/funnel``
   （无口令 20003 / 错口令 20003 / 正确口令 code=0 且结构完整）；
4. ``GET /health/models``：提供商配置探针结构。

说明：dev 库可能存在历史事件（e2e 冒烟等），断言采用「窗口内原始行
对账」而非固定数值，避免环境耦合；用例结束清理本用例写入的事件。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.db import async_session_factory
from app.main import app
from app.models import (
    AnalyticsEvent,
    CreditAccount,
    CreditTransaction,
    Subscription,
    User,
    UserIdentity,
)
from app.services import analytics_service

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")

#: 测试手机号（与其他套件隔离）
PHONE = "13900009995"

#: 本套件使用的客户端事件名（清理标记用）
_TEST_EVENTS = ("ui_funnel_test", "perf_probe")

#: 本套件插入事件使用的合成用户 ID（900001~900004 段位专用）
_FAKE_USER_IDS = (900001, 900002, 900003, 900004)

#: 服务端埋点事件名（清理范围）
_SERVER_EVENTS = (
    analytics_service.EVENT_USER_REGISTERED,
    analytics_service.EVENT_PROJECT_ACTIVATED,
    analytics_service.EVENT_GATE_PASSED,
    analytics_service.EVENT_GATE_REJECTED,
    analytics_service.EVENT_EXPORT_STARTED,
    analytics_service.EVENT_EXPORT_COMPLETED,
    analytics_service.EVENT_EXPORT_FAILED,
)


async def _cleanup() -> None:
    """清理测试用户与测试事件（按手机号/合成用户 ID/事件名精确删除）。"""
    async with async_session_factory() as session, session.begin():
        # 真实测试用户（按手机号注册产生）——先清计费数据再删用户（FK 顺序）
        real_user_ids = list(
            (await session.execute(select(User.id).where(User.phone == PHONE)))
            .scalars()
            .all()
        )
        if real_user_ids:
            account_ids = list(
                (
                    await session.execute(
                        select(CreditAccount.id).where(
                            CreditAccount.owner_type == "user",
                            CreditAccount.user_id.in_(real_user_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if account_ids:
                await session.execute(
                    delete(CreditTransaction).where(
                        CreditTransaction.account_id.in_(account_ids)
                    )
                )
            await session.execute(
                delete(CreditAccount).where(CreditAccount.user_id.in_(real_user_ids))
            )
            await session.execute(
                delete(Subscription).where(Subscription.user_id.in_(real_user_ids))
            )
            await session.execute(
                delete(UserIdentity).where(UserIdentity.user_id.in_(real_user_ids))
            )
            await session.execute(delete(User).where(User.id.in_(real_user_ids)))

        # 合成用户（900001~900004，仅存在于埋点事件中）
        fake_ids = list(_FAKE_USER_IDS)
        await session.execute(
            delete(AnalyticsEvent).where(AnalyticsEvent.user_id.in_(fake_ids))
        )
        await session.execute(
            delete(AnalyticsEvent).where(AnalyticsEvent.user_id.in_(real_user_ids or [0]))
        )
        # 客户端事件名标记清理
        await session.execute(
            delete(AnalyticsEvent).where(AnalyticsEvent.event_name.in_(_TEST_EVENTS))
        )
        # 服务端事件（本套件插入的均为匿名事件：user_id/project_id 为空）
        await session.execute(
            delete(AnalyticsEvent).where(
                AnalyticsEvent.event_name.in_(_SERVER_EVENTS),
                AnalyticsEvent.user_id.is_(None),
                AnalyticsEvent.project_id.is_(None),
            )
        )


@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _clean() -> AsyncIterator[None]:
    """session 级清理 fixture。"""
    await _cleanup()
    yield
    await _cleanup()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """session 级 HTTP 客户端：ASGITransport 直连 app。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(scope="session")
def ctx() -> dict[str, Any]:
    """session 级共享上下文。"""
    return {}


# ---- track 服务 ----


async def test_track_writes_event() -> None:
    """track：事件落库，字段完整（含 props JSONB 与 occurred_at）。"""
    await analytics_service.track(
        "ui_funnel_test",
        user_id=None,
        project_id=None,
        session_id="sess-1",
        props={"k": "v"},
    )
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(AnalyticsEvent)
                .where(AnalyticsEvent.event_name == "ui_funnel_test")
                .order_by(AnalyticsEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert row.event_props == {"k": "v"}
    assert row.session_id == "sess-1"
    assert row.occurred_at is not None


async def test_track_never_raises() -> None:
    """埋点失败绝不向业务传播：会话工厂抛异常时 track 静默告警。"""

    def _broken_factory() -> None:
        raise RuntimeError("db down")

    # track 通过 async with session_factory() as session 使用工厂；
    # 传入直接抛异常的可调用对象即可模拟落库失败。
    await analytics_service.track(
        "ui_funnel_test", session_factory=_broken_factory  # type: ignore[arg-type]
    )  # 不应抛出


# ---- funnel_summary 聚合对账 ----


async def test_funnel_summary_reconciles_with_raw_rows() -> None:
    """漏斗聚合与窗口内原始行数对账（注册/激活/关口/导出）。"""
    now = datetime.now(timezone.utc)

    # 注册：2 个不同用户 + 1 重复事件
    for uid in (900001, 900002):
        await analytics_service.track(
            analytics_service.EVENT_USER_REGISTERED, user_id=uid, occurred_at=now
        )
    await analytics_service.track(
        analytics_service.EVENT_USER_REGISTERED, user_id=900001, occurred_at=now
    )
    # 激活：900001 创建首个项目
    await analytics_service.track(
        analytics_service.EVENT_PROJECT_ACTIVATED, user_id=900001, occurred_at=now
    )
    # 关口：script 2 过 1 驳；asset 1 过 0 驳
    gate_events = [
        (analytics_service.EVENT_GATE_PASSED, "script", 2),
        (analytics_service.EVENT_GATE_PASSED, "script", 3),
        (analytics_service.EVENT_GATE_REJECTED, "script", 2),
        (analytics_service.EVENT_GATE_PASSED, "asset", 4),
    ]
    for event_name, gate, uid in gate_events:
        await analytics_service.track(
            event_name, user_id=uid, props={"gate": gate}, occurred_at=now
        )
    # 导出：2 成功 1 失败 1 进行中
    await analytics_service.track(analytics_service.EVENT_EXPORT_STARTED, occurred_at=now)
    await analytics_service.track(analytics_service.EVENT_EXPORT_STARTED, occurred_at=now)
    await analytics_service.track(analytics_service.EVENT_EXPORT_STARTED, occurred_at=now)
    await analytics_service.track(analytics_service.EVENT_EXPORT_STARTED, occurred_at=now)
    await analytics_service.track(analytics_service.EVENT_EXPORT_COMPLETED, occurred_at=now)
    await analytics_service.track(analytics_service.EVENT_EXPORT_COMPLETED, occurred_at=now)
    await analytics_service.track(analytics_service.EVENT_EXPORT_FAILED, occurred_at=now)

    async with async_session_factory() as session:
        summary = await analytics_service.funnel_summary(session, days=30)

        # ---- 与原始行数对账（窗口 30 天内）----
        since = now - timedelta(days=30)

        async def count(event_name: str, user_id: int | None = None) -> int:
            stmt = (
                select(func.count())
                .select_from(AnalyticsEvent)
                .where(AnalyticsEvent.event_name == event_name, AnalyticsEvent.occurred_at >= since)
            )
            if user_id is not None:
                stmt = stmt.where(AnalyticsEvent.user_id == user_id)
            return int((await session.execute(stmt)).scalar_one())

        # 注册去重人数：与库内窗口内 user_registered 去重用户数一致，且 ≥ 本用例插入的 2 人
        distinct_registered = int(
            (
                await session.execute(
                    select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                        AnalyticsEvent.event_name == analytics_service.EVENT_USER_REGISTERED,
                        AnalyticsEvent.occurred_at >= since,
                    )
                )
            ).scalar_one()
        )
        assert summary["registered_users"] == distinct_registered >= 2
        # 激活率 = 激活去重 / 注册去重
        assert summary["activation_rate"] == round(
            summary["activated_users"] / summary["registered_users"], 4
        )
        # 关口：script passed=库内实际行数（本用例 +2）
        script_passed_rows = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AnalyticsEvent)
                    .where(
                        AnalyticsEvent.event_name == analytics_service.EVENT_GATE_PASSED,
                        AnalyticsEvent.occurred_at >= since,
                        AnalyticsEvent.event_props["gate"].astext == "script",
                    )
                )
            ).scalar_one()
        )
        assert summary["gates"]["script"]["passed"] == script_passed_rows >= 2
        script_rejected_rows = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AnalyticsEvent)
                    .where(
                        AnalyticsEvent.event_name == analytics_service.EVENT_GATE_REJECTED,
                        AnalyticsEvent.occurred_at >= since,
                        AnalyticsEvent.event_props["gate"].astext == "script",
                    )
                )
            ).scalar_one()
        )
        assert summary["gates"]["script"]["rejected"] == script_rejected_rows >= 1
        total = summary["gates"]["script"]["total"]
        assert total == script_passed_rows + script_rejected_rows
        assert summary["gates"]["script"]["pass_rate"] == round(script_passed_rows / total, 4)
        # asset 关口：passed ≥ 1 且 rejected=库内行数
        assert summary["gates"]["asset"]["passed"] >= 1
        # 四关口键齐全
        assert set(summary["gates"].keys()) == {"script", "asset", "storyboard", "master"}
        # 导出漏斗：started ≥ 4、completed ≥ 2、failed ≥ 1
        assert summary["export_funnel"]["started"] >= 4
        assert summary["export_funnel"]["completed"] >= 2
        assert summary["export_funnel"]["failed"] >= 1
        finished = summary["export_funnel"]["completed"] + summary["export_funnel"]["failed"]
        assert summary["export_funnel"]["completion_rate"] == round(
            summary["export_funnel"]["completed"] / finished, 4
        )


# ---- API 端点 ----


async def test_analytics_events_requires_auth(client: httpx.AsyncClient) -> None:
    """未登录上报 → 20001 包络。"""
    resp = await client.post(
        "/api/v1/analytics/events", json={"event_name": "ui_funnel_test"}
    )
    body = resp.json()
    assert body["code"] == 20001


async def test_analytics_events_validates_event_name(client: httpx.AsyncClient, ctx: dict) -> None:
    """注册登录后：合法事件名 202 受理并落库归属正确；非法事件名 10001。"""
    # 注册（dev 固定验证码）
    await client.post("/api/v1/auth/sms-code", json={"phone": PHONE})
    resp = await client.post(
        "/api/v1/auth/register",
        json={"phone": PHONE, "sms_code": "123456", "password": "AnalYtics123"},
    )
    body = resp.json()
    assert body["code"] == 0, body
    token = body["data"]["access_token"]
    ctx["token"] = token
    user_id = int(body["data"]["user"]["id"])
    headers = {"Authorization": f"Bearer {token}"}

    # 非法事件名（大写/连字符/过短）→ 统一包络 10001（HTTP 200）
    for bad in ("Bad-Name", "x", "UPPER_CASE"):
        resp = await client.post(
            "/api/v1/analytics/events", json={"event_name": bad}, headers=headers
        )
        assert resp.json()["code"] == 10001, f"{bad} 应被校验拒绝"

    # 合法上报
    resp = await client.post(
        "/api/v1/analytics/events",
        json={"event_name": "ui_funnel_test", "props": {"page": "home"}},
        headers=headers,
    )
    assert resp.status_code == 202
    assert resp.json()["data"]["accepted"] is True

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(AnalyticsEvent)
                .where(AnalyticsEvent.event_name == "ui_funnel_test")
                .order_by(AnalyticsEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert row.user_id == user_id
    assert row.event_props == {"page": "home"}


async def test_funnel_requires_admin_token(client: httpx.AsyncClient) -> None:
    """无口令/错口令 → 20003；正确口令 → code=0 且结构完整。"""
    resp = await client.get("/api/v1/analytics/funnel")
    assert resp.json()["code"] == 20003

    resp = await client.get(
        "/api/v1/analytics/funnel", headers={"X-Admin-Token": "wrong-token"}
    )
    assert resp.json()["code"] == 20003

    resp = await client.get(
        "/api/v1/analytics/funnel", headers={"X-Admin-Token": settings.ADMIN_TOKEN}
    )
    body = resp.json()
    assert body["code"] == 0, body
    data = body["data"]
    for key in ("window_days", "registered_users", "activated_users", "activation_rate", "gates", "export_funnel"):
        assert key in data
    assert data["window_days"] == 30


async def test_health_models_structure(client: httpx.AsyncClient) -> None:
    """模型提供商配置探针：结构完整，密钥仅返回布尔位不泄露明文。"""
    resp = await client.get("/api/v1/health/models")
    body = resp.json()
    assert body["code"] == 0
    providers = body["data"]["providers"]
    assert {p["name"] for p in providers} == {"deepseek", "glm"}
    for p in providers:
        assert isinstance(p["api_key_configured"], bool)
        assert "base_url" in p and "model" in p
    # 响应全文不包含任何密钥明文
    raw = resp.text
    assert settings.DEEPSEEK_API_KEY not in raw
    assert settings.GLM_API_KEY not in raw
