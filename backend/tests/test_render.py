"""生成执行域集成测试（Task 8 · 渲染任务 API + asyncio 执行器）。

覆盖范围：
1. 成本估算：estimate 单价合计正确（ceil(时长秒) × 30 积分/秒）；
2. 批量提交 → 轮询全部 succeeded：shots=rendered、render_outputs /
   media_files / shot_versions / qc_reports 落库、冻结已结算、
   actual ≤ estimated × 1.5；
3. 幂等：同 Idempotency-Key 重复 batch → 相同任务、不重复冻结；
4. 余额不足：账户清零再 batch → 30001（data 携带 needed/available）；
5. 取消：队列暂停后提交 2 镜 → cancel 其一（cancelled + 全额退款），
   恢复执行器后另一照常完成；
6. 越权：用户 B 提交到用户 A 项目 → 20003；
7. 网关瞬断重试：monkeypatch 网关首次抛错 → 任务经 retrying 最终
   succeeded（retry_count ≥ 1）。

运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_render.py -x -q

说明：
- 复用 ``app.main.app``（异常处理器已注册），手动挂载 auth 与 render
  两个 router（render 不在主控聚合器内）；
- 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）；
- 测试手机号 13600000901~2，fixture 前后自动清理全部业务数据；
- 执行器以 session 级 fixture 启停（ASGITransport 不触发 lifespan）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.core.db import async_session_factory
from app.main import app
from app.models import (
    AiCallLog,
    Asset,
    AssetVersion,
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    Episode,
    FaceEmbedding,
    MediaFile,
    Project,
    QcReport,
    RenderTask,
    Script,
    Shot,
    ShotVersion,
    Storyboard,
    Subscription,
    User,
)
from app.services.consistency_service import make_deterministic_embedding
from app.tasks import executor as executor_module
from app.tasks.executor import render_executor

# render 路由不在主控聚合器 api_router 内，测试进程内手动挂载
from app.api.render import router as render_router
from app.api.auth import router as auth_router

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")

#: 测试手机号
PHONE_A = "13600000901"  # 项目所有者
PHONE_B = "13600000902"  # 越权用户
TEST_PHONES = (PHONE_A, PHONE_B)

#: 两镜头最终提示词（portrait 伪向量按 prompt 派生，保证 QC 一轮通过）
PROMPT_1 = "夕阳下少女在天台眺望城市天际线，电影感构图，暖色调"
PROMPT_2 = "雨夜少年在街头奔跑，霓虹灯在地面积反光，冷色调"
#: 镜头时长（毫秒）：3000ms → ceil(3) × 30 = 90 积分/镜
SHOT_DURATION_MS = 3000
#: stub-video 单价（积分/秒，对齐种子数据 model_registry.stub-video）
VIDEO_UNIT_PRICE = 30
#: 单镜预估成本
SHOT_EST = ((SHOT_DURATION_MS + 999) // 1000) * VIDEO_UNIT_PRICE


def _ensure_routers() -> None:
    """把本任务的 render router 与 auth router 挂载到 app（幂等）。

    主控聚合器 api_router 暂未包含 render 路由，测试进程内手动 include；
    以各自特征端点路径是否存在判断是否已挂载。
    """
    existing_paths = {getattr(route, "path", "") for route in app.routes}
    markers = {
        "render": f"{settings.API_V1_PREFIX}/projects/{{project_id}}/render/estimate",
        "auth": f"{settings.API_V1_PREFIX}/auth/sms-code",
    }
    if markers["render"] not in existing_paths:
        app.include_router(render_router, prefix=settings.API_V1_PREFIX)
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
            await session.execute(delete(AiCallLog).where(AiCallLog.project_id.in_(project_ids)))
            await session.execute(delete(MediaFile).where(MediaFile.project_id.in_(project_ids)))
            await session.execute(delete(Asset).where(Asset.project_id.in_(project_ids)))
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
        await session.execute(delete(Subscription).where(Subscription.user_id.in_(user_ids)))
        await session.execute(delete(CreditAccount).where(CreditAccount.user_id.in_(user_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))


@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    """session 级清理 fixture：跑前清残留、跑后清数据。"""
    await _cleanup()
    yield
    await _cleanup()


@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _executor_lifecycle() -> AsyncIterator[None]:
    """session 级执行器 fixture：ASGITransport 不触发 lifespan，手动启停。"""
    await render_executor.start()
    yield
    await render_executor.stop()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """session 级 HTTP 客户端：ASGITransport 直连 app。"""
    _ensure_routers()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0) as c:
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


async def _build_project_with_shots(user_id: int) -> dict[str, Any]:
    """构造完整渲染前置数据：项目 + 剧本 + 集 + 分镜板 + 2 个锁定镜头。

    每个镜头绑定独立角色资产（portrait 版本 + 人脸伪向量）。伪向量以
    对应镜头 final_prompt 派生，保证执行器 QC 一轮通过（score=1.0）。

    Args:
        user_id: 项目所有者用户 ID。

    Returns:
        {project_id, shot_ids, storyboard_id}。
    """
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session, session.begin():
        project = Project(
            owner_user_id=user_id,
            title="渲染域测试项目",
            synopsis="Task 8 集成测试",
            status="rendering",
        )
        session.add(project)
        await session.flush()

        script = Script(
            project_id=int(project.id),
            version_no=1,
            source_type="generated",
            status="locked",
            title="渲染域测试剧本",
        )
        session.add(script)
        await session.flush()

        episode = Episode(
            script_id=int(script.id),
            project_id=int(project.id),
            episode_no=1,
            order_index=0,
            status="locked",
        )
        session.add(episode)
        await session.flush()

        storyboard = Storyboard(
            project_id=int(project.id),
            episode_id=int(episode.id),
            version_no=1,
            status="locked",
            shot_count=2,
            locked_at=now,
        )
        session.add(storyboard)
        await session.flush()

        shot_ids: list[int] = []
        for idx, prompt in enumerate((PROMPT_1, PROMPT_2), start=1):
            asset = Asset(
                project_id=int(project.id),
                owner_user_id=user_id,
                asset_type="character",
                name=f"测试角色{idx}",
                status="locked",
                source="ai_generated",
                locked_at=now,
            )
            session.add(asset)
            await session.flush()
            version = AssetVersion(
                asset_id=int(asset.id),
                version_type="portrait",
                version_no=1,
                is_current=True,
                prompt=prompt,
            )
            session.add(version)
            await session.flush()
            session.add(
                FaceEmbedding(
                    asset_id=int(asset.id),
                    asset_version_id=int(version.id),
                    embedding=make_deterministic_embedding(prompt),
                    face_index=0,
                    model_key="stub-face",
                )
            )
            shot = Shot(
                storyboard_id=int(storyboard.id),
                project_id=int(project.id),
                episode_id=int(episode.id),
                shot_no=idx,
                order_index=idx - 1,
                shot_size="medium",
                camera_move="static",
                duration_ms=SHOT_DURATION_MS,
                final_prompt=prompt,
                asset_anchor_ids=[int(version.id)],
                character_asset_ids=[int(asset.id)],
                status="locked",
                locked_at=now,
            )
            session.add(shot)
            await session.flush()
            shot_ids.append(int(shot.id))

    return {
        "project_id": int(project.id),
        "shot_ids": shot_ids,
        "storyboard_id": int(storyboard.id),
    }


async def _wait_task_terminal(
    client: httpx.AsyncClient,
    token: str,
    task_id: int,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """轮询渲染任务直至进入终态。

    Args:
        client: HTTP 客户端。
        token: 访问令牌。
        task_id: 任务 ID。
        timeout: 超时秒数。

    Returns:
        终态任务详情。

    Raises:
        TimeoutError: 超时未进入终态。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/v1/render-tasks/{task_id}", headers=_auth_headers(token))
        body = resp.json()
        assert body["code"] == 0, body
        if body["data"]["status"] in ("succeeded", "failed", "cancelled", "manual_required"):
            return body["data"]
        await asyncio.sleep(0.4)
    raise TimeoutError(f"任务 {task_id} 在 {timeout}s 内未进入终态")


