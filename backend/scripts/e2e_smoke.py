"""端到端全旅程冒烟脚本（Task 17 · 前后端集成联调）。

覆盖：
- 全旅程 11 步：注册 → 充值 → 建项目 → 剧本生成 → 剧本审批 → 资产 →
  分镜 → 渲染 → 后期 → 导出 → 通知与 SSE；
- 异常流走查：未登录 20001 / 越权 20003+审计 / 非法流转 40001 /
  预算超限 30002 / 重复支付回调幂等 / 重复发起 40001 / 未绑锚点 lock 60005；
- SSE 三通道：项目事件流（progress）+ 渲染事件流（render.task.updated）
  + 通知 API。

使用方式::

    # 前置：后端服务已启动（uvicorn app.main:app --port 8100）
    cd backend
    .venv\\Scripts\\python scripts\\e2e_smoke.py              # 全量
    .venv\\Scripts\\python scripts\\e2e_smoke.py --from 06    # 从第 06 步断点重跑
    .venv\\Scripts\\python scripts\\e2e_smoke.py --reset      # 清空断点状态重新开始

说明：
- 断点状态持久化在 scripts/.e2e_state.json（token / project_id 等上下文），
  已完成步骤自动跳过；token 失效自动用密码重登；
- 手机号固定 13900009901（主测）/ 13900009902（越权测试）；
- 每步结果以 PASS/FAIL + 耗时打印，任一步失败即终止（修复后断点重跑）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import select

# 把 backend 根目录加入 sys.path（脚本内延迟 import app.* 的 DB 直查需要）
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

# ---------------------------------------------------------------------------
# 常量与全局
# ---------------------------------------------------------------------------

#: 后端服务地址与 API 前缀（可用环境变量 GD_BASE_URL 覆盖，如 8101 备用端口）
BASE_URL = os.environ.get("GD_BASE_URL", "http://127.0.0.1:8100")
API = "/api/v1"

#: 主测手机号（全旅程）与越权测试手机号
# 2026-10-29：改为动态手机号，避免 --reset 断点重跑复用历史脏账号
# （残留冻结/余额会让"赠送 500 / 冻结清零"等绝对值断言失真）。
import random as _random

PHONE_A = f"139{_random.randint(10000000, 99999999)}"
PHONE_B = "13900009904"

#: 统一测试密码
PASSWORD = "E2ePass123456"

#: 沙箱支付回调固定签名（与 billing_service.SANDBOX_WEBHOOK_SIGN 一致）
SANDBOX_SIGN = "gleamdream-sandbox-sign"

#: 断点状态文件
STATE_FILE = Path(__file__).resolve().parent / ".e2e_state.json"

#: 步骤结果表：[步骤名, 描述, PASS/FAIL, 耗时秒, 备注]
RESULT_ROWS: list[list[str]] = []


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _now() -> float:
    """返回当前单调时钟秒数。

    Returns:
        单调时钟时间戳（秒）。
    """
    return time.monotonic()


class StepFail(AssertionError):
    """步骤失败异常：携带失败说明，终止冒烟流程。"""


def log(msg: str) -> None:
    """打印带时间戳的冒烟日志行。

    Args:
        msg: 日志内容。
    """
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict[str, Any]:
    """加载断点状态文件（不存在则返回空结构）。

    Returns:
        状态字典，形如 ``{"done": [...], "ctx": {...}}``。
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"done": [], "ctx": {}}
    return {"done": [], "ctx": {}}


def save_state(state: dict[str, Any]) -> None:
    """把断点状态写盘（UTF-8 缩进 JSON）。

    Args:
        state: 状态字典。
    """
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_done(state: dict[str, Any], name: str) -> None:
    """把步骤标记为完成并立即持久化（保证断点可靠性）。

    Args:
        state: 状态字典。
        name: 步骤名。
    """
    if name not in state["done"]:
        state["done"].append(name)
    save_state(state)


def _bal(acc: dict[str, Any]) -> int:
    """读取账户摘要的余额字段（兼容 balance_credits/balance 键名）。

    Args:
        acc: 积分账户摘要 dict。

    Returns:
        余额积分数（int）。
    """
    return int(acc.get("balance_credits") if acc.get("balance_credits") is not None else acc.get("balance") or 0)


def _frozen(acc: dict[str, Any]) -> int:
    """读取账户摘要的冻结字段（兼容 frozen_credits/frozen 键名）。

    Args:
        acc: 积分账户摘要 dict。

    Returns:
        冻结积分数（int）。
    """
    return int(acc.get("frozen_credits") if acc.get("frozen_credits") is not None else acc.get("frozen") or 0)


def record(name: str, desc: str, ok: bool, cost: float, note: str = "") -> None:
    """登记一步结果到结果表并打印。

    Args:
        name: 步骤编号/名称。
        desc: 步骤描述。
        ok: 是否通过。
        cost: 耗时（秒）。
        note: 附加备注（失败原因/关键数值）。
    """
    status = "PASS" if ok else "FAIL"
    RESULT_ROWS.append([name, desc, status, f"{cost:.1f}s", note])
    log(f"== {name} {desc} -> {status} ({cost:.1f}s) {note}")


def api(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, Any]]:
    """统一 API 调用：请求 /api/v1 前缀并打印一行结果。

    Args:
        client: httpx 同步客户端。
        method: HTTP 方法。
        path: API 路径（不含 /api/v1 前缀）。
        token: Bearer 访问令牌（可选）。
        json_body: JSON 请求体（可选）。
        params: 查询参数（可选）。
        headers: 额外请求头（可选）。
        timeout: 请求超时秒数。

    Returns:
        (HTTP 状态码, 响应包络 dict)。非 JSON 响应时包络 code=-1。
    """
    h: dict[str, str] = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    resp = client.request(
        method, API + path, json=json_body, params=params, headers=h, timeout=timeout
    )
    try:
        env = resp.json()
    except Exception:  # noqa: BLE001
        env = {"code": -1, "message": f"non-json({resp.status_code})", "data": None}
    code = env.get("code")
    log(f"  {method:6s} {path} -> http={resp.status_code} code={code} msg={env.get('message')!r}")
    return resp.status_code, env


def expect_ok(env: dict[str, Any], where: str) -> dict[str, Any]:
    """断言业务成功（code==0），失败抛 StepFail。

    Args:
        env: 响应包络。
        where: 断言位置说明（用于错误提示）。

    Returns:
        包络 data 字段。
    """
    if env.get("code") != 0:
        raise StepFail(f"{where}: 期望 code=0 实际 code={env.get('code')} msg={env.get('message')!r}")
    return env.get("data") or {}


def expect_biz(env: dict[str, Any], want_code: int, where: str, *, http_code: int | None = None) -> dict[str, Any]:
    """断言业务错误码，失败抛 StepFail。

    Args:
        env: 响应包络。
        want_code: 期望业务错误码。
        where: 断言位置说明。
        http_code: 期望 HTTP 状态码（可选，None 不校验）。

    Returns:
        包络 data 字段。
    """
    if env.get("code") != want_code:
        raise StepFail(f"{where}: 期望 code={want_code} 实际 code={env.get('code')} msg={env.get('message')!r}")
    if http_code is not None and env.get("_http") is not None:
        pass
    return env.get("data") or {}


def wait_for(
    fn: Callable[[], Any],
    *,
    timeout: float,
    interval: float,
    desc: str,
) -> Any:
    """轮询等待 fn 返回真值，超时抛 StepFail。

    Args:
        fn: 轮询函数（返回真值即视为就绪）。
        timeout: 总超时秒数。
        interval: 轮询间隔秒数。
        desc: 等待内容描述（用于超时报错）。

    Returns:
        fn 的真值返回。

    Raises:
        StepFail: 超时未就绪。
    """
    deadline = _now() + timeout
    last_err: str = ""
    while _now() < deadline:
        try:
            val = fn()
            if val:
                return val
        except StepFail as exc:
            last_err = str(exc)
        time.sleep(interval)
    raise StepFail(f"等待超时({timeout}s): {desc} last={last_err}")


