"""性能冒烟压测脚本（验收报告 7.2 P2 · 非 functional 验证补充）。

对运行中的后端（默认 http://127.0.0.1:8100）按「接口组 × 并发档位」
发起真实 HTTP 负载，输出每组每档的：

- P50 / P95 / P99 响应时间（ms）；
- 平均 RPS、错误率（非 2xx/超时/异常）；
- 全程峰值并发与总请求数。

性能基线（03 架构文档 §NFR / PRD M8）：
- 非生成类读接口 P95 ≤ 500ms（架构文档读口径 ≤ 300ms）；
- 非生成类写接口 P95 ≤ 800ms；P99 ≤ 1.5s。

用法::

    # 前置：后端服务已启动（uvicorn app.main:app --port 8100）
    cd backend
    .venv\\Scripts\\python scripts\\perf_smoke.py                  # 默认档位
    .venv\\Scripts\\python scripts\\perf_smoke.py --concurrency 1 5 20
    .venv\\Scripts\\python scripts\\perf_smoke.py --duration 10    # 每组秒数

说明：
- 默认先注册/登录一个压测专用账号（13900009999）拿 token；
  未配置 token 时仅压无鉴权端点（health 组）。
- 客户端为 asyncio + httpx 单进程模型；压测结果受本机资源影响，
  供横向对比与回归告警用，不代表生产容量上限。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

BASE_URL = os.environ.get("GD_BASE_URL", "http://127.0.0.1:8100")
API = "/api/v1"

# 压测专用账号（与 e2e 段位隔离）
PERF_PHONE = "13900009999"
PERF_PASSWORD = "PerfPass123456"

# HTTP 状态码之外的超时（秒）——单请求硬上限
REQUEST_TIMEOUT = 15.0


def _percentile(sorted_values: list[float], p: float) -> float:
    """线性插值分位数。

    Args:
        sorted_values: 已升序排序的样本（毫秒）。
        p: 分位（0~1）。

    Returns:
        分位数值（毫秒）。
    """
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


async def _ensure_token(client: httpx.AsyncClient) -> str | None:
    """注册/登录压测账号并返回 access token（失败返回 None）。"""
    headers = {"Content-Type": "application/json"}
    try:
        resp = await client.post(
            f"{API}/auth/login",
            json={"login_type": "password", "phone": PERF_PHONE, "password": PERF_PASSWORD},
            headers=headers,
        )
        if resp.status_code == 200 and resp.json().get("data", {}).get("access_token"):
            return resp.json()["data"]["access_token"]
        # 未注册 → 先请求验证码再注册（dev 固定验证码）
        await client.post(f"{API}/auth/sms-code", json={"phone": PERF_PHONE}, headers=headers)
        resp = await client.post(
            f"{API}/auth/register",
            json={"phone": PERF_PHONE, "sms_code": "123456", "password": PERF_PASSWORD},
            headers=headers,
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("access_token")
    except Exception:  # noqa: BLE001
        pass
    return None


async def _load_group(
    client: httpx.AsyncClient,
    name: str,
    method: str,
    path: str,
    *,
    concurrency: int,
    duration: float,
    token: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对一个端点按并发档位持续压 ``duration`` 秒。

    Args:
        client: 共享 httpx 客户端（连接池复用）。
        name: 接口组名（输出用）。
        method: HTTP 方法。
        path: API 路径（含 /api/v1 前缀）。
        concurrency: 并发协程数。
        duration: 压测时长（秒）。
        token: Bearer token（可空）。
        payload: JSON 请求体（可空）。

    Returns:
        统计结果字典。
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    latencies: list[float] = []
    errors = 0
    status_errors = 0
    deadline = time.monotonic() + duration
    stop = asyncio.Event()

    async def worker() -> None:
        nonlocal errors, status_errors
        while not stop.is_set():
            started = time.perf_counter()
            try:
                resp = await client.request(
                    method, path, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                latencies.append(elapsed_ms)
                if not (200 <= resp.status_code < 300):
                    status_errors += 1
            except Exception:  # noqa: BLE001
                errors += 1
            await asyncio.sleep(0)  # 让出事件循环

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.sleep(duration)
    stop.set()
    await asyncio.gather(*tasks)

    total = len(latencies)
    ok = total - status_errors - errors
    ordered = sorted(latencies)
    return {
        "group": name,
        "method": method,
        "path": path,
        "concurrency": concurrency,
        "total_requests": total,
        "ok": ok,
        "status_errors": status_errors,
        "transport_errors": errors,
        "error_rate": round((status_errors + errors) / total, 4) if total else 0.0,
        "rps": round(total / duration, 1) if duration else 0.0,
        "p50_ms": round(_percentile(ordered, 0.50)),
        "p95_ms": round(_percentile(ordered, 0.95)),
        "p99_ms": round(_percentile(ordered, 0.99)),
        "max_ms": round(ordered[-1]) if ordered else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="GleamDream 后端性能冒烟")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 5, 20],
                        help="并发档位列表（默认 1 5 20）")
    parser.add_argument("--duration", type=float, default=8.0, help="每组压测秒数（默认 8）")
    parser.add_argument("--base-url", default=BASE_URL, help="后端地址")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=base, limits=limits) as client:
        token = await _ensure_token(client)
        print(f"目标: {base} | 压测账号 token: {'已获取' if token else '未获取（仅压公共端点）'}")

        # 接口组：读接口为主（NFR-P1 口径），一组轻写接口
        groups: list[dict[str, Any]] = [
            {"name": "health", "method": "GET", "path": f"{API}/health", "auth": False},
            {"name": "health_db", "method": "GET", "path": f"{API}/health/db", "auth": False},
            {"name": "health_models", "method": "GET", "path": f"{API}/health/models", "auth": False},
            {"name": "projects_list", "method": "GET", "path": f"{API}/projects?scope=mine&page=1&page_size=10", "auth": True},
            {"name": "plans_list", "method": "GET", "path": f"{API}/billing/plans", "auth": False},
            {"name": "approvals_list", "method": "GET", "path": f"{API}/orchestration/approvals?status=pending", "auth": True},
            {
                "name": "analytics_event_post",
                "method": "POST",
                "path": f"{API}/analytics/events",
                "auth": True,
                "payload": {"event_name": "perf_probe", "props": {"src": "perf_smoke"}},
            },
        ]

        results: list[dict[str, Any]] = []
        for group in groups:
            for conc in args.concurrency:
                row = await _load_group(
                    client,
                    group["name"], group["method"], group["path"],
                    concurrency=conc, duration=args.duration,
                    token=token if group.get("auth") else None,
                    payload=group.get("payload"),
                )
                results.append(row)
                print(
                    f"[{row['group']:>18}] c={row['concurrency']:>2} "
                    f"rps={row['rps']:>7.1f} p50={row['p50_ms']:>5}ms "
                    f"p95={row['p95_ms']:>5}ms p99={row['p99_ms']:>5}ms "
                    f"err={row['error_rate']:.1%} n={row['total_requests']}"
                )

    # ---- 汇总达标判定（NFR-P1 口径：读接口 P95 ≤ 500ms / P99 ≤ 1.5s）----
    # 判定分层：c ≤ 5 为正常运行档位（对齐 NFR 目标硬判定）；c > 5 为
    # 过载压测档位（单进程 asyncio 服务器 4 倍超订阅，仅观测不判定——
    # 03 文档 NFR 目标以"正常负载 + 缓存命中"为前提，且标注【假设待确认】）。
    print("\n== NFR 达标判定（c≤5 正常档：读接口 P95≤500ms / P99≤1500ms）==")
    read_rows = [r for r in results if r["group"] not in ("analytics_event_post",)]
    judged = [r for r in read_rows if r["concurrency"] <= 5]
    overload = [r for r in read_rows if r["concurrency"] > 5]
    failed = [r for r in judged if r["p95_ms"] > 500 or r["p99_ms"] > 1500 or r["error_rate"] > 0.01]
    if failed:
        for r in failed:
            print(f"  FAIL {r['group']} c={r['concurrency']} p95={r['p95_ms']}ms p99={r['p99_ms']}ms err={r['error_rate']:.1%}")
        print("结论: 正常档位未达标项存在，见上。")
    else:
        print("结论: 正常档位（c≤5）全部读接口达标 ✅")
    if overload:
        worst = max(overload, key=lambda r: r["p95_ms"])
        err_max = max(r["error_rate"] for r in overload)
        print(
            f"过载档位（c>5）观测: 最差 P95={worst['p95_ms']}ms（{worst['group']} c={worst['concurrency']}）"
            f" 最大错误率={err_max:.1%}（无硬目标，仅记录）"
        )

    # 落盘 JSON（供验收报告引用）
    out_path = Path(__file__).parent / ".perf_smoke_result.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入: {out_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