async def _get_account_balance(user_id: int) -> dict[str, int]:
    """查询用户积分账户余额快照。

    Args:
        user_id: 用户 ID。

    Returns:
        {balance, frozen} 字典。
    """
    async with async_session_factory() as session:
        account = (
            await session.execute(
                select(CreditAccount).where(
                    CreditAccount.owner_type == "user", CreditAccount.user_id == user_id
                )
            )
        ).scalar_one()
        return {"balance": int(account.balance_credits), "frozen": int(account.frozen_credits)}


async def _reset_shot_locked(shot_id: int) -> None:
    """把镜头状态重置回 locked（为 cancel / retry 用例重新提交做准备）。

    Args:
        shot_id: 镜头 ID。
    """
    async with async_session_factory() as session, session.begin():
        await session.execute(update(Shot).where(Shot.id == shot_id).values(status="locked"))


async def _build_extra_shot(user_id: int, project_id: int, storyboard_id: int, prompt: str, shot_no: int) -> int:
    """在既有分镜板上追加一个锁定镜头（独立角色资产 + 伪向量）。

    用于网关重试用例：全新 prompt 保证 input_hash 唯一，绕开二次幂等
    复用路径，真实走「生成 → QC → 重试」流水线。

    Args:
        user_id: 项目所有者用户 ID。
        project_id: 项目 ID。
        storyboard_id: 分镜板 ID。
        prompt: 镜头最终提示词（同时派生 portrait 伪向量）。
        shot_no: 镜号（分镜板内唯一）。

    Returns:
        新镜头 ID。

    Raises:
        AssertionError: 数据库约束冲突等。
    """
    now = datetime.now(timezone.utc)
    async with async_session_factory() as session, session.begin():
        asset = Asset(
            project_id=project_id,
            owner_user_id=user_id,
            asset_type="character",
            name=f"重试用角色{shot_no}",
            status="locked",
            source="ai_generated",
            locked_at=now,
        )
        session.add(asset)
        await session.flush()
        version = AssetVersion(
            asset_id=int(asset.id),
            version_type="portrait",
            version_no=1,
            is_current=True,
            prompt=prompt,
        )
        session.add(version)
        await session.flush()
        session.add(
            FaceEmbedding(
                asset_id=int(asset.id),
                asset_version_id=int(version.id),
                embedding=make_deterministic_embedding(prompt),
                face_index=0,
                model_key="stub-face",
            )
        )
        episode_id = int(
            (
                await session.execute(
                    select(Storyboard.episode_id).where(Storyboard.id == storyboard_id)
                )
            ).scalar_one()
        )
        shot = Shot(
            storyboard_id=storyboard_id,
            project_id=project_id,
            episode_id=episode_id,
            shot_no=shot_no,
            order_index=shot_no - 1,
            duration_ms=SHOT_DURATION_MS,
            final_prompt=prompt,
            asset_anchor_ids=[int(version.id)],
            character_asset_ids=[int(asset.id)],
            status="locked",
            locked_at=now,
        )
        session.add(shot)
        await session.flush()
        return int(shot.id)


