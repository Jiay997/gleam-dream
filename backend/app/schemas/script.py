"""剧本域 Pydantic Schema 模块（Task 7a）。

定义 /projects/{id}/scripts、/documents、/script-lines、
/parse-confidence-items 等剧本域路由的请求体模型，以及
ORM -> 响应字典的序列化函数（统一与 ``success_envelope`` 配套使用）。

包含：
- 台词行局部更新（PATCH /script-lines/{line_id}）请求模型；
- 局部改写（POST /projects/{id}/scripts/rewrite）请求模型；
- 解析置信度人工校正（PATCH /parse-confidence-items/{item_id}）请求模型；
- ``script_out`` / ``episode_out`` / ``scene_out`` / ``line_out`` /
  ``document_out`` / ``confidence_item_out`` 序列化函数。

枚举取值与数据库 CHECK 约束严格对齐：
- line_type 4 值：dialogue/narration/action/stage_direction；
- emotion 8 值：calm/joy/anger/sadness/fear/surprise/tension/romance。
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.script import (
    Document,
    Episode,
    ParseConfidenceItem,
    Scene,
    Script,
    ScriptLine,
)

# ---- 行类型 4 值（script_lines.line_type CHECK） ----
LineType = str  # 开放收口：LLM 输出经服务层归一化后落库，Schema 层不作 Literal 限制
# ---- 情绪标签 8 值（script_lines.emotion_tag CHECK） ----
EMOTION_TAGS: frozenset[str] = frozenset({
    "calm", "joy", "anger", "sadness", "fear", "surprise", "tension", "romance",
})
# ---- 行类型 4 值（script_lines.line_type CHECK） ----
LINE_TYPES: frozenset[str] = frozenset({"dialogue", "narration", "action", "stage_direction"})
# ---- 时段 4 值（scenes.time_of_day CHECK: day/night/dawn/dusk） ----
TIME_OF_DAY_DB: frozenset[str] = frozenset({"day", "night", "dawn", "dusk"})


class ScriptLinePatchRequest(BaseModel):
    """台词行局部更新请求体（PATCH /script-lines/{line_id}）。

    PATCH 语义：仅显式传入的字段会被更新；至少传入一个字段。

    Attributes:
        content: 新行内容（1~2000 字）。
        speaker: 新说话人名（≤64 字，dialogue 行适用）。
        emotion: 新情绪标签（8 值之一）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(
        default=None, min_length=1, max_length=2000, description="行内容"
    )
    speaker: str | None = Field(default=None, max_length=64, description="说话人")
    emotion: str | None = Field(
        default=None, description=f"情绪标签（{'/'.join(sorted(EMOTION_TAGS))}）"
    )


class ScriptRewriteRequest(BaseModel):
    """局部改写请求体（POST /projects/{project_id}/scripts/rewrite）。

    Attributes:
        episode_no: 目标集号（≥1）。
        scene_no: 目标场号（≥1；缺省时改写整集全部场次）。
        feedback: 改写反馈原文（≥5 字，作为编剧 Agent 的改写依据）。
    """

    model_config = ConfigDict(extra="forbid")

    episode_no: int = Field(ge=1, description="目标集号")
    scene_no: int | None = Field(default=None, ge=1, description="目标场号（缺省改写整集）")
    feedback: str = Field(min_length=5, max_length=2000, description="改写反馈（≥5 字）")


class ConfidenceItemPatchRequest(BaseModel):
    """解析置信度人工校正请求体（PATCH /parse-confidence-items/{item_id}）。

    Attributes:
        corrected_value: 人工校正值（1~2000 字，写入后该项 needs_manual 置否）。
    """

    model_config = ConfigDict(extra="forbid")

    corrected_value: str = Field(
        min_length=1, max_length=2000, description="人工校正值"
    )