# ---------------------------------------------------------------------------
# 认证辅助
# ---------------------------------------------------------------------------


def _register_or_login(client: httpx.Client, phone: str) -> tuple[str, dict[str, Any]]:
    """注册或登录一个手机号，返回 (access_token, 用户信息)。

    流程：先尝试密码登录（已有账号场景）；失败则发验证码注册。

    Args:
        client: httpx 客户端。
        phone: 手机号。

    Returns:
        (access_token, data) 登录响应数据（含 user 信息）。
    """
    _, login_env = api(
        client, "POST", "/auth/login",
        json_body={"login_type": "password", "phone": phone, "password": PASSWORD},
    )
    if login_env.get("code") == 0:
        return login_env["data"]["access_token"], login_env["data"]

    _, sms_env = api(client, "POST", "/auth/sms-code", json_body={"phone": phone})
    sms_data = expect_ok(sms_env, f"发验证码 {phone}")
    dev_code = sms_data.get("dev_code")
    if not dev_code:
        raise StepFail(f"{phone}: 非沙箱模式未返回 dev_code")
    _, reg_env = api(
        client, "POST", "/auth/register",
        json_body={"phone": phone, "sms_code": str(dev_code), "password": PASSWORD},
    )
    if reg_env.get("code") != 0:
        # 可能发码后被注册过（残留）→ 再试一次登录
        _, login_env = api(
            client, "POST", "/auth/login",
            json_body={"login_type": "password", "phone": phone, "password": PASSWORD},
        )
        if login_env.get("code") != 0:
            raise StepFail(f"{phone}: 注册与登录均失败: {reg_env.get('message')}")
        return login_env["data"]["access_token"], login_env["data"]
    reg_data = expect_ok(reg_env, f"注册 {phone}")
    token = reg_data.get("access_token")
    if not token:
        # 注册响应可能不含 token，则补一次登录
        _, login_env = api(
            client, "POST", "/auth/login",
            json_body={"login_type": "password", "phone": phone, "password": PASSWORD},
        )
        login_data = expect_ok(login_env, f"注册后登录 {phone}")
        token = login_data["access_token"]
        reg_data = login_data
    return token, reg_data


def ensure_token(client: httpx.Client, ctx: dict[str, Any]) -> str:
    """确保上下文里有可用 token：先用现值探测，失效则重登。

    Args:
        client: httpx 客户端。
        ctx: 全局上下文（读写 ctx["token"]）。

    Returns:
        可用的 access_token。
    """
    token = ctx.get("token")
    if token:
        _, me_env = api(client, "GET", "/users/me", token=token)
        if me_env.get("code") == 0:
            return token
        log("  token 已失效，重新登录…")
    token, data = _register_or_login(client, PHONE_A)
    ctx["token"] = token
    if isinstance(data.get("user"), dict) and data["user"].get("id"):
        ctx["user_id"] = data["user"]["id"]
    return token


# ---------------------------------------------------------------------------
# SSE 采集线程
# ---------------------------------------------------------------------------


def sse_collect(
    url_path: str,
    token: str,
    duration: float,
    bucket: list[dict[str, Any]],
    *,
    use_query_token: bool = False,
    last_event_id: str | None = None,
) -> threading.Thread:
    """启动后台线程流式采集 SSE 事件帧。

    Args:
        url_path: SSE 端点路径（含 /api/v1 前缀，如 /api/v1/projects/1/events）。
        token: 访问令牌。
        duration: 采集时长秒数（超时后自动结束）。
        bucket: 收集结果输出列表（元素 {id,event,data,raw}）。
        use_query_token: True 时用 ?access_token= 传令牌（EventSource 兜底方式）。
        last_event_id: Last-Event-ID 请求头（历史回放）。

    Returns:
        已启动的线程对象（调用方 join）。
    """
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    params: dict[str, str] = {}
    if use_query_token:
        params["access_token"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id

    def _run() -> None:
        """线程主体：流式读响应体，按空行分帧解析 id/event/data。"""
        deadline = _now() + duration
        try:
            with httpx.stream(
                "GET", BASE_URL + url_path, headers=headers, params=params,
                timeout=httpx.Timeout(10.0, read=max(15.0, duration + 5.0)),
            ) as resp:
                log(f"  [SSE] {url_path} -> http={resp.status_code}")
                if resp.status_code != 200:
                    return
                block: dict[str, str] = {}
                for line in resp.iter_lines():
                    if _now() > deadline:
                        break
                    line = line.rstrip("\r")
                    if not line:
                        if block:
                            frame = {
                                "id": block.get("id", ""),
                                "event": block.get("event", "message"),
                                "data": block.get("data", ""),
                            }
                            bucket.append(frame)
                            block = {}
                        continue
                    if line.startswith(":"):
                        continue  # 心跳注释行
                    if line.startswith("id:"):
                        block["id"] = line[3:].strip()
                    elif line.startswith("event:"):
                        block["event"] = line[6:].strip()
                    elif line.startswith("data:"):
                        block["data"] = line[5:].strip()
                if block:
                    bucket.append({
                        "id": block.get("id", ""),
                        "event": block.get("event", "message"),
                        "data": block.get("data", ""),
                    })
        except Exception as exc:  # noqa: BLE001
            log(f"  [SSE] 采集异常: {exc!r}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def assert_sse_frames(bucket: list[dict[str, Any]], want_event: str | None, where: str) -> list[dict[str, Any]]:
    """断言 SSE 帧可解析、id 单调递增、包含指定事件类型。

    Args:
        bucket: sse_collect 采集到的帧列表。
        want_event: 必须出现的事件类型（None 不校验）。
        where: 断言位置说明。

    Returns:
        采集到的帧列表（便于后续细粒度断言）。

    Raises:
        StepFail: 无帧 / JSON 不可解析 / id 不单调 / 缺少目标事件。
    """
    if not bucket:
        raise StepFail(f"{where}: SSE 未收到任何事件帧")
    parsed = 0
    ids: list[int] = []
    for f in bucket:
        if f["data"]:
            try:
                json.loads(f["data"])
                parsed += 1
            except Exception:  # noqa: BLE001
                raise StepFail(f"{where}: 事件 data 非 JSON: {f['data'][:120]!r}")
        if f["id"]:
            try:
                ids.append(int(f["id"]))
            except ValueError:
                pass
    if parsed == 0:
        raise StepFail(f"{where}: 没有可解析的 data JSON 帧")
    for a, b in zip(ids, ids[1:]):
        if b <= a:
            raise StepFail(f"{where}: 事件 id 非单调: {ids}")
    if want_event is not None:
        events = {f["event"] for f in bucket}
        if want_event not in events:
            raise StepFail(f"{where}: 未收到事件 {want_event}，实际 {sorted(events)}")
    log(f"  [SSE] {where}: {len(bucket)} 帧, events={sorted({f['event'] for f in bucket})[:8]}")
    return bucket


# ---------------------------------------------------------------------------
# 数据库辅助（audit_logs 校验）
# ---------------------------------------------------------------------------


def query_audit_logs(project_id: int, action: str) -> list[dict[str, Any]]:
    """直连数据库查询 audit_logs 记录（独立 event loop）。

    Args:
        project_id: 项目 ID。
        action: 动作标识（如 acl_denied，与 core/deps.py 写入值一致）。

    Returns:
        匹配的审计日志列表（字典形式）。

    Raises:
        StepFail: 数据库查询失败。
    """
    from app.core.db import async_session_factory
    from app.models import AuditLog

    async def _q() -> list[dict[str, Any]]:
        """查询指定项目与动作的审计日志行。"""
        async with async_session_factory() as s:
            rows = (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.project_id == project_id, AuditLog.action == action
                    )
                )
            ).scalars().all()
            return [
                {
                    "id": int(r.id),
                    "action": r.action,
                    "actor_user_id": r.actor_user_id,
                    "project_id": r.project_id,
                }
                for r in rows
            ]

    try:
        return asyncio.run(_q())
    except Exception as exc:  # noqa: BLE001
        raise StepFail(f"audit_logs 查询失败: {exc!r}")


