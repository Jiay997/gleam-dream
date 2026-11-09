"""AI 生成内容标识服务（V4.0 合规强制 NFR-C6 / AC-F7-08-1~2）。

依据《人工智能生成合成内容标识办法》，导出母版必须同时携带：

1. **显式标识**（visible）：片尾固定位置嵌入「本内容由 AI 辅助生成」文字，
   样式与位置由法务口径配置；**用户不可关闭**（导出确认页仅展示，无开关）；
2. **隐式标识**（implicit）：文件元数据中写入 AI 生成声明字段，并随分发
   元数据上送平台（与 Q14 隐形水印协同）。

本模块是标识口径的**唯一事实源**：渲染管线、导出确认页、分发元数据三处
共用同一份配置，避免各处文案漂移导致合规覆盖不到 100%（``ai_label_rendered``
埋点目标覆盖率 100%）。标识文案与位置来自 Q15【假设待验证：需法务最终口径】，
集中在此便于一次性替换。
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

#: 显式标识文案（Q15 待法务确认，集中维护）
VISIBLE_LABEL_TEXT: Final[str] = "本内容由 AI 辅助生成"

#: 显式标识固定位置：片尾（tail）——不可配置为 off
VISIBLE_LABEL_POSITION: Final[str] = "tail"

#: 显式标识视觉参数（导出确认页预览与渲染管线共用）
VISIBLE_LABEL_STYLE: Final[dict[str, Any]] = {
    "font_size": 28,
    "opacity": 0.85,
    "color": "#FFFFFF",
    "shadow": True,
    "duration_ms": 3000,
    "background": "rgba(0,0,0,0.45)",
}

#: 隐式标识元数据键（写入文件元数据并随平台 API 上送）
IMPLICIT_LABEL_KEY: Final[str] = "AIGC"
IMPLICIT_LABEL_VALUE: Final[str] = "1"
IMPLICIT_CONTENT_PRODUCER: Final[str] = "GleamDream Studio"


def build_ai_label(
    project_id: int,
    episode_no: int | None = None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 AI 标识配置（显式 + 隐式）。

    Args:
        project_id: 项目 ID（用于生成可追溯的隐式标识内容编号）。
        episode_no: 集号；None 表示不分集（整片）。
        extra: 额外元数据（如平台专属标识字段），合并进 ``implicit.metadata``。

    Returns:
        ``{visible: {enabled, text, position, style, closable}, implicit: {...}}``；
        ``closable`` 恒为 False（合规要求用户不可关闭）。
    """
    scope = f"{project_id}-{episode_no if episode_no is not None else 'all'}"
    content_id = hashlib.sha256(
        f"{IMPLICIT_CONTENT_PRODUCER}:{scope}".encode("utf-8")
    ).hexdigest()[:32]
    metadata: dict[str, Any] = {
        IMPLICIT_LABEL_KEY: IMPLICIT_LABEL_VALUE,
        "content_producer": IMPLICIT_CONTENT_PRODUCER,
        "content_id": content_id,
        "regulation": "《人工智能生成合成内容标识办法》",
    }
    if extra:
        metadata.update(extra)
    return {
        "visible": {
            "enabled": True,
            "text": VISIBLE_LABEL_TEXT,
            "position": VISIBLE_LABEL_POSITION,
            "style": dict(VISIBLE_LABEL_STYLE),
            "closable": False,
        },
        "implicit": {"metadata": metadata},
    }


def ai_label_metadata(ai_label: dict[str, Any] | None) -> dict[str, Any]:
    """从标识配置中抽取隐式元数据（供分发元数据上送与文件写入）。

    Args:
        ai_label: :func:`build_ai_label` 的产物；None 时按空处理。

    Returns:
        隐式标识元数据字典（恒含 AIGC 声明）。
    """
    if not ai_label:
        return {IMPLICIT_LABEL_KEY: IMPLICIT_LABEL_VALUE}
    implicit = ai_label.get("implicit") or {}
    metadata = implicit.get("metadata") or {}
    return dict(metadata)


def visible_label_text(ai_label: dict[str, Any] | None) -> str:
    """取显式标识文案（渲染管线落片用）。

    Args:
        ai_label: 标识配置；None 时回退默认文案。

    Returns:
        显式标识文案字符串。
    """
    if not ai_label:
        return VISIBLE_LABEL_TEXT
    visible = ai_label.get("visible") or {}
    return str(visible.get("text") or VISIBLE_LABEL_TEXT)


def is_label_enabled(ai_label: dict[str, Any] | None) -> bool:
    """判断标识是否启用（合规强制：恒为 True，不接受关闭请求）。

    Args:
        ai_label: 标识配置。

    Returns:
        恒为 True——显式标识不可关闭。
    """
    return True