class VersionRestoreRequest(BaseModel):
    """历史版本恢复请求体（POST /projects/{id}/script/versions/{vid}/restore）。

    Attributes:
        label: 新版本命名备注（≤50 字，缺省自动生成「恢复自 vN」）。
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=50, description="新版本备注")


class ScriptContinueRequest(BaseModel):
    """剧本续写请求体（POST /projects/{id}/script/continue，US-F2-08）。

    Attributes:
        count: 续写集数（1~10，且「当前集数 + count」不得 > 30）。
        outline: 续写要点（≥5 字，编剧据此展开新集）。
    """

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1, le=10, description="续写集数")
    outline: str = Field(min_length=5, max_length=500, description="续写要点")


class ScriptExpandRequest(BaseModel):
    """单场扩写请求体（POST /projects/{id}/script/expand，US-F2-08）。

    Attributes:
        episode_no: 目标集号（≥1）。
        scene_no: 目标场号（≥1）。
        instruction: 扩写指令（≤200 字，AC-F2-08-2）。
    """

    model_config = ConfigDict(extra="forbid")

    episode_no: int = Field(ge=1, description="目标集号")
    scene_no: int = Field(ge=1, description="目标场号")
    instruction: str = Field(min_length=5, max_length=200, description="扩写指令（≤200 字）")


class ApplyTemplateRequest(BaseModel):
    """模板包套用请求体（POST /projects/{id}/apply-template，US-F2-09）。

    Attributes:
        template_id: 模板包 ID（须存在于内置模板库）。
        deviated_fields: 用户手改过的字段名（缺省空，套用后按项目当前值比较）。
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1, max_length=64, description="模板包 ID")
    deviated_fields: list[str] = Field(
        default_factory=list, description="已手改偏离的字段名"
    )


# ---- ORM -> 响应字典 序列化函数 ----


def line_out(line: ScriptLine) -> dict[str, Any]:
    """把剧本行 ORM 实例序列化为响应字典。

    Args:
        line: 剧本行实例。

    Returns:
        含 id/scene_id/line_no/line_type/speaker/content/emotion/
        order_index 的字典。
    """
    return {
        "id": int(line.id),
        "scene_id": int(line.scene_id),
        "line_no": int(line.line_no),
        "line_type": line.line_type,
        "speaker": line.speaker_name,
        "content": line.content,
        "emotion": line.emotion_tag,
        "order_index": int(line.order_index),
    }


def characters_in_scene(lines: list[dict[str, Any]]) -> list[dict[str, str]]:
    """从场次行中派生「本场出场角色 + 当前着装」。

    2026-11 QA 修复（A1 字段缺失）：剧本数据结构为 scenes → lines，没有
    ``characters_in_scene`` 字段，验收方无法直接读到"本场有谁、穿什么"。
    这里从台词 speaker 与动作行「名字——着装——」标注确定性派生，不改库表结构。

    Args:
        lines: 已序列化的剧本行列表。

    Returns:
        ``[{"name": 角色名, "outfit": 着装描述}]``（按首次出现顺序去重）。

    Raises:
        无。
    """
    found: dict[str, dict[str, str]] = {}
    for line in lines:
        if str(line.get("line_type") or "") == "dialogue":
            name = str(line.get("speaker") or "").strip()
            if name:
                found.setdefault(name, {"name": name, "outfit": ""})
        if str(line.get("line_type") or "") != "action":
            continue
        content = str(line.get("content") or "")
        for name, outfit in re.findall(r"([\u4e00-\u9fa5]{2,4})——([^—]{2,80})——", content):
            entry = found.setdefault(name, {"name": name, "outfit": ""})
            if not entry["outfit"]:
                entry["outfit"] = outfit.strip()
    return list(found.values())