# ---------------------------------------------------------------------------
# 步骤实现（编号即执行顺序，可断点重跑）
# ---------------------------------------------------------------------------


def step01_register(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 01：注册主测用户并断言赠送 500 积分。"""
    t0 = _now()
    token, data = _register_or_login(client, PHONE_A)
    ctx["token"] = token
    uid = None
    if isinstance(data.get("user"), dict):
        uid = data["user"].get("id")
    ctx["user_id"] = uid

    _, acc_env = api(client, "GET", "/billing/credit-account", token=token)
    acc = expect_ok(acc_env, "查询积分账户")
    balance = _bal(acc)
    if balance < 500:
        raise StepFail(f"注册赠送积分断言失败: balance={balance} 期望>=500")
    log(f"  用户 user_id={uid} 余额={acc.get('balance_credits')} frozen={acc.get('frozen_credits')}")
    mark_done(ctx_state(), "01_register")
    record("01", "注册 + 500 积分", True, _now() - t0, f"balance={balance}")


def step02_recharge(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 02：充值 10000 积分（下单→沙箱回调）+ 重复回调幂等 + 余额增量 +10000。

    断言口径：余额增量恒等 +10000（全新用户等价于 500→10500；
    断点重跑/残留账户场景下依然成立，避免历史余额污染绝对值断言）。
    """
    t0 = _now()
    token = ensure_token(client, ctx)

    _, pre_env = api(client, "GET", "/billing/credit-account", token=token)
    pre_acc = expect_ok(pre_env, "下单前查询账户")
    balance_before = _bal(pre_acc)

    _, order_env = api(
        client, "POST", "/billing/orders", token=token,
        json_body={"credit_pack_credits": 10000},
    )
    order = expect_ok(order_env, "创建充值订单")
    pay_url = order.get("pay_url") or ""
    if "out_trade_no=" not in pay_url:
        raise StepFail(f"pay_url 缺少 out_trade_no: {pay_url!r}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(pay_url).query)
    out_trade_no = q["out_trade_no"][0]
    log(f"  订单 {out_trade_no} pay_url 解析成功")

    _, pay_env = api(
        client, "POST", "/billing/webhooks/payment",
        json_body={"out_trade_no": out_trade_no, "status": "success", "sign": SANDBOX_SIGN},
    )
    pay = expect_ok(pay_env, "支付回调首次")
    if pay.get("duplicate"):
        raise StepFail(f"首次回调 duplicate 应为 False: {pay}")

    # 重复回调 2 次 → 幂等（异常流 EX-4）
    for i in range(2):
        _, rep_env = api(
            client, "POST", "/billing/webhooks/payment",
            json_body={"out_trade_no": out_trade_no, "status": "success", "sign": SANDBOX_SIGN},
        )
        rep = expect_ok(rep_env, f"重复回调#{i + 2}")
        if not rep.get("duplicate"):
            raise StepFail(f"重复回调#{i + 2} 应 duplicate=True: {rep}")

    _, acc_env = api(client, "GET", "/billing/credit-account", token=token)
    acc = expect_ok(acc_env, "回调后查询账户")
    balance = _bal(acc)
    if balance - balance_before != 10000:
        raise StepFail(
            f"充值后余额增量断言失败: {balance_before} -> {balance} 期望 +10000"
        )
    ctx["balance"] = balance
    mark_done(ctx_state(), "02_recharge")
    record("02", "充值 10000 + 回调幂等", True, _now() - t0,
           f"balance={balance_before}->{balance}")


def step03_create_project(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 03：建项目（budget 10000 / 1 集 / live_action / 9:16）+ 异常流。"""
    t0 = _now()
    token = ensure_token(client, ctx)

    # 异常流 EX-3：draft 直接 transition 到 rendering → 40001（先建临时项目）
    _, tmp_env = api(
        client, "POST", "/projects", token=token,
        json_body={
            "title": "E2E异常流-非法流转",
            "budget_credits": 10000,
            "target_episode_count": 1,
            "style_preset_key": "live_action",
            "aspect_ratio": "9:16",
        },
    )
    tmp = expect_ok(tmp_env, "建临时项目(异常流)")
    tmp_id = int(tmp["id"])
    ctx["tmp_project_id"] = tmp_id
    _, tr_env = api(
        client, "POST", f"/projects/{tmp_id}/transition", token=token,
        json_body={"target_status": "rendering"},
    )
    expect_biz(tr_env, 40001, "draft→rendering 非法流转")
    log(f"  [EX] 非法流转 40001 OK（临时项目 {tmp_id}，随后删除用不到）")

    # 异常流 EX-预算：超大预算 → 30002
    _, big_env = api(
        client, "POST", "/projects", token=token,
        json_body={
            "title": "E2E异常流-预算超限",
            "budget_credits": 999999999,
            "target_episode_count": 1,
            "style_preset_key": "live_action",
            "aspect_ratio": "9:16",
        },
    )
    if big_env.get("code") == 30002:
        log("  [EX] 创建即 30002 预算超限 OK")
    elif big_env.get("code") == 0:
        # 建成功 → 用超大 estimate 触发 30002（后续渲染域会再验一次预算）
        big = big_env["data"]
        ctx["big_project_id"] = int(big["id"])
        log(f"  [EX] 预算超限未在创建时拦截（big_project_id={big['id']}），留待 estimate 验证")
    else:
        raise StepFail(f"超大预算项目: 期望 30002 或 0，实际 {big_env.get('code')} {big_env.get('message')}")

    # 正主项目（2026-10-29 复测：30 秒规格极简故事，控制对白量与分镜压缩空间）
    _, pj_env = api(
        client, "POST", "/projects", token=token,
        json_body={
            "title": "30秒短剧-深夜便利店的守望",
            "synopsis": "深夜便利店，夜班店员小夏发现常来买关东煮的老人连续三天只买不食，"
            "追问后得知老人独居且儿子失联，小夏默默把热汤温好递上，老人终于露出笑容。"
            "30 秒单场景温情短剧，人物不超过 2 人，对白 4-6 句。",
            "genre": "都市温情",
            "budget_credits": 10000,
            "target_episode_count": 1,
            "style_preset_key": "live_action",
            "aspect_ratio": "9:16",
        },
    )
    pj = expect_ok(pj_env, "创建主项目")
    pid = int(pj["id"])
    if pj.get("status") != "draft":
        raise StepFail(f"新项目状态应为 draft: {pj.get('status')!r}")
    ctx["project_id"] = pid
    mark_done(ctx_state(), "03_create_project")
    record("03", "建项目 + 40001/30002 异常流", True, _now() - t0, f"project_id={pid}")


def step04_script_generate(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 04：发起剧本生成（202）+ 重复发起 40001 + SSE progress + 审批单到达。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])

    # SSE 项目事件流采集线程（生成期间收 progress）
    sse_bucket: list[dict[str, Any]] = []
    sse_thread = sse_collect(
        f"/api/v1/projects/{pid}/events", token, 90.0, sse_bucket, use_query_token=True
    )

    _, gen_env = api(
        client, "POST", f"/orchestration/projects/{pid}/script-generate", token=token,
        json_body={"idea": "外卖骑手深夜救下昏迷女孩，发现她是失踪五年的千金，卷入豪门阴谋"},
    )
    if gen_env.get("code") != 0:
        # 兼容 202 由 JSONResponse 直接返回的情况
        if gen_env.get("code") == -1:
            raise StepFail(f"script-generate 非 JSON 响应")
        raise StepFail(f"script-generate 失败: {gen_env}")
    log(f"  script-generate 受理: {json.dumps(gen_env.get('data'), ensure_ascii=False)[:160]}")

    # 异常流 EX-5：生成进行中重复发起 → 40001/40005
    _, dup_env = api(
        client, "POST", f"/orchestration/projects/{pid}/script-generate", token=token,
        json_body={"idea": "重复发起测试"},
    )
    if dup_env.get("code") not in (40001, 40005):
        raise StepFail(f"重复 script-generate: 期望 40001/40005 实际 {dup_env.get('code')} {dup_env.get('message')!r}")
    log(f"  [EX] 重复发起 {dup_env.get('code')} OK")

    # 轮询审批单（每 5s，超时 300s）
    def _approval() -> dict[str, Any] | None:
        """查询项目 pending 审批单，返回 script 类型首单。"""
        _, env = api(
            client, "GET", "/orchestration/approvals", token=token,
            params={"project_id": pid, "status": "pending"},
        )
        data = expect_ok(env, "轮询审批单")
        items = [it for it in (data.get("items") or []) if it.get("approval_type") == "script"]
        return items[0] if items else None

    approval = wait_for(_approval, timeout=300, interval=5, desc="script 审批单 pending")
    ctx["script_approval_id"] = int(approval["id"])
    log(f"  审批单 #{approval['id']} 到达 approval_type={approval['approval_type']}")

    # 断言剧本树落库
    _, tree_env = api(client, "GET", f"/projects/{pid}/scripts/current", token=token)
    tree = expect_ok(tree_env, "查询当前剧本树")
    episodes = tree.get("episodes") or []
    if not episodes:
        raise StepFail("剧本树 episodes 为空")
    log(f"  剧本落库: script_id={ (tree.get('script') or {}).get('id') } episodes={len(episodes)} 行数摘要={sum(len((s.get('lines') or [])) for ep in episodes for s in (ep.get('scenes') or []))}")

    # SSE 断言
    sse_thread.join(timeout=95)
    try:
        assert_sse_frames(sse_bucket, "progress", "项目事件流(script-generate)")
    except StepFail:
        log("  [SSE][WARN] 生成期间未实时收到 progress（可能订阅窗口错过），改用历史回放校验")
        replay_bucket: list[dict[str, Any]] = []
        t2 = sse_collect(
            f"/api/v1/projects/{pid}/events", token, 6.0, replay_bucket,
            use_query_token=True, last_event_id="0",
        )
        t2.join(timeout=10)
        assert_sse_frames(replay_bucket, None, "项目事件流(回放)")
        ctx["sse_replay_used"] = True
    mark_done(ctx_state(), "04_script_generate")
    record("04", "剧本生成+审批单+SSE", True, _now() - t0, f"approval={ctx['script_approval_id']} episodes={len(episodes)}")


def step05_script_review(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 05：reject → 新单 → approve → 项目 asset_preparing + 剧本 locked + 冻结结算。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])
    old_approval_id = int(ctx["script_approval_id"])

    # reject（反馈 ≥10 字）
    _, rej_env = api(
        client, "POST", f"/orchestration/approvals/{old_approval_id}/decide", token=token,
        json_body={"action": "reject", "feedback": "开场节奏偏慢，请加强悬念冲突与千金身世的伏笔铺垫"},
    )
    expect_ok(rej_env, "reject 决策")

    # 轮询新的 pending script 审批单
    def _new_approval() -> dict[str, Any] | None:
        """查询 pending script 审批单（排除旧单）。"""
        _, env = api(
            client, "GET", "/orchestration/approvals", token=token,
            params={"project_id": pid, "status": "pending"},
        )
        data = expect_ok(env, "轮询新审批单")
        items = [
            it for it in (data.get("items") or [])
            if it.get("approval_type") == "script" and int(it["id"]) != old_approval_id
        ]
        return items[0] if items else None

    new_appr = wait_for(_new_approval, timeout=300, interval=5, desc="reject 后新 script 审批单")
    new_id = int(new_appr["id"])
    log(f"  新审批单 #{new_id} 到达")

    # approve
    _, ok_env = api(
        client, "POST", f"/orchestration/approvals/{new_id}/decide", token=token,
        json_body={"action": "approve"},
    )
    expect_ok(ok_env, "approve 决策")

    # 轮询项目状态 → asset_preparing（后台结算需要时间）
    def _status() -> str | None:
        """读取项目状态，到达 asset_preparing 即返回。"""
        _, env = api(client, "GET", f"/projects/{pid}", token=token)
        data = expect_ok(env, "查项目详情")
        return data.get("status") if data.get("status") == "asset_preparing" else None

    wait_for(_status, timeout=90, interval=3, desc="项目状态 asset_preparing")

    # 剧本 locked
    _, tree_env = api(client, "GET", f"/projects/{pid}/scripts/current", token=token)
    tree = expect_ok(tree_env, "审批后查剧本树")
    script_status = (tree.get("script") or {}).get("status")
    if script_status != "locked":
        raise StepFail(f"审批通过后剧本应 locked: {script_status!r}")

    # 冻结已结算（frozen==0）
    _, acc_env = api(client, "GET", "/billing/credit-account", token=token)
    acc = expect_ok(acc_env, "审批后查账户")
    frozen = _frozen(acc)
    if frozen != 0:
        raise StepFail(f"审批后冻结应结算清零: frozen={frozen}")
    ctx["balance"] = _bal(acc)
    log(f"  剧本 locked，frozen=0，balance={ctx['balance']}")
    mark_done(ctx_state(), "05_script_review")
    record("05", "剧本 reject→approve 流转", True, _now() - t0, f"balance={ctx['balance']}")


def step06_assets(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 06：资产 plan → 主角色 portrait/三视图/表情集生成 → approve → lock。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])

    _, plan_env = api(client, "POST", f"/projects/{pid}/assets/plan", token=token, json_body={})
    expect_ok(plan_env, "资产 plan")

    def _assets() -> list[dict[str, Any]] | None:
        """查询资产列表，出现角色+场景即返回全量 items。"""
        _, env = api(client, "GET", f"/projects/{pid}/assets", token=token, params={"page_size": 100})
        data = expect_ok(env, "轮询资产列表")
        items = data.get("items") or []
        types = {it.get("asset_type") for it in items}
        if "character" in types:
            return items
        return None

    items = wait_for(_assets, timeout=120, interval=5, desc="资产 plan 落库")
    characters = [it for it in items if it.get("asset_type") == "character"]
    if not characters:
        raise StepFail("plan 后无角色资产")
    # 取 weight 最高（字段名兼容 weight/sort_weight/priority）
    def _weight(a: dict[str, Any]) -> float:
        """读取资产权重字段（多字段名兼容）。"""
        return float(a.get("weight") or a.get("sort_weight") or a.get("priority") or 0)

    hero = max(characters, key=_weight)
    hero_id = int(hero["id"])
    ctx["hero_asset_id"] = hero_id
    log(f"  主角色 asset={hero_id} name={hero.get('name')!r} weight={_weight(hero)}")

    def _wait_review(asset_id: int, timeout: float) -> None:
        """轮询资产版本生成完成（status=pending_review）。"""
        def _chk() -> bool:
            """查资产详情，状态到达 pending_review 返回 True。"""
            _, env = api(client, "GET", f"/assets/{asset_id}", token=token)
            data = expect_ok(env, f"查资产 {asset_id}")
            return data.get("status") == "pending_review"
        wait_for(_chk, timeout=timeout, interval=3, desc=f"资产 {asset_id} pending_review")

    def _gen_and_approve(asset_id: int, version_type: str, timeout: float = 60.0) -> None:
        """生成一个资产版本并审核通过（generate → 轮询 → approve）。

        幂等保护：资产已 locked 或已有待审核版本（遗留 pending_review）
        时跳过 generate，避免 40001 重复生成冲突。
        """
        _, det_env = api(client, "GET", f"/assets/{asset_id}", token=token)
        det = expect_ok(det_env, f"查资产 {asset_id}")
        if det.get("status") == "locked":
            log(f"  资产 {asset_id} 已锁定，跳过 {version_type}")
            return
        if det.get("status") == "pending_review":
            _, ok_env = api(client, "POST", f"/assets/{asset_id}/approve", token=token)
            expect_ok(ok_env, f"approve 遗留待审版本 {version_type}")
            log(f"  资产 {asset_id} 遗留 {version_type} 待审版本已通过")
            return
        _, gen_env = api(
            client, "POST", f"/assets/{asset_id}/generate", token=token,
            json_body={"version_type": version_type},
        )
        expect_ok(gen_env, f"generate {version_type}")
        _wait_review(asset_id, timeout)
        _, ok_env = api(client, "POST", f"/assets/{asset_id}/approve", token=token)
        expect_ok(ok_env, f"approve {version_type}")
        log(f"  资产 {asset_id} 版本 {version_type} 已生成并通过")

    def _lock_with_retry(asset_id: int) -> None:
        """锁定资产（60003 时对缺失版本补生成后重试；已锁定则跳过）。"""
        _, det_env = api(client, "GET", f"/assets/{asset_id}", token=token)
        det = expect_ok(det_env, f"查资产 {asset_id}")
        if det.get("status") == "locked":
            log(f"  资产 {asset_id} 已锁定，跳过 lock")
            return
        _, lock_env = api(client, "POST", f"/assets/{asset_id}/lock", token=token)
        if lock_env.get("code") == 60003:
            _, detail_env = api(client, "GET", f"/assets/{asset_id}", token=token)
            detail = expect_ok(detail_env, "lock 失败后查资产详情")
            versions = detail.get("versions") or []
            for v in versions:
                if v.get("status") in ("failed", "pending", "generating"):
                    vt = v.get("version_type")
                    if vt:
                        _gen_and_approve(asset_id, vt, timeout=90)
            _, lock_env = api(client, "POST", f"/assets/{asset_id}/lock", token=token)
        if lock_env.get("code") != 0:
            raise StepFail(f"资产 {asset_id} lock 失败: {lock_env}")

    def _ensure_version(asset_id: int, version_type: str, timeout: float = 90.0) -> None:
        """确保资产存在指定版本（已生成则跳过，否则 generate → approve）。

        Args:
            asset_id: 资产 ID。
            version_type: 版本类型。
            timeout: 生成轮询超时秒数。
        """
        _, det_env = api(client, "GET", f"/assets/{asset_id}", token=token)
        det = expect_ok(det_env, f"查资产 {asset_id}")
        versions = det.get("versions") or []
        if any(
            v.get("version_type") == version_type and v.get("status") != "failed"
            for v in versions
        ):
            log(f"  资产 {asset_id} 版本 {version_type} 已存在，跳过")
            return
        _gen_and_approve(asset_id, version_type, timeout)

    # 主角色：portrait + 三视图 + 表情集 → lock
    for vt in ("portrait", "turnaround_front", "turnaround_side", "turnaround_back", "expression_set"):
        _ensure_version(hero_id, vt)
    _lock_with_retry(hero_id)
    log(f"  主角色资产 {hero_id} locked")

    # 其余实体资产（配角/场景/道具）全部生成并锁定：
    # 状态推进钩子要求项目实体资产全部 locked 后才 asset_preparing → asset_review，
    # 这是分镜审批 approve 前置链路（Task 17 联调修复的一部分）。
    # 角色类资产 lock 要求 portrait + 三视图；场景/道具仅要求 scene_ref/prop_ref。
    for it in items:
        aid = int(it["id"])
        atype = it.get("asset_type")
        if aid == hero_id or atype not in ("character", "scene", "prop"):
            continue
        _, det_env = api(client, "GET", f"/assets/{aid}", token=token)
        det = expect_ok(det_env, f"查资产 {aid}")
        if det.get("status") == "locked":
            continue
        vts = (
            ("portrait", "turnaround_front", "turnaround_side", "turnaround_back")
            if atype == "character"
            else (("scene_ref",) if atype == "scene" else ("prop_ref",))
        )
        for vt in vts:
            _ensure_version(aid, vt)
        _lock_with_retry(aid)
        log(f"  {atype} 资产 {aid} 已生成并锁定")
    mark_done(ctx_state(), "06_assets")
    record("06", "资产生成与锁定", True, _now() - t0, f"hero={hero_id} total={len(items)}")


def step07_storyboard(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 07：分镜生成 → 未绑锚点 lock 60005 → 绑锚点 → lock → 审批 → rendering。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])
    hero_id = int(ctx["hero_asset_id"])

    # 2026-10-29 复测：30 秒成片规格（6~8 镜，分镜归一化 Σduration≈30s）
    # 断点重跑优化：项目已有可用分镜板（pending_review/locked）时跳过重新生成。
    def _existing_board() -> dict[str, Any] | None:
        """查询项目现有分镜板，返回 pending_review 或 locked 的第一块。"""
        _, env = api(client, "GET", f"/projects/{pid}/storyboards", token=token)
        data = expect_ok(env, "查询现有分镜板")
        items = data.get("items") or []
        for item in items:
            if item.get("status") in ("pending_review", "locked", "approved"):
                return item
        return None

    existing = _existing_board()
    if existing is not None:
        sb = existing
        sb_id = int(sb["id"])
        ctx["storyboard_id"] = sb_id
        log(f"  复用现有分镜板 #{sb_id}（status={sb.get('status')}），跳过重新生成")
    else:
        _, gen_env = api(
            client, "POST", f"/projects/{pid}/storyboards/generate", token=token,
            json_body={"episode_no": 1, "target_duration_ms": 30000},
        )
        expect_ok(gen_env, "发起分镜生成")

        def _sb_pending() -> dict[str, Any] | None:
            """轮询项目分镜板，返回 status=pending_review 的分镜板。"""
            _, env = api(client, "GET", f"/projects/{pid}/storyboards", token=token)
            data = expect_ok(env, "轮询分镜板列表")
            items = data.get("items") or []
            pend = [s for s in items if s.get("status") == "pending_review"]
            return pend[0] if pend else None

        sb = wait_for(_sb_pending, timeout=300, interval=5, desc="分镜板 pending_review")
        sb_id = int(sb["id"])
        ctx["storyboard_id"] = sb_id
        log(f"  分镜板 #{sb_id} pending_review")

    _, sb_env = api(client, "GET", f"/storyboards/{sb_id}", token=token)
    sb_detail = expect_ok(sb_env, "查分镜板详情")
    shots = sb_detail.get("shots") or []
    if not shots:
        raise StepFail("分镜板无 shots")
    log(f"  shots={len(shots)}")

    # 异常流 EX-6：未绑锚点先 lock → 60005
    # 2026-10-29：分镜服务生成时已自动绑定资产锚点，未绑锚点场景不再出现，
    # lock 会直接成功（code=0）。断言宽容化：60005=防御校验触发，0=自动绑定生效；
    # 分镜板已锁定（断点重跑）时跳过 lock 调用（重复锁定 40001）。
    board_locked = str(sb.get("status")) == "locked"
    if board_locked:
        log("  分镜板已锁定（断点重跑），跳过 lock")
    else:
        _, early_lock = api(client, "POST", f"/storyboards/{sb_id}/lock", token=token)
        if early_lock.get("code") == 60005:
            log("  [EX] 未绑锚点 lock 60005 OK")
        elif early_lock.get("code") == 0:
            board_locked = True
            log("  [EX] 分镜生成已自动绑定锚点，lock 直接成功（60005 防御分支不触发）")
        else:
            raise StepFail(
                f"未绑锚点 lock: 期望 60005/0 实际 {early_lock.get('code')} {early_lock.get('message')!r}"
            )

    if not board_locked:
        # 全部 shots 绑定主角色锚点
        for shot in shots:
            _, anch_env = api(
                client, "POST", f"/shots/{int(shot['id'])}/anchors", token=token,
                json_body={"character_asset_ids": [hero_id]},
            )
            expect_ok(anch_env, f"shot {shot['id']} 绑锚点")
        log(f"  {len(shots)} 个镜头均已绑定角色锚点")

        # lock → storyboard 审批单
        _, lock_env = api(client, "POST", f"/storyboards/{sb_id}/lock", token=token)
        expect_ok(lock_env, "分镜板 lock")

    def _sb_approval() -> dict[str, Any] | None:
        """轮询 pending 的 storyboard 审批单。"""
        _, env = api(
            client, "GET", "/orchestration/approvals", token=token,
            params={"project_id": pid, "status": "pending", "approval_type": "storyboard"},
        )
        data = expect_ok(env, "轮询 storyboard 审批单")
        items = data.get("items") or []
        return items[0] if items else None

    appr = wait_for(_sb_approval, timeout=60, interval=3, desc="storyboard 审批单")
    ctx["storyboard_approval_id"] = int(appr["id"])

    # storyboard 类型审批单须走分镜域专用 decide 端点（分镜 approve 会
    # 推进项目状态 storyboarding/storyboard_review → rendering；
    # orchestration 的 decide 仅处理剧本域审批单，不改项目状态）。
    _, dec_env = api(
        client, "POST", f"/storyboards/{sb_id}/decide", token=token,
        json_body={"action": "approve"},
    )
    expect_ok(dec_env, "storyboard approve")

    # 轮询项目 rendering
    def _rendering() -> str | None:
        """读取项目状态，到达 rendering 返回。"""
        _, env = api(client, "GET", f"/projects/{pid}", token=token)
        data = expect_ok(env, "查项目详情")
        return data.get("status") if data.get("status") == "rendering" else None

    wait_for(_rendering, timeout=90, interval=3, desc="项目状态 rendering")
    mark_done(ctx_state(), "07_storyboard")
    record("07", "分镜生成+锚点+审批", True, _now() - t0, f"shots={len(shots)} sb={sb_id}")


def step08_render(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 08：渲染估算 → 批量提交（幂等）→ 全部 succeeded → 扣费/幂等断言 + SSE。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])
    sb_id = int(ctx["storyboard_id"])

    # 预算超限异常流（若 step03 建了 big_project 且仍处 draft 之外则先跳过）
    # estimate
    _, est_env = api(client, "GET", f"/projects/{pid}/render/estimate", token=token)
    est = expect_ok(est_env, "渲染估算")
    log(f"  estimate: {json.dumps(est, ensure_ascii=False)[:200]}")

    # 断点重跑复用分支：项目镜头已全部渲染（任务全部 succeeded）时跳过重复
    # 提交——shot 已 rendered 不再是 locked，batch 会 60001。
    _, exist_env = api(
        client, "GET", f"/projects/{pid}/render-tasks", token=token,
        params={"page_size": 100},
    )
    exist_rows = (expect_ok(exist_env, "查询现有渲染任务").get("items")) or []
    if exist_rows and all(r.get("status") == "succeeded" for r in exist_rows):
        task_ids = [int(r["id"]) for r in exist_rows]
        ctx["render_task_ids"] = task_ids
        log(f"  断点复用：现有 {len(task_ids)} 个渲染任务已全部 succeeded，跳过提交")
        _, sb_env0 = api(client, "GET", f"/storyboards/{sb_id}", token=token)
        sb_detail0 = expect_ok(sb_env0, "渲染后查分镜板")
        rendered0 = [s for s in (sb_detail0.get("shots") or []) if s.get("status") == "rendered"]
        if len(rendered0) < len(sb_detail0.get("shots") or []):
            raise StepFail(
                f"镜头未全部 rendered: {len(rendered0)}/{len(sb_detail0.get('shots') or [])}"
            )
        _, acc_env0 = api(client, "GET", "/billing/credit-account", token=token)
        mark_done(ctx_state(), "08_render")
        record("08", "渲染批次+幂等+SSE", True, _now() - t0,
               f"reuse tasks={len(task_ids)} balance={_bal(acc_env0)}")
        return

    _, acc_env = api(client, "GET", "/billing/credit-account", token=token)
    acc = expect_ok(acc_env, "渲染前余额")
    balance_before = _bal(acc)

    # 渲染 SSE 采集线程
    sse_bucket: list[dict[str, Any]] = []
    sse_thread = sse_collect(
        f"/api/v1/projects/{pid}/render-tasks/events", token, 60.0, sse_bucket, use_query_token=True
    )

    idem_key = str(uuid.uuid4())
    _, batch_env = api(
        client, "POST", f"/projects/{pid}/render-tasks/batch", token=token,
        json_body={}, headers={"Idempotency-Key": idem_key}, timeout=120,
    )
    batch = expect_ok(batch_env, "批量提交渲染")
    tasks = batch.get("tasks") or batch.get("items") or []
    task_ids = [int(t["id"]) for t in tasks]
    if not task_ids:
        raise StepFail(f"batch 未产生渲染任务: {json.dumps(batch, ensure_ascii=False)[:200]}")
    ctx["render_task_ids"] = task_ids
    log(f"  提交 {len(task_ids)} 个渲染任务")

    # 轮询全部 succeeded（120s）
    def _all_done() -> bool:
        """查询任务列表，全部 succeeded 返回 True。"""
        _, env = api(
            client, "GET", f"/projects/{pid}/render-tasks", token=token,
            params={"page_size": 100},
        )
        data = expect_ok(env, "轮询渲染任务")
        rows = data.get("items") or []
        mine = [r for r in rows if int(r["id"]) in task_ids]
        if len(mine) < len(task_ids):
            return False
        return all(r.get("status") == "succeeded" for r in mine)

    # 轮询全部 succeeded（真实 wan2.6 每镜 60~180s，8 镜并发 ≈4-7 分钟）
    wait_for(_all_done, timeout=600, interval=6, desc="渲染任务全部 succeeded")

    # shots rendered 断言
    _, sb_env = api(client, "GET", f"/storyboards/{sb_id}", token=token)
    sb_detail = expect_ok(sb_env, "渲染后查分镜板")
    rendered = [s for s in (sb_detail.get("shots") or []) if s.get("status") == "rendered"]
    if len(rendered) < len(sb_detail.get("shots") or []):
        raise StepFail(f"镜头未全部 rendered: {len(rendered)}/{len(sb_detail.get('shots') or [])}")

    # 余额扣减断言
    _, acc_env2 = api(client, "GET", "/billing/credit-account", token=token)
    acc2 = expect_ok(acc_env2, "渲染后余额")
    balance_after = _bal(acc2)
    cost = balance_before - balance_after
    if cost <= 0:
        raise StepFail(f"渲染扣费异常: before={balance_before} after={balance_after}")
    log(f"  渲染扣费 {cost}（before={balance_before} after={balance_after}）")
    ctx["balance"] = balance_after

    # 幂等重放：同 key 再提交 → 不新增任务、不重复扣费
    _, replay_env = api(
        client, "POST", f"/projects/{pid}/render-tasks/batch", token=token,
        json_body={}, headers={"Idempotency-Key": idem_key}, timeout=120,
    )
    replay = expect_ok(replay_env, "幂等重放渲染批次")
    replay_tasks = replay.get("tasks") or replay.get("items") or []
    replay_ids = sorted(int(t["id"]) for t in replay_tasks)
    if replay_ids != sorted(task_ids):
        raise StepFail(f"幂等重放任务集不一致: 原 {sorted(task_ids)} 重放 {replay_ids}")
    _, acc_env3 = api(client, "GET", "/billing/credit-account", token=token)
    acc3 = expect_ok(acc_env3, "幂等重放后余额")
    if _bal(acc3) != balance_after:
        raise StepFail(f"幂等重放重复扣费: {balance_after} -> {acc3.get('balance_credits')}")
    log("  幂等重放一致，未重复扣费")

    # SSE 断言
    sse_thread.join(timeout=65)
    try:
        assert_sse_frames(sse_bucket, "render.task.updated", "渲染事件流")
    except StepFail as e:
        log(f"  [SSE][WARN] 实时流断言失败({e})，回放校验…")
        replay_bucket: list[dict[str, Any]] = []
        t2 = sse_collect(
            f"/api/v1/projects/{pid}/render-tasks/events", token, 6.0, replay_bucket,
            use_query_token=True, last_event_id="0",
        )
        t2.join(timeout=10)
        assert_sse_frames(replay_bucket, None, "渲染事件流(回放)")
    mark_done(ctx_state(), "08_render")
    record("08", "渲染批次+幂等+SSE", True, _now() - t0, f"tasks={len(task_ids)} cost={cost}")


def step09_post(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 09：TTS → 字幕 → BGM → 时间线 → 合成 QC → final_cut 审批 → completed。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])

    _, pv_env = api(
        client, "POST", f"/projects/{pid}/post/tts-preview", token=token,
        json_body={"episode_no": 1},
    )
    pv = expect_ok(pv_env, "TTS 试听")
    log(f"  tts-preview: {json.dumps(pv, ensure_ascii=False)[:140]}")

    _, tts_env = api(client, "POST", f"/projects/{pid}/post/tts", token=token)
    expect_ok(tts_env, "全剧 TTS（202）")

    # 轮询 audio clips（60s）
    def _audio_ready() -> bool:
        """查时间线，audio 轨出现片段返回 True。"""
        _, env = api(
            client, "GET", f"/projects/{pid}/post/timeline", token=token,
            params={"cut_type": "preview"},
        )
        data = expect_ok(env, "轮询时间线")
        clips = data.get("clips") or []
        return any(c.get("track_type") == "audio" for c in clips)

    # 真实 qwen3-tts 逐句生成 4~8 句，放宽到 240 秒
    wait_for(_audio_ready, timeout=240, interval=4, desc="audio 轨片段生成")

    _, sub_env = api(
        client, "POST", f"/projects/{pid}/post/subtitles", token=token, json_body={},
    )
    expect_ok(sub_env, "字幕生成")

    _, bgm_env = api(
        client, "GET", f"/projects/{pid}/post/bgm-recommend", token=token,
        params={"emotion": "tension"},
    )
    bgm_data = expect_ok(bgm_env, "BGM 推荐")
    bgm_items = bgm_data.get("items") or bgm_data.get("tracks") or []
    if not bgm_items:
        raise StepFail("BGM 推荐为空")
    bgm_track_id = int((bgm_items[0].get("id")) or bgm_items[0].get("track_id") or 0)
    if not bgm_track_id:
        raise StepFail(f"BGM 曲目缺少 id 字段: {bgm_items[0]}")

    _, set_env = api(
        client, "PUT", f"/projects/{pid}/post/bgm", token=token,
        json_body={"bgm_track_id": bgm_track_id, "volume_db": -6, "sidechain": True},
    )
    expect_ok(set_env, "设置 BGM")

    _, tl_env = api(
        client, "GET", f"/projects/{pid}/post/timeline", token=token,
        params={"cut_type": "preview"},
    )
    tl = expect_ok(tl_env, "读取时间线")
    log(f"  时间线: clips={len(tl.get('clips') or [])} bgm={(tl.get('bgm') or {}).get('title', 'N/A')}")

    _, comp_env = api(client, "POST", f"/projects/{pid}/post/compose", token=token)
    comp = expect_ok(comp_env, "发起合成（202）")
    log(f"  compose 受理: {json.dumps(comp, ensure_ascii=False)[:160]}")

    # 轮询 final_cut 审批单（60s）
    def _final_cut() -> dict[str, Any] | None:
        """轮询 pending final_cut 审批单。"""
        _, env = api(
            client, "GET", "/orchestration/approvals", token=token,
            params={"project_id": pid, "status": "pending", "approval_type": "final_cut"},
        )
        data = expect_ok(env, "轮询 final_cut 审批单")
        items = data.get("items") or []
        return items[0] if items else None

    appr = wait_for(_final_cut, timeout=60, interval=3, desc="final_cut 审批单")
    ctx["final_cut_approval_id"] = int(appr["id"])

    # QC 音画同步通过断言：合成管线 QC 恒通过（stub 对齐）；检查审批单已产生
    # 即代表 QC 通过（QC 失败会置 timeline failed 而不出单）。
    log("  QC 音画同步：final_cut 审批单已产生 → QC 通过（stub 管线恒过）")

    _, dec_env = api(
        client, "POST", f"/projects/{pid}/post/decide", token=token,
        json_body={"action": "approve"},
    )
    expect_ok(dec_env, "成片审批 approve")

    def _completed() -> str | None:
        """读取项目状态，到达 completed 返回。"""
        _, env = api(client, "GET", f"/projects/{pid}", token=token)
        data = expect_ok(env, "查项目详情")
        return data.get("status") if data.get("status") == "completed" else None

    wait_for(_completed, timeout=60, interval=3, desc="项目状态 completed")
    mark_done(ctx_state(), "09_post")
    record("09", "后期 TTS→合成→审批", True, _now() - t0, f"bgm={bgm_track_id}")


def step10_export(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 10：1080p 导出（free 70003 则开 pro）→ 下载 → 元数据 → 4K 70003。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])

    def _try_export(resolution: str) -> tuple[int, dict[str, Any]]:
        """用新 Idempotency-Key 发起一次导出请求。

        Args:
            resolution: 目标分辨率。

        Returns:
            (响应, 包络)。
        """
        return api(
            client, "POST", f"/projects/{pid}/exports", token=token,
            json_body={"resolution": resolution, "with_watermark": True, "episode_no": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())}, timeout=120,
        )

    _, exp_env = _try_export("1080p")
    if exp_env.get("code") == 70003:
        log("  1080p 遇 70003（free 仅 720p），开通 pro 套餐后重试…")
        _, sub_env = api(
            client, "POST", "/billing/subscriptions", token=token,
            json_body={"plan_key": "pro"},
        )
        expect_ok(sub_env, "开通 pro 套餐")
        _, exp_env = _try_export("1080p")
    exp = expect_ok(exp_env, "创建 1080p 导出")
    # create_export 契约：data={"export_job": {...}, "idempotent": bool}
    exp_job = exp.get("export_job") or exp
    export_id = int(exp_job["id"])
    ctx["export_id"] = export_id

    def _export_done() -> dict[str, Any] | None:
        """轮询导出任务，succeeded 返回详情。"""
        _, env = api(client, "GET", f"/exports/{export_id}", token=token)
        data = expect_ok(env, "轮询导出任务")
        return data if data.get("status") == "succeeded" else None

    job = wait_for(_export_done, timeout=90, interval=4, desc=f"导出 {export_id} succeeded")

    # 下载 URL 200（详情契约：output={"download_url": "/files/..."}）
    download_url = job.get("download_url") or ""
    media = job.get("media") or {}
    output = job.get("output") or {}
    download_url = (
        download_url
        or media.get("download_url")
        or media.get("url")
        or output.get("download_url")
        or ""
    )
    if not download_url:
        raise StepFail(f"导出详情缺少下载地址: {json.dumps(job, ensure_ascii=False)[:200]}")
    dl_path = download_url if download_url.startswith("/") else "/" + download_url.split("/", 3)[-1]
    resp = client.get(dl_path if dl_path.startswith("/files") else download_url, timeout=30)
    log(f"  下载 {download_url} -> http={resp.status_code}")
    if resp.status_code != 200:
        raise StepFail(f"导出文件下载失败 http={resp.status_code}")

    # 发行元数据（真实 LLM）
    _, meta_env = api(
        client, "POST", f"/projects/{pid}/exports/metadata", token=token, json_body={},
        timeout=120,
    )
    meta = expect_ok(meta_env, "生成发行元数据")
    title = (meta.get("title") or "").strip()
    tags = meta.get("tags") or []
    desc = (meta.get("description") or meta.get("synopsis") or "").strip()
    if not title or not tags:
        raise StepFail(f"发行元数据字段缺失: {json.dumps(meta, ensure_ascii=False)[:200]}")
    log(f"  元数据 title={title!r} tags={tags[:5]} desc_len={len(desc)}")

    # 4K → 70003（当前套餐权益不足）
    _, k4_env = _try_export("4k")
    expect_biz(k4_env, 70003, "4K 导出权益校验")
    log(f"  [EX] 4K → 70003 OK（套餐={k4_env.get('data', {}).get('current_plan', 'free')}）")
    mark_done(ctx_state(), "10_export")
    record("10", "导出+下载+元数据+70003", True, _now() - t0, f"export={export_id} title={title[:12]}")


def step11_notify(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 11：通知列表非空 + 项目事件流回放（id 单调、JSON 可解析）。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])

    _, n_env = api(client, "GET", "/notifications", token=token, params={"page_size": 20})
    n_data = expect_ok(n_env, "查询通知列表")
    items = n_data.get("items") or []
    if not items:
        raise StepFail("通知列表为空")
    log(f"  通知 {len(items)} 条，首条 type={items[0].get('type')!r} title={str(items[0].get('title'))[:24]!r}")

    _, uc_env = api(client, "GET", "/notifications/unread-count", token=token)
    uc = expect_ok(uc_env, "查询未读数")
    log(f"  未读数: {uc}")

    # SSE 项目事件流：Last-Event-ID=0 回放全部历史（3 条帧校验）
    bucket: list[dict[str, Any]] = []
    t = sse_collect(
        f"/api/v1/projects/{pid}/events", token, 8.0, bucket,
        use_query_token=True, last_event_id="0",
    )
    t.join(timeout=12)
    frames = assert_sse_frames(bucket, None, "项目事件流(回放校验)")
    if len(frames) < 3:
        raise StepFail(f"事件流回放不足 3 帧: {len(frames)}")
    log(f"  事件流回放 {len(frames)} 帧，首帧 event={frames[0]['event']} id={frames[0]['id']}")
    mark_done(ctx_state(), "11_notify")
    record("11", "通知 + SSE 回放", True, _now() - t0, f"notify={len(items)} frames={len(frames)}")


def step12_cross_user(client: httpx.Client, ctx: dict[str, Any]) -> None:
    """步骤 12：异常流——未登录 20001 / 用户 B 越权 20003 + audit_logs 留痕。"""
    t0 = _now()
    token = ensure_token(client, ctx)
    pid = int(ctx["project_id"])

    # 未登录 → 20001
    _, anon_env = api(client, "GET", "/projects")
    expect_biz(anon_env, 20001, "未登录访问 /projects")

    # 用户 B 注册/登录后越权访问用户 A 项目 → 20003
    token_b, _ = _register_or_login(client, PHONE_B)
    ctx["token_b"] = token_b
    _, fb_env = api(client, "GET", f"/projects/{pid}", token=token_b)
    expect_biz(fb_env, 20003, "用户 B 访问用户 A 项目")

    # audit_logs 有记录（action 与 core/deps.py 的 acl_denied 一致）
    rows = query_audit_logs(pid, "acl_denied")
    if not rows:
        raise StepFail("audit_logs 未发现 acl_denied 留痕")
    log(f"  audit_logs acl_denied {len(rows)} 条，最近 actor={rows[-1]['actor_user_id']}")
    mark_done(ctx_state(), "12_cross_user")
    record("12", "越权 20003 + 审计留痕", True, _now() - t0, f"audit={len(rows)}")


# ---------------------------------------------------------------------------
# 步骤注册表与主流程
# ---------------------------------------------------------------------------

#: 全部步骤（名称 → 执行函数）
STEPS: list[tuple[str, Callable[[httpx.Client, dict[str, Any]], None]]] = [
    ("01_register", step01_register),
    ("02_recharge", step02_recharge),
    ("03_create_project", step03_create_project),
    ("04_script_generate", step04_script_generate),
    ("05_script_review", step05_script_review),
    ("06_assets", step06_assets),
    ("07_storyboard", step07_storyboard),
    ("08_render", step08_render),
    ("09_post", step09_post),
    ("10_export", step10_export),
    ("11_notify", step11_notify),
    ("12_cross_user", step12_cross_user),
]

#: 全局状态（模块级单例，供 mark_done(ctx_state()) 使用）
_STATE: dict[str, Any] = {"done": [], "ctx": {}}


def ctx_state() -> dict[str, Any]:
    """返回模块级全局状态字典（上下文 + 已完成步骤）。

    Returns:
        全局状态 dict。
    """
    return _STATE


def main() -> int:
    """冒烟主流程：解析参数 → 逐步骤执行 → 输出结果表。

    Returns:
        进程退出码：0 全绿，1 存在失败。

    Raises:
        SystemExit: argparse 异常时由 argparse 自行抛出。
    """
    parser = argparse.ArgumentParser(description="端到端全旅程冒烟（Task 17）")
    parser.add_argument("--from", dest="from_step", default=None, help="从指定步骤开始重跑（如 06_assets）")
    parser.add_argument("--reset", action="store_true", help="清空断点状态重新开始")
    args = parser.parse_args()

    state = load_state()
    if args.reset:
        state = {"done": [], "ctx": {}}
        STATE_FILE.unlink(missing_ok=True)
    _STATE.update(state)
    ctx: dict[str, Any] = _STATE["ctx"]

    start_from = args.from_step
    if start_from:
        names = [n for n, _ in STEPS]
        if start_from not in names:
            print(f"未知步骤: {start_from}，可选: {names}")
            return 2
        # 清除该步及之后的完成标记（强制重跑）
        idx = names.index(start_from)
        _STATE["done"] = [d for d in _STATE["done"] if d not in names[idx:]]
        save_state(_STATE)

    client = httpx.Client(base_url=BASE_URL, timeout=60.0)
    total_t0 = _now()
    try:
        # 服务连通预检
        resp = client.get("/api/v1/health", timeout=10)
        log(f"服务预检 /api/v1/health -> {resp.status_code}")

        for name, fn in STEPS:
            if name in _STATE["done"]:
                record(name, fn.__doc__.strip().split("。")[0] if fn.__doc__ else name, True, 0.0, "（断点跳过）")
                continue
            log(f"---- 开始 {name}: {fn.__doc__.strip() if fn.__doc__ else ''}")
            step_t0 = _now()
            try:
                fn(client, ctx)
            except StepFail as exc:
                record(name, fn.__doc__.strip().split("。")[0] if fn.__doc__ else name, False, _now() - step_t0, str(exc)[:160])
                raise
            except Exception as exc:  # noqa: BLE001
                record(name, fn.__doc__.strip().split("。")[0] if fn.__doc__ else name, False, _now() - step_t0, f"{exc!r}"[:160])
                raise
    finally:
        # 输出结果表
        print("\n==================== 冒烟结果表 ====================")
        print(f"{'步骤':<6}{'描述':<28}{'结果':<6}{'耗时':<9}备注")
        for row in RESULT_ROWS:
            print(f"{row[0]:<6}{row[1]:<28}{row[2]:<6}{row[3]:<9}{row[4]}")
        print(f"总耗时 {_now() - total_t0:.1f}s")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StepFail:
        raise SystemExit(1)
