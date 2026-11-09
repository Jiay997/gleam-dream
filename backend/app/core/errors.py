"""统一业务错误与响应包络模块（契约 §8.1 / §8.3）。

核心约定：
- 所有响应体为统一包络 ``{code, message, data, request_id}``；
- ``code = 0`` 表示业务成功，非 0 见 :class:`ErrorCode`；
- HTTP 状态码与业务码解耦：业务失败一般仍返回 200，
  仅鉴权失败返回 401/403、限流返回 429（见 :func:`http_status_for`）；
- FastAPI 全局异常处理器在本模块注册：BizError / 参数校验失败(10001) /
  未捕获异常兜底(10003)。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.ids import new_request_id

logger = logging.getLogger("gleamdream.errors")


class ErrorCode:
    """业务错误码常量集（契约 §8.3 全量，含 V3.1 扩展码）。

    每个码的中文默认消息见 :data:`DEFAULT_MESSAGES`。
    """

    # 成功
    OK = 0

    # ---- 10000-19999 通用/系统 ----
    PARAM_INVALID = 10001           # 参数校验失败
    NOT_FOUND = 10002               # 资源不存在
    INTERNAL_ERROR = 10003          # 服务内部错误
    RATE_LIMITED = 10004            # 请求过于频繁
    IDEMPOTENCY_CONFLICT = 10005    # 幂等键冲突【扩展】
    VERSION_CONFLICT = 10006        # 数据版本冲突（乐观锁）【扩展】
    PAYLOAD_TOO_LARGE = 10007       # 请求体过大【扩展】

    # ---- 20000-29999 认证与授权 ----
    UNAUTHORIZED = 20001            # 未登录
    TOKEN_EXPIRED = 20002           # token 过期
    FORBIDDEN = 20003               # 无权限
    ACCOUNT_BANNED = 20004          # 账号被封禁
    LOGIN_RISK = 20005              # 登录风控

    # ---- 30000-39999 计费 ----
    CREDIT_INSUFFICIENT = 30001     # 积分不足
    BUDGET_EXCEEDED = 30002         # 预算超限
    ORDER_ALREADY_PAID = 30003      # 订单已支付
    PAY_CALLBACK_SIGN_ERROR = 30004 # 支付回调签名错误
    FREEZE_NOT_FOUND = 30005        # 冻结记录不存在

    # ---- 40000-49999 项目与剧本 ----
    PROJECT_STATE_INVALID = 40001   # 项目状态不允许该操作
    SCRIPT_STRUCTURE_INVALID = 40002  # 剧本结构校验失败
    PARSE_CONFIDENCE_LOW = 40003    # 解析置信度过低
    PROJECT_ARCHIVED = 40004        # 项目已归档
    EPISODE_LIMIT_EXCEEDED = 40005  # 集数超限【扩展】
    PROJECT_TITLE_DUPLICATED = 40006  # 项目名与现有项目重复【扩展】

    # ---- 50000-59999 资产 ----
    ASSET_NOT_LOCKED = 50001        # 资产未锁定
    FACE_SIMILARITY_LOW = 50002     # 人脸相似度过低
    HUMAN_AUTH_MISSING = 50003      # 未完成真人授权
    UPLOAD_FILE_INVALID = 50004     # 上传文件非法
    ASSET_LIMIT_EXCEEDED = 50005    # 资产超限【扩展】

    # ---- 60000-69999 分镜与生成 ----
    STORYBOARD_NOT_LOCKED = 60001   # 分镜未锁定
    TASK_NOT_FOUND = 60002          # 任务不存在
    QC_NOT_PASSED = 60003           # 质检未通过
    RETRY_LIMIT_EXCEEDED = 60004    # 重试次数超限
    ASSET_ANCHOR_MISSING = 60005    # 资产锚点缺失
    CONCURRENCY_LIMIT_EXCEEDED = 60006  # 并发任务超限【扩展】

    # ---- 70000-79999 后期与导出 ----
    AV_SYNC_FAILED = 70001          # 音画同步不达标
    EXPORT_PARAM_INVALID = 70002    # 导出参数非法
    ENTITLEMENT_INSUFFICIENT = 70003  # 权益不足（如 4K）
    EXPORT_LIMIT_EXCEEDED = 70004   # 导出超限【扩展】
    VOICE_MISSING = 70005           # 台词缺少有效配音（禁止静默合成无声成片）

    # ---- 80000-89999 外部依赖 ----
    MODEL_UNAVAILABLE = 80001       # 模型不可用
    THIRD_PARTY_REVIEW_FAILED = 80002  # 三方审核失败
    STORAGE_ERROR = 80003           # 存储服务异常
    PAY_GATEWAY_ERROR = 80004       # 支付网关异常
    SMS_ERROR = 80005               # 短信服务异常【扩展】

    # ---- 90000-99999 管理后台 ----
    CONFIG_CONFLICT = 90001         # 配置冲突
    SENSITIVE_WORD_IMPORT_FAILED = 90002  # 敏感词库导入失败


#: 错误码 -> 中文默认消息 映射
DEFAULT_MESSAGES: dict[int, str] = {
    ErrorCode.OK: "ok",
    # 通用/系统
    ErrorCode.PARAM_INVALID: "参数校验失败",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.RATE_LIMITED: "请求过于频繁",
    ErrorCode.IDEMPOTENCY_CONFLICT: "幂等键冲突",
    ErrorCode.VERSION_CONFLICT: "数据版本冲突，请刷新后重试",
    ErrorCode.PAYLOAD_TOO_LARGE: "请求体过大",
    # 认证与授权
    ErrorCode.UNAUTHORIZED: "未登录或登录凭证无效",
    ErrorCode.TOKEN_EXPIRED: "登录已过期，请重新登录",
    ErrorCode.FORBIDDEN: "无权限执行该操作",
    ErrorCode.ACCOUNT_BANNED: "账号已被封禁",
    ErrorCode.LOGIN_RISK: "登录触发风控，请稍后重试",
    # 计费
    ErrorCode.CREDIT_INSUFFICIENT: "积分不足",
    ErrorCode.BUDGET_EXCEEDED: "预算超限",
    ErrorCode.ORDER_ALREADY_PAID: "订单已支付，请勿重复支付",
    ErrorCode.PAY_CALLBACK_SIGN_ERROR: "支付回调签名错误",
    ErrorCode.FREEZE_NOT_FOUND: "冻结记录不存在",
    # 项目与剧本
    ErrorCode.PROJECT_STATE_INVALID: "项目当前状态不允许该操作",
    ErrorCode.SCRIPT_STRUCTURE_INVALID: "剧本结构校验失败",
    ErrorCode.PARSE_CONFIDENCE_LOW: "解析置信度过低，请人工确认",
    ErrorCode.PROJECT_ARCHIVED: "项目已归档",
    ErrorCode.EPISODE_LIMIT_EXCEEDED: "集数超出上限",
    ErrorCode.PROJECT_TITLE_DUPLICATED: "项目名与现有项目重复",
    # 资产
    ErrorCode.ASSET_NOT_LOCKED: "资产未锁定",
    ErrorCode.FACE_SIMILARITY_LOW: "人脸相似度过低",
    ErrorCode.HUMAN_AUTH_MISSING: "未完成真人授权",
    ErrorCode.UPLOAD_FILE_INVALID: "上传文件非法",
    ErrorCode.ASSET_LIMIT_EXCEEDED: "资产数量超出上限",
    # 分镜与生成
    ErrorCode.STORYBOARD_NOT_LOCKED: "分镜未锁定",
    ErrorCode.TASK_NOT_FOUND: "任务不存在",
    ErrorCode.QC_NOT_PASSED: "质检未通过",
    ErrorCode.RETRY_LIMIT_EXCEEDED: "重试次数超限",
    ErrorCode.ASSET_ANCHOR_MISSING: "资产锚点缺失",
    ErrorCode.CONCURRENCY_LIMIT_EXCEEDED: "并发生成任务超出上限",
    # 后期与导出
    ErrorCode.AV_SYNC_FAILED: "音画同步不达标",
    ErrorCode.EXPORT_PARAM_INVALID: "导出参数非法",
    ErrorCode.ENTITLEMENT_INSUFFICIENT: "权益不足",
    ErrorCode.EXPORT_LIMIT_EXCEEDED: "导出次数超出上限",
    ErrorCode.VOICE_MISSING: "台词缺少有效配音，禁止合成无声成片",
    # 外部依赖
    ErrorCode.MODEL_UNAVAILABLE: "模型服务不可用",
    ErrorCode.THIRD_PARTY_REVIEW_FAILED: "三方内容审核失败",
    ErrorCode.STORAGE_ERROR: "存储服务异常",
    ErrorCode.PAY_GATEWAY_ERROR: "支付网关异常",
    ErrorCode.SMS_ERROR: "短信服务异常",
    # 管理后台
    ErrorCode.CONFIG_CONFLICT: "配置冲突",
    ErrorCode.SENSITIVE_WORD_IMPORT_FAILED: "敏感词库导入失败",
}

#: 业务码 -> HTTP 状态码 的特例映射（契约 §8.1：鉴权 401/403、限流 429，其余 200）
_HTTP_STATUS_BY_CODE: dict[int, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.RATE_LIMITED: 429,
}


class BizError(Exception):
    """业务异常：携带契约错误码、中文消息与可选扩展数据。

    服务层/仓储层抛出本异常后，由全局异常处理器转换为统一包络响应。

    Attributes:
        code: 业务错误码（契约 §8.3）。
        message: 中文错误消息（未显式指定时取该码默认消息）。
        data: 可选扩展数据（如冲突详情、字段级错误）。
        error_detail: 可选错误细分类标记（V4.0，06 文档 §22.2）——用于区分
            同一业务码下的多重语义，如 ``10001 + episode_limit_exceeded``、
            ``50003 + authorization_revoked``。
    """

    def __init__(
        self,
        code: int,
        message: str | None = None,
        *,
        data: Any | None = None,
        error_detail: str | None = None,
    ) -> None:
        """构造业务异常。

        Args:
            code: 业务错误码。
            message: 自定义消息；为 None 时使用该码的中文默认消息。
            data: 附加到包络 data 字段的扩展数据。
            error_detail: 错误细分类标记（V4.0）。

        Raises:
            无（本构造函数不会抛出异常）。
        """
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, "未知错误")
        self.data = data
        self.error_detail = error_detail
        super().__init__(self.message)


def http_status_for(code: int) -> int:
    """根据业务码决定 HTTP 状态码。

    规则（契约 §8.1）：20001/20002 -> 401，20003 -> 403，
    10004 -> 429，其余业务码一律 200。

    Args:
        code: 业务错误码。

    Returns:
        对应的 HTTP 状态码。
    """
    return _HTTP_STATUS_BY_CODE.get(code, 200)


def success_envelope(data: Any = None, request_id: str | None = None, *, message: str = "ok") -> dict[str, Any]:
    """构造成功响应包络。

    Args:
        data: 业务数据，默认 None。
        request_id: 请求 ID；缺省时自动生成 ULID。
        message: 消息文本，默认 "ok"。

    Returns:
        统一包络字典 ``{code, message, data, request_id}``，code 恒为 0。
    """
    return {
        "code": ErrorCode.OK,
        "message": message,
        "data": data,
        "request_id": request_id or new_request_id(),
    }


def error_envelope(
    code: int,
    message: str,
    request_id: str | None = None,
    data: Any | None = None,
    error_detail: str | None = None,
) -> dict[str, Any]:
    """构造错误响应包络。

    Args:
        code: 业务错误码（非 0）。
        message: 中文错误消息。
        request_id: 请求 ID；缺省时自动生成 ULID。
        data: 可选错误扩展数据。
        error_detail: 可选错误细分类标记（V4.0，06 文档 §22.2）。

    Returns:
        统一包络字典 ``{code, message, data, request_id}``；当 ``error_detail``
        非空时额外携带该字段（既有端点字段不删不改，仅在需要时新增）。
    """
    envelope: dict[str, Any] = {
        "code": code,
        "message": message,
        "data": data,
        "request_id": request_id or new_request_id(),
    }
    if error_detail:
        envelope["error_detail"] = error_detail
    return envelope


def _current_request_id(request: Request) -> str:
    """从请求状态中取 request_id，缺失时兜底生成。

    Args:
        request: 当前请求对象。

    Returns:
        请求 ID 字符串。
    """
    return getattr(request.state, "request_id", None) or new_request_id()


def _simplify_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """将 pydantic 校验错误简化为可 JSON 序列化的结构。

    Args:
        exc: FastAPI 参数校验异常。

    Returns:
        仅含 loc/msg/type 的错误列表，避免 ctx 中不可序列化对象。
    """
    simplified: list[dict[str, Any]] = []
    for err in exc.errors():
        simplified.append(
            {
                "loc": [str(item) for item in err.get("loc", [])],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return simplified


def register_exception_handlers(app: FastAPI) -> None:
    """在 FastAPI 应用上注册全局异常处理器。

    注册三类处理器：
    1. BizError -> 按业务码映射 HTTP 状态（401/403/429 特例，其余 200）；
    2. RequestValidationError -> code=10001，HTTP 200；
    3. Exception 兜底 -> code=10003，HTTP 500，日志记录完整堆栈但
       不向客户端泄露内部细节。

    Args:
        app: FastAPI 应用实例。
    """

    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
        """业务异常处理器：返回业务码包络。

        Args:
            request: 当前请求。
            exc: 业务异常。

        Returns:
            统一包络的 JSONResponse。
        """
        rid = _current_request_id(request)
        logger.warning(
            "业务异常 code=%s message=%s path=%s request_id=%s",
            exc.code, exc.message, request.url.path, rid,
        )
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=error_envelope(
                exc.code, exc.message, rid, exc.data, exc.error_detail
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """参数校验失败处理器：统一返回 10001。

        2026-11 QA 修复：参数非法此前返回 HTTP 200（只有业务码非 0），
        监控/客户端无法按 HTTP 语义识别失败。现改为 422，同时保留统一
        包络中的 10001 业务码与错误明细。

        Args:
            request: 当前请求。
            exc: 参数校验异常。

        Returns:
            code=10001 的统一包络 JSONResponse（HTTP 422）。
        """
        rid = _current_request_id(request)
        logger.warning(
            "参数校验失败 path=%s request_id=%s errors=%s",
            request.url.path, rid, _simplify_validation_errors(exc),
        )
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                ErrorCode.PARAM_INVALID,
                DEFAULT_MESSAGES[ErrorCode.PARAM_INVALID],
                rid,
                {"errors": _simplify_validation_errors(exc)},
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """未捕获异常兜底处理器：统一返回 10003，不泄露内部细节。

        Args:
            request: 当前请求。
            exc: 任意未处理异常。

        Returns:
            code=10003 的统一包络 JSONResponse（HTTP 500）。
        """
        rid = _current_request_id(request)
        logger.exception("服务内部错误 path=%s request_id=%s error=%r", request.url.path, rid, exc)
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                ErrorCode.INTERNAL_ERROR,
                DEFAULT_MESSAGES[ErrorCode.INTERNAL_ERROR],
                rid,
            ),
        )
