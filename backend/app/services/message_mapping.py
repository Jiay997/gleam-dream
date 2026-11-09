"""失败原因「用户语言」映射服务（V4.0 细节功能 SF-F5-13 / AC-F5-02-4）。

背景：渲染与分发失败后会暴露供应商原始错误码（``timeout`` / ``rate_limited`` /
``403`` 等），对用户不可读。本模块提供**运营可配置**的映射表，把技术错误
翻译成用户语言，并保留原始错误码供折叠查看：

- 渲染侧（F5）：``failure_type`` → 用户文案（AC-F5-02-4）；
- 分发侧（F7）：错误码/异常串 → 用户文案（AC-F7-09-1 复用 F5 体系）。

未命中映射时按域兜底，且**始终**提示积分已返还与可联系客服，避免用户误以为
需要自己排查。映射表可经 :func:`register_override` 在运行期热覆盖（运营侧配置），
不重启进程即生效。

计费护栏（GR-6）：本模块只负责文案，不参与扣费/退款决策。
"""

from __future__ import annotations

import logging
from typing import Final

logger = logging.getLogger("gleamdream.message_mapping")

#: 渲染失败类型 → 用户语言文案（AC-F5-02-4 基线表，可被运营覆盖）
_RENDER_MESSAGES: Final[dict[str, str]] = {
    "timeout": "模型响应超时，已自动切换备用模型重试",
    "rate_limited": "模型服务繁忙（触发限流），已排队等待重试",
    "moderation_rejected": "内容审核未通过，已返还积分；请调整画面或文案后重新生成",
    "param_invalid": "生成参数不合法（如提示词或时长越界），请检查后重试",
    "model_unavailable": "所选模型暂时不可用，已尝试备用模型",
    "unknown": "生成出现异常，已返还积分，可联系客服并提供错误编号",
}

#: 渲染失败类型 → 是否可重试（moderation_rejected 不可重试，GR-7）
_RETRYABLE: Final[frozenset[str]] = frozenset(
    {"timeout", "rate_limited", "param_invalid", "model_unavailable", "unknown"}
)

#: 分发失败 → 用户语言文案（AC-F7-09-1，复用 F5 体系）
_DISTRIBUTION_MESSAGES: Final[dict[str, str]] = {
    "auth_expired": "平台授权已过期，请重新授权后再分发",
    "rate_limited": "平台接口限流，请稍后重试",
    "timeout": "平台接口超时未返回结果，请到平台后台核实，避免重复发布",
    "result_unknown": "平台未返回明确结果，请到平台后台核实，避免重复发布",
    "network": "网络异常导致上传中断，可重试",
    "rejected": "平台审核拒绝，请调整标题/封面/内容后重试",
    "unknown": "分发出现异常，可重试或联系客服并提供错误编号",
}

#: 分发失败是否可重试（授权过期需先重新授权，故不直接重试）
_DISTRIBUTION_RETRYABLE: Final[frozenset[str]] = frozenset(
    {"rate_limited", "timeout", "network", "unknown"}
)

#: 运行期运营覆盖（键为 ``render:<type>`` / ``distribution:<type>``）
_OVERRIDES: dict[str, str] = {}


def register_override(domain: str, key: str, message: str) -> None:
    """登记一条运营侧文案覆盖（进程内热生效）。

    Args:
        domain: 域标识，``render`` 或 ``distribution``。
        key: 该域内的失败类型/错误码。
        message: 用户语言文案。
    """
    _OVERRIDES[f"{domain}:{key}"] = message
    logger.info("失败文案覆盖已登记 domain=%s key=%s", domain, key)


def clear_overrides() -> None:
    """清空全部运营覆盖（测试与配置回滚用）。"""
    _OVERRIDES.clear()


def render_failure_message(
    failure_type: str | None, error_code: str | None = None
) -> str:
    """把渲染失败类型翻译为用户语言文案。

    Args:
        failure_type: 失败类型（``timeout`` / ``rate_limited`` /
            ``moderation_rejected`` / ``param_invalid`` / ``model_unavailable`` /
            ``unknown``）；None 或未识别按 ``unknown`` 处理。
        error_code: 原始错误码（仅用于日志，文案中不含技术细节）。

    Returns:
        面向用户的失败文案（≤200 字）。
    """
    key = (failure_type or "unknown").strip().lower()
    override = _OVERRIDES.get(f"render:{key}")
    if override:
        return override[:200]
    message = _RENDER_MESSAGES.get(key)
    if message is None:
        logger.info(
            "渲染失败文案未映射 failure_type=%s error_code=%s，使用兜底文案",
            key,
            error_code,
        )
        message = _RENDER_MESSAGES["unknown"]
    return message[:200]


def distribution_failure_message(
    error_key: str | None, error_code: str | None = None
) -> str:
    """把分发失败原因翻译为用户语言文案。

    Args:
        error_key: 失败分类键（见 :data:`_DISTRIBUTION_MESSAGES`）。
        error_code: 原始错误码（仅用于日志）。

    Returns:
        面向用户的失败文案（≤200 字）。
    """
    key = (error_key or "unknown").strip().lower()
    override = _OVERRIDES.get(f"distribution:{key}")
    if override:
        return override[:200]
    message = _DISTRIBUTION_MESSAGES.get(key)
    if message is None:
        logger.info(
            "分发失败文案未映射 error_key=%s error_code=%s，使用兜底文案", key, error_code
        )
        message = _DISTRIBUTION_MESSAGES["unknown"]
    return message[:200]


def is_retryable(failure_type: str | None) -> bool:
    """判断渲染失败类型是否允许自动/手动重试。

    对齐 GR-7：``moderation_rejected`` 不提供重试入口，避免反复触发审核。

    Args:
        failure_type: 失败类型。

    Returns:
        允许重试返回 True。
    """
    return (failure_type or "unknown").strip().lower() in _RETRYABLE


def is_distribution_retryable(error_key: str | None) -> bool:
    """判断分发失败是否允许直接重试（授权过期需先重新授权）。

    Args:
        error_key: 分发失败分类键。

    Returns:
        允许重试返回 True。
    """
    return (error_key or "unknown").strip().lower() in _DISTRIBUTION_RETRYABLE


def render_failure_detail(
    failure_type: str | None,
    error_code: str | None,
    raw_reason: str | None,
) -> dict[str, object]:
    """组装渲染失败详情的对外结构（用户文案 + 可折叠原始信息）。

    Args:
        failure_type: 失败类型。
        error_code: 原始错误码。
        raw_reason: 原始失败详情。

    Returns:
        ``{user_message, error_code, failure_type, raw_reason, retryable}``。
    """
    key = (failure_type or "unknown").strip().lower()
    return {
        "user_message": render_failure_message(key, error_code),
        "failure_type": key,
        "error_code": error_code,
        "raw_reason": raw_reason,
        "retryable": is_retryable(key),
    }
