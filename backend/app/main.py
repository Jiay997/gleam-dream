"""GleamDream Studio 后端应用入口。

组装内容：
- lifespan：启动时输出配置摘要（敏感信息脱敏）；
- 纯 ASGI 请求 ID 中间件：接收/生成 X-Request-ID（ULID），注入
  request.state、日志上下文与响应头（不用 BaseHTTPMiddleware，
  规避 contextvars 向下游路由任务不传播的问题）；
- CORS 全开（开发环境）；
- 全局异常处理器（统一包络）；
- /api/v1 业务路由聚合；
- /files 静态文件挂载（本地存储目录，模拟 OSS 下载）；
- 进程启动即启用 faulthandler，原生崩溃栈落盘 logs/faulthandler.log（P0-1）。
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import IO, Any, AsyncIterator
from urllib.parse import parse_qs

# ---- Windows 事件循环策略（必须在任何异步库导入前设置）----
# psycopg3 的异步连接池（LangGraph checkpointer 依赖）不支持 Windows 默认的
# ProactorEventLoop，需切换为 SelectorEventLoopPolicy 才能正常工作。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ---- 崩溃取证：faulthandler（P0-1）----
# 现象：uvicorn 进程在剧本审批中途静默退出，控制台无 traceback、Windows
# 事件查看器也无记录，无法定位死因。原因：进程级致命错误（段错误、栈溢出、
# 原生库 abort 等）不走 Python 异常链，普通日志/异常处理器完全捕获不到。
# 修复：进程启动即启用 faulthandler，把原生崩溃时的全线程 Python 栈写入
# logs/faulthandler.log（同时保持句柄常驻，避免被 GC 关闭）。
_fault_log_handle: IO[str] | None = None
try:
    _fault_log_dir = Path(__file__).resolve().parent.parent / "logs"
    _fault_log_dir.mkdir(parents=True, exist_ok=True)
    _fault_log_handle = open(  # noqa: SIM115 需常驻句柄供 faulthandler 写入
        _fault_log_dir / "faulthandler.log", "a", encoding="utf-8"
    )
    faulthandler.enable(file=_fault_log_handle, all_threads=True)
except Exception:  # noqa: BLE001 取证失败不得阻断服务启动
    faulthandler.enable(all_threads=True)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import Message

from app import __version__
from app.api import api_router
from app.core.config import settings
from app.core.db import async_engine
from app.core.errors import BizError, ErrorCode, error_envelope, register_exception_handlers
from app.core.ids import new_request_id
from app.core.logging import reset_request_id, set_request_id, setup_logging
from app.core.security import decode_token
from app.infra.storage import storage

logger = logging.getLogger("gleamdream.main")

#: ASGI 调用签名别名
ASGIApp = Callable[[dict[str, Any], Any, Any], Awaitable[None]]

# OpenAPI tag 元数据（/docs 分组展示与导出的 openapi.json 一并生效）
OPENAPI_TAG_METADATA: list[dict[str, str]] = [
    {"name": "health", "description": "健康探针：服务存活 / 数据库 / 模型提供商配置"},
    {"name": "auth", "description": "认证域：注册 / 登录 / 令牌刷新 / 微信 Stub"},
    {"name": "users", "description": "用户域：个人资料"},
    {"name": "billing", "description": "计费域：套餐 / 积分账户 / 流水 / 订单 / 支付回调"},
    {"name": "projects", "description": "项目域：CRUD / 状态机 / 快照回滚 / 成员 ACL / 事件流"},
    {"name": "teams", "description": "团队域：成员管理"},
    {"name": "orchestration", "description": "编排域：剧本生成触发 / HITL-1 审批决策 / 审批单查询"},
    {"name": "script", "description": "剧本域：上传解析 / 结构 / 局部改写 / 合规送审"},
    {"name": "assets", "description": "资产域：清单 / 生成 / HITL-2 审批 / 锁定锚点 / 音色"},
    {"name": "storyboards", "description": "分镜域：拆镜 / 画布 / HITL-3 决策 / 草稿预览"},
    {"name": "render", "description": "生成执行域：批量渲染 / 取消重试 / 渲染事件流"},
    {"name": "post", "description": "后期域：TTS / 字幕 / BGM / 四轨时间线 / HITL-4 成片审批"},
    {"name": "export", "description": "导出分发域：单集/批量导出 / 元数据 / 平台授权与分发"},
    {"name": "notifications", "description": "通知域：未读徽标 / 已读"},
    {"name": "analytics", "description": "埋点域：客户端事件上报 / 管理端漏斗摘要（X-Admin-Token）"},
    {"name": "model-configs", "description": "模型配置域：四类模型接入参数维护 / 连通性测试 / 默认路由绑定（即时生效）"},
]


class RequestIDMiddleware:
    """纯 ASGI 请求 ID 中间件。

    从请求头读取 X-Request-ID，缺失则生成 ULID；随后：
    1. 写入 scope["state"]（Starlette Request.state 的底层存储）；
    2. 写入日志 contextvars（与下游路由同任务同上下文，必然可见）；
    3. 在响应头中回写 X-Request-ID。

    Attributes:
        app: 下游 ASGI 应用。
    """

    def __init__(self, app: ASGIApp) -> None:
        """保存下游应用引用。

        Args:
            app: 下游 ASGI 应用。
        """
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """处理一次 ASGI 调用。

        Args:
            scope: ASGI 作用域字典。
            receive: ASGI 接收通道。
            send: ASGI 发送通道。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. 提取或生成请求 ID
        request_id = ""
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                request_id = value.decode("latin-1").strip()
                break
        if not request_id:
            request_id = new_request_id()

        # 2. 注入 scope.state（Request.state 底层即 scope["state"]）
        scope.setdefault("state", {})["request_id"] = request_id

        # 3. 注入日志上下文，请求结束复原
        token = set_request_id(request_id)

        async def send_wrapper(message: Message) -> None:
            """发送包装器：在响应起始消息中追加追踪与安全响应头。

            Args:
                message: ASGI 消息。
            """
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                # 基础安全响应头（不引入 CSP，避免破坏现有内联样式与第三方图标）
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"x-frame-options", b"SAMEORIGIN"))
                headers.append((b"referrer-policy", b"no-referrer"))
                headers.append((b"permissions-policy", b"camera=(), microphone=(), geolocation=()"))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)