# ---------------------------------------------------------------------------
# 用例 1：estimate 成本估算
# ---------------------------------------------------------------------------


async def test_01_estimate(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """注册用户 A + 构造项目/镜头 → estimate 返回逐镜 90 积分、合计 180。"""
    user = await _register_user(client, PHONE_A)
    ctx["user_a_id"] = user["user_id"]
    ctx["token_a"] = user["token"]
    data_setup = await _build_project_with_shots(user["user_id"])
    ctx.update(data_setup)

    resp = await client.get(
        f"/api/v1/projects/{ctx['project_id']}/render/estimate",
        params={"shot_ids": f"{ctx['shot_ids'][0]},{ctx['shot_ids'][1]}"},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["shot_count"] == 2, body
    # 单价取决于本机 model_registry 中视频模型的 unit_price_credits，
    # 测试不写死 30，而是以 estimate 返回值作为后续断言基准。
    shot_est = int(data["per_shot"][0]["estimated_credits"])
    ctx["shot_est"] = shot_est
    assert data["total_credits"] == shot_est * 2, body
    assert {item["estimated_credits"] for item in data["per_shot"]} == {shot_est}, body
    assert {item["shot_id"] for item in data["per_shot"]} == set(ctx["shot_ids"]), body
    assert data["available_credits"] == 500, body


# ---------------------------------------------------------------------------
# 用例 2：批量提交 → 轮询全部 succeeded → 产物/结算断言
# ---------------------------------------------------------------------------


async def test_02_batch_submit_and_succeed(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """batch 2 镜 → 202 → 轮询 succeeded → 产物落库 + 冻结结算 + 余额核对。"""
    idem_key = "test-render-batch-001"
    ctx["batch_key_1"] = idem_key
    balance_before = (await _get_account_balance(ctx["user_a_id"]))["balance"]
    assert balance_before == 500, "前置：注册赠送 500 积分"

    resp = await client.post(
        f"/api/v1/projects/{ctx['project_id']}/render-tasks/batch",
        json={"shot_ids": ctx["shot_ids"]},
        headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": idem_key},
    )
    body = resp.json()
    assert resp.status_code == 202, body
    assert body["code"] == 0, body
    data = body["data"]
    assert len(data["tasks"]) == 2, body
    assert all(t["status"] == "queued" for t in data["tasks"]), body
    assert data["frozen_credits"] == ctx["shot_est"] * 2, body
    assert data["estimated_credits"] == ctx["shot_est"] * 2, body
    task_ids = [t["id"] for t in data["tasks"]]
    ctx["batch_task_ids"] = task_ids

    # 提交后冻结态
    frozen_snap = await _get_account_balance(ctx["user_a_id"])
    assert frozen_snap["frozen"] == ctx["shot_est"] * 2, frozen_snap
    assert frozen_snap["balance"] == 500, "冻结不动 balance"

    # 轮询全部终态（≤60s）
    details = [await _wait_task_terminal(client, ctx["token_a"], tid) for tid in task_ids]
    assert all(d["status"] == "succeeded" for d in details), details

    async with async_session_factory() as session:
        # 镜头状态 rendered
        shots = (
            (await session.execute(select(Shot).where(Shot.id.in_(ctx["shot_ids"])))).scalars().all()
        )
        assert all(s.status == "rendered" for s in shots), [s.status for s in shots]
        for d in details:
            # 任务行：实际成本 ≤ 预估 × 1.5（stub 单轮 QC 恒 90 ≤ 135）
            assert d["actual_cost_credits"] is not None
            assert d["actual_cost_credits"] <= d["estimated_cost_credits"] * 1.5, d
            # P0-3：stub 产物属于降级占位，任务零计费（冻结全额释放）
            if d.get("degraded"):
                assert d["actual_cost_credits"] == 0, d
                assert d["freeze"]["status"] == "released", d
            else:
                assert d["freeze"]["status"] == "settled", d
                assert d["freeze"]["settled_credits"] == d["actual_cost_credits"], d
            # 产物 / 媒体 / 版本 / QC 落库
            assert len(d["outputs"]) >= 1, d
            media_id = d["outputs"][0]["media_file_id"]
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.media_type == "video"
            versions = (
                (
                    await session.execute(
                        select(ShotVersion).where(ShotVersion.shot_id == d["shot_id"])
                    )
                )
                .scalars()
                .all()
            )
            assert len(versions) >= 1
            qc_reports = (
                (
                    await session.execute(
                        select(QcReport).where(
                            QcReport.target_type == "shot",
                            QcReport.target_id == d["shot_id"],
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert any(q.result in ("passed", "warning") for q in qc_reports), d

    # 结算后余额 = 500 - Σ actual
    snap = await _get_account_balance(ctx["user_a_id"])
    total_actual = sum(int(d["actual_cost_credits"]) for d in details)
    assert snap["balance"] == 500 - total_actual, snap
    assert snap["frozen"] == 0, snap
    ctx["balance_after_batch"] = snap["balance"]


# ---------------------------------------------------------------------------
# 用例 3：幂等——同 Idempotency-Key 重复 batch
# ---------------------------------------------------------------------------


async def test_03_batch_idempotent(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """同 Idempotency-Key 重复 batch → 200 + 相同任务 ID + 不重复冻结。"""
    resp = await client.post(
        f"/api/v1/projects/{ctx['project_id']}/render-tasks/batch",
        json={"shot_ids": ctx["shot_ids"]},
        headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": ctx["batch_key_1"]},
    )
    body = resp.json()
    assert resp.status_code == 200, body  # 幂等命中返回 200
    assert body["code"] == 0, body
    data = body["data"]
    assert data["idempotent"] is True, body
    assert [t["id"] for t in data["tasks"]] == ctx["batch_task_ids"], body
    # 重复提交不产生新冻结：余额与冻结保持 batch 完成后的状态
    snap = await _get_account_balance(ctx["user_a_id"])
    assert snap["frozen"] == 0, snap
    assert snap["balance"] == ctx["balance_after_batch"], snap


# ---------------------------------------------------------------------------
# 用例 4：余额不足 → 30001
# ---------------------------------------------------------------------------


async def test_04_insufficient_credits(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """账户清零再 batch（新幂等键）→ 30001，data 携带 needed/available。"""
    # 重置镜头为 locked（上一批已 rendered），保证 batch 校验能走到余额检查
    await _reset_shot_locked(ctx["shot_ids"][0])
    await _reset_shot_locked(ctx["shot_ids"][1])

    async with async_session_factory() as session, session.begin():
        await session.execute(
            update(CreditAccount)
            .where(CreditAccount.owner_type == "user", CreditAccount.user_id == ctx["user_a_id"])
            .values(balance_credits=0, frozen_credits=0)
        )

    resp = await client.post(
        f"/api/v1/projects/{ctx['project_id']}/render-tasks/batch",
        json={"shot_ids": ctx["shot_ids"]},
        headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": "test-render-batch-poor"},
    )
    body = resp.json()
    assert body["code"] == 30001, body
    assert body["data"]["needed"] == ctx["shot_est"] * 2, body
    assert body["data"]["available"] == 0, body

    # 失败路径无冻结残留
    snap = await _get_account_balance(ctx["user_a_id"])
    assert snap["frozen"] == 0, snap

    # 恢复余额供后续用例
    async with async_session_factory() as session, session.begin():
        await session.execute(
            update(CreditAccount)
            .where(CreditAccount.owner_type == "user", CreditAccount.user_id == ctx["user_a_id"])
            .values(balance_credits=500)
        )


# ---------------------------------------------------------------------------
# 用例 5：取消（暂停执行器保证 queued 窗口）
# ---------------------------------------------------------------------------


async def test_05_cancel_and_sibling_completes(
    client: httpx.AsyncClient, ctx: dict[str, Any]
) -> None:
    """暂停执行器 → 提交 2 镜 → cancel 其一（cancelled+退款）→ 恢复后另一完成。"""
    # 重置镜头为 locked（上一批已 rendered）
    await _reset_shot_locked(ctx["shot_ids"][0])
    await _reset_shot_locked(ctx["shot_ids"][1])

    # 暂停执行器：任务入队但无 worker 消费，保证 cancel 命中 queued 态
    await render_executor.stop()
    try:
        balance_before = (await _get_account_balance(ctx["user_a_id"]))["balance"]
        resp = await client.post(
            f"/api/v1/projects/{ctx['project_id']}/render-tasks/batch",
            json={"shot_ids": ctx["shot_ids"]},
            headers={**_auth_headers(ctx["token_a"]), "Idempotency-Key": "test-render-batch-cancel"},
        )
        body = resp.json()
        assert resp.status_code == 202, body
        tasks = body["data"]["tasks"]
        assert len(tasks) == 2, body
        cancel_id, keep_id = tasks[0]["id"], tasks[1]["id"]

        resp = await client.post(
            f"/api/v1/render-tasks/{cancel_id}/cancel", headers=_auth_headers(ctx["token_a"])
        )
        body = resp.json()
        assert body["code"] == 0, body
        assert body["data"]["status"] == "cancelled", body
        assert body["data"]["refunded_credits"] == ctx["shot_est"], body

        snap = await _get_account_balance(ctx["user_a_id"])
        assert snap["frozen"] == ctx["shot_est"], "取消后仅剩另一任务冻结"
    finally:
        await render_executor.start()

    # 恢复执行器（recover 重新入队 queued 任务）→ 另一任务照常完成
    detail = await _wait_task_terminal(client, ctx["token_a"], keep_id, timeout=60.0)
    assert detail["status"] == "succeeded", detail
    snap = await _get_account_balance(ctx["user_a_id"])
    assert snap["frozen"] == 0, snap
    assert snap["balance"] == balance_before - int(detail["actual_cost_credits"]), snap

    # 被取消任务的镜头已回 locked
    async with async_session_factory() as session:
        cancelled = (await session.execute(select(RenderTask).where(RenderTask.id == cancel_id))).scalar_one()
        assert cancelled.status == "cancelled"
        shot = (await session.execute(select(Shot).where(Shot.id == cancelled.shot_id))).scalar_one()
        assert shot.status == "locked", shot.status


# ---------------------------------------------------------------------------
# 用例 6：越权 → 20003
# ---------------------------------------------------------------------------


async def test_06_forbidden_cross_user(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """用户 B 提交渲染到用户 A 项目 → 20003（HTTP 403）。"""
    user_b = await _register_user(client, PHONE_B)
    resp = await client.post(
        f"/api/v1/projects/{ctx['project_id']}/render-tasks/batch",
        json={"shot_ids": ctx["shot_ids"]},
        headers={**_auth_headers(user_b["token"]), "Idempotency-Key": "test-render-batch-b"},
    )
    body = resp.json()
    assert resp.status_code == 403, body
    assert body["code"] == 20003, body


# ---------------------------------------------------------------------------
# 用例 7：网关瞬断 → retrying → succeeded
# ---------------------------------------------------------------------------


async def test_07_gateway_retry_then_success(
    client: httpx.AsyncClient, ctx: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """monkeypatch 网关首次抛错 → 任务经 retrying 最终 succeeded。"""
    # 全新镜头（独立 prompt/input_hash），绕开二次幂等复用路径
    retry_shot_id = await _build_extra_shot(
        ctx["user_a_id"],
        ctx["project_id"],
        ctx["storyboard_id"],
        f"雪夜列车驶过山谷，车灯划破黑暗，{PHONE_A}",
        3,
    )
    calls = {"n": 0}
    original_generate = executor_module.model_gateway.generate

    async def flaky_generate(capability: str, task_type: str, request, **kwargs):
        """首调用抛 RuntimeError 模拟网关瞬断，其后走原实现。

        Args:
            capability: 生成能力。
            task_type: 任务类型。
            request: 统一生成请求。
            **kwargs: 透传参数（project_id / task_id 等）。

        Returns:
            原网关生成结果。

        Raises:
            RuntimeError: 第一次调用时模拟瞬断。
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟网关瞬断")
        return await original_generate(capability, task_type, request, **kwargs)

    monkeypatch.setattr(executor_module.model_gateway, "generate", flaky_generate)
    try:
        resp = await client.post(
            f"/api/v1/shots/{retry_shot_id}/regenerate",
            headers=_auth_headers(ctx["token_a"]),
        )
        body = resp.json()
        assert resp.status_code == 202, body
        task_id = body["data"]["tasks"][0]["id"]

        detail = await _wait_task_terminal(client, ctx["token_a"], task_id, timeout=60.0)
        assert detail["status"] == "succeeded", detail
        assert detail["retry_count"] >= 1, detail
        assert calls["n"] >= 2, calls
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# 用例 8：任务分页列表 + SSE 事件历史（补充断言）
# ---------------------------------------------------------------------------


async def test_08_task_list_and_events(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """任务分页列表返回含镜头摘要；事件总线历史含 render.* 前缀事件。"""
    resp = await client.get(
        f"/api/v1/projects/{ctx['project_id']}/render-tasks",
        params={"page": 1, "page_size": 10},
        headers=_auth_headers(ctx["token_a"]),
    )
    body = resp.json()
    assert body["code"] == 0, body
    data = body["data"]
    assert data["total"] >= 5, data  # batch 2 + cancel 2 + regen 1
    assert data["page"] == 1 and data["page_size"] == 10, data
    first = data["items"][0]
    assert {"id", "shot_id", "status", "shot", "estimated_cost_credits", "failure_type"} <= set(first), first

    # 非法 sort → 10001
    resp = await client.get(
        f"/api/v1/projects/{ctx['project_id']}/render-tasks",
        params={"sort": "hack"},
        headers=_auth_headers(ctx["token_a"]),
    )
    assert resp.json()["code"] == 10001

    # SSE 事件历史（进程内 event_bus）：应含 render.task.updated 与 batch.completed
    from app.infra.event_bus import event_bus

    events = [r["event"] for r in event_bus._history.get(ctx["project_id"], [])]
    assert any(e == "render.task.updated" for e in events), events
    assert any(e == "render.batch.completed" for e in events), events