def scene_out(scene: Scene, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """把场次 ORM 实例与其行列表序列化为响应字典。

    Args:
        scene: 场次实例。
        lines: 已序列化的行字典列表（按 order_index 升序）。

    Returns:
        含场次字段、lines 与派生 ``characters_in_scene`` 的字典。
    """
    return {
        "id": int(scene.id),
        "episode_id": int(scene.episode_id),
        "scene_no": int(scene.scene_no),
        "location": scene.location,
        "time_of_day": scene.time_of_day,
        "int_ext": scene.int_ext,
        "summary": scene.summary,
        "order_index": int(scene.order_index),
        "lines": lines,
        # 派生字段：镜头（shots）由分镜层产出，剧本层只提供场次与行。
        "characters_in_scene": characters_in_scene(lines),
    }


def episode_out(episode: Episode, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    """把分集 ORM 实例与其场次列表序列化为响应字典。

    Args:
        episode: 分集实例。
        scenes: 已序列化的场次字典列表（按 order_index 升序）。

    Returns:
        含分集字段与 scenes 的字典。
    """
    return {
        "id": int(episode.id),
        "episode_no": int(episode.episode_no),
        "title": episode.title,
        "synopsis": episode.synopsis,
        "target_duration_seconds": episode.target_duration_seconds,
        "hook_text": episode.hook_text,
        "hook_suggestion": episode.hook_suggestion,
        "est_duration_sec": episode.est_duration_sec,
        "order_index": int(episode.order_index),
        "status": episode.status,
        "scenes": scenes,
    }


def script_out(script: Script) -> dict[str, Any]:
    """把剧本版本 ORM 实例序列化为响应字典。

    Args:
        script: 剧本版本实例。

    Returns:
        含版本元数据的字典（不含 structure 全文，树由服务层组装）。
    """
    created_at: datetime | None = script.created_at
    return {
        "id": int(script.id),
        "project_id": int(script.project_id),
        "version_no": int(script.version_no),
        "parent_script_id": (
            int(script.parent_script_id) if script.parent_script_id else None
        ),
        "source_type": script.source_type,
        "document_id": int(script.document_id) if script.document_id else None,
        "status": script.status,
        "title": script.title,
        "logline": script.logline,
        "genre": script.genre,
        "total_episodes": int(script.total_episodes),
        "total_words": int(script.total_words),
        "version_label": script.version_label,
        "confidence_overall": (
            float(script.confidence_overall)
            if script.confidence_overall is not None
            else None
        ),
        "created_at": created_at.isoformat() if created_at else None,
    }


def document_out(
    document: Document,
    *,
    overall_confidence: Decimal | float | None,
    manual_pending_count: int,
) -> dict[str, Any]:
    """把上传文档 ORM 实例序列化为响应字典（解析状态视图）。

    Args:
        document: 文档实例。
        overall_confidence: 全文档置信度均值（无置信项时为 None）。
        manual_pending_count: 待人工校正的低置信项数量。

    Returns:
        含解析状态与置信度概览的字典。
    """
    created_at: datetime | None = document.created_at
    return {
        "id": int(document.id),
        "document_id": int(document.id),
        "project_id": int(document.project_id),
        "file_name": document.file_name,
        "doc_type": document.doc_type,
        "parse_status": document.parse_status,
        "parse_error": document.parse_error,
        "overall_confidence": (
            float(overall_confidence) if overall_confidence is not None else None
        ),
        "manual_pending_count": manual_pending_count,
        "created_at": created_at.isoformat() if created_at else None,
    }


def confidence_item_out(item: ParseConfidenceItem) -> dict[str, Any]:
    """把解析置信度项 ORM 实例序列化为响应字典。

    Args:
        item: 置信度项实例。

    Returns:
        含目标引用/字段名/置信度/校正信息的字典。
    """
    return {
        "id": int(item.id),
        "document_id": int(item.document_id) if item.document_id else None,
        "script_id": int(item.script_id) if item.script_id else None,
        "target_type": item.target_type,
        "target_ref": item.target_ref,
        "field_name": item.field_name,
        "confidence": float(item.confidence),
        "extracted_value": item.extracted_value,
        "corrected_value": item.corrected_value,
        "needs_manual": bool(item.needs_manual),
        "reviewed_at": (
            item.reviewed_at.isoformat() if item.reviewed_at else None
        ),
    }