class FilesAuthMiddleware:
    """静态文件鉴权中间件（可选开启，settings.FILES_REQUIRE_AUTH）。

    开启后 ``/files/*`` 必须携带 ``Authorization: Bearer`` 或
    ``?access_token=``（兼容浏览器 ``<img>``/``<video>`` 直链）。默认关闭
    以兼容既有前端媒体直链，生产环境建议开启并同步改造媒体 URL。

    Attributes:
        app: 下游 ASGI 应用。
    """

    def __init__(self, app: ASGIApp) -> None:
        """保存下游应用引用。

        Args:
            app: 下游 ASGI 应用。
        """
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """校验 /files 请求的访问令牌。

        Args:
            scope: ASGI 作用域。
            receive: ASGI 接收通道。
            send: ASGI 发送通道。
        """
        if (
            scope["type"] != "http"
            or not settings.FILES_REQUIRE_AUTH
            or not scope.get("path", "").startswith("/files/")
        ):
            await self.app(scope, receive, send)
            return

        token = ""
        for key, value in scope.get("headers") or []:
            if key == b"authorization" and value.lower().startswith(b"bearer "):
                token = value[7:].decode("latin-1").strip()
                break
        if not token:
            query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            token = (query.get("access_token") or [""])[0]
        try:
            if not token:
                raise BizError(ErrorCode.UNAUTHORIZED)
            decode_token(token, expected_type="access")
        except BizError as exc:
            body = error_envelope(exc.code, exc.message)
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(payload)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子。

    启动时初始化日志并打印配置摘要（数据库密码脱敏、密钥不打印）；
    退出时打印关停日志。

    Args:
        app: FastAPI 应用实例。

    Yields:
        None（进入服务运行期）。
    """
    setup_logging()
    logger.info(
        "%s v%s 启动 | env=%s | db=%s | storage=%s | sandbox=%s | models=%s,%s",
        settings.APP_NAME,
        __version__,
        settings.ENV,
        settings.database_url_masked,
        storage.root_path,
        settings.SANDBOX_MODE,
        settings.DEEPSEEK_MODEL,
        settings.GLM_MODEL,
    )

    # ---- 启动安全自检：非开发环境对未加固配置发出显式告警 ----
    if settings.ENV != "dev":
        if len(settings.JWT_SECRET) < 32:
            logger.warning("安全告警：JWT_SECRET 长度不足 32 字节，请在 .env 更换强随机密钥")
        if not settings.ADMIN_TOKEN.strip():
            logger.warning("安全告警：ADMIN_TOKEN 未配置，模型配置管理端点将不可用")
        if not settings.PAYMENT_WEBHOOK_SECRET.strip():
            logger.warning("安全告警：PAYMENT_WEBHOOK_SECRET 未配置，真实支付回调将被拒绝")
        if not settings.FILES_REQUIRE_AUTH:
            logger.warning("安全告警：FILES_REQUIRE_AUTH=false，/files 媒体目录可被匿名直链访问")

    # ---- Task 8：渲染任务执行器随应用启动/停止（本方法为允许的 lifespan 修改点）----
    from app.tasks.executor import render_executor

    await render_executor.start()

    # ---- D19 修复：启动时补偿跨进程重启遗留的孤儿审批单（不阻塞启动）----
    from app.services import orchestration_service

    app.state._reconcile_task = asyncio.create_task(orchestration_service.reconcile_orphan_approvals())

    try:
        yield
    finally:
        # 关停顺序：先停执行器，再取消补偿任务，最后释放数据库连接池
        await render_executor.stop()
        reconcile_task: asyncio.Task[Any] | None = getattr(app.state, "_reconcile_task", None)
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconcile_task
        await async_engine.dispose()
        logger.info("%s 已关停", settings.APP_NAME)


app = FastAPI(
    title="GleamDream Studio API",
    version="1.0.0",
    description=(
        "浮光造梦 · 多智能体 AI 短剧创作 SaaS 后端 API\n\n"
        "**统一约定**\n"
        "- 基路径：`/api/v1`；成功包络 `{code:0, message, data, request_id}`；\n"
        "- 认证：`Authorization: Bearer <JWT>`（SSE 端点支持 `?access_token=`）；\n"
        "- 幂等：生成类创建接口支持 `Idempotency-Key` 请求头；\n"
        "- 错误码分段：00 契约 §6（20001 未登录 / 20003 越权 / 3000x 计费 /\n"
        "  4000x 状态 / 5000x 资产 / 6000x 生成 / 7000x 导出 / 8000x 模型）；\n"
        "- 媒体下载：`/files/{object_key}`；\n"
        "- 健康探针：`/api/v1/health`（存活）、`/health/db`（库）、\n"
        "  `/health/models`（模型提供商配置）。\n\n"
        "交互式文档：`/docs`（Swagger UI）· `/redoc`（ReDoc）。"
    ),
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAG_METADATA,
)

# ---- 中间件 ----
# CORS：显式来源白名单（settings.ALLOWED_ORIGINS，生产禁止通配 + 凭证组合）
_origins = settings.allowed_origins
_wildcard = "*" in _origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=not _wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(FilesAuthMiddleware)
app.add_middleware(RequestIDMiddleware)

# ---- 全局异常处理器（统一响应包络）----
register_exception_handlers(app)

# ---- 业务路由 ----
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# ---- 本地存储静态下载：/files/{object_key} ----
# 确保存储目录存在后再挂载（storage 单例导入时已尝试创建，此处兜底）
storage.root_path.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=str(storage.root_path)), name="files")
