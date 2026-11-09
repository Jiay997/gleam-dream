"""剧本锚点 ↔ 画面语义校验服务。

背景：渲染侧 QC 此前只做伪向量人脸一致性、帧差动态性与时长覆盖，
没有任何「画面是否真的演了剧本这一段」的校验。剧本锚点
（``shots.script_anchor``）写入了台词 + 动作行原文，但生成结果跑偏时
系统无法感知，只能靠人工逐镜复看。

本模块提供两层软校验（均为 warning 级别，不自动重生成，避免成本失控）：

1. :func:`check_anchor_coverage`：**确定性规则校验（默认开启，0 成本）**
   比较剧本锚点与「实际发给模型的提示词」的语义覆盖，直接发现
   「剧本写了动作/道具，但提示词里没有」这类信息丢失。
2. :func:`check_frames_with_vision`：**抽帧视觉比对（可选）**
   把首/中/尾帧交给多模态模型描述，再与锚点比对。当前项目文本模型
   不支持图片输入，故默认关闭（``ANCHOR_VISION_QC=1`` 开启）；
   任何失败都返回 None，由调用方按规则校验结果处理。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from app.services.motion_service import ACTION_VERBS, image_to_data_uri

logger = logging.getLogger("gleamdream.anchor_qc")

#: 规则校验覆盖率阈值：锚点二元组在提示词中的覆盖比例低于该值即判定跑偏
ANCHOR_COVERAGE_THRESHOLD: float = 0.5
#: 视觉比对阈值（0~1，多模态模型给出的 match 分数）
ANCHOR_VISION_THRESHOLD: float = 0.6
#: 视觉比对开关（默认关闭：当前 LLM 路由均为纯文本模型）
VISION_ENABLED: bool = os.getenv("ANCHOR_VISION_QC", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
#: 参与比对的最少锚点二元组数量（过短锚点不做覆盖率判定，避免误报）
_MIN_ANCHOR_GRAMS: int = 8

#: 道具标注解析（与 storyboard_service._parse_action_annotations 同口径）
_PROP_PATTERN = re.compile(r"道具(?:：|:)([^；;\n。]+)")


def _anchor_text(shot: Any) -> str:
    """提取镜头的剧本锚点文本。

    Args:
        shot: 镜头实例（读取 script_anchor / action_desc / dialogue）。

    Returns:
        锚点文本；全部缺失时返回空字符串。

    Raises:
        无。
    """
    anchor = getattr(shot, "script_anchor", None)
    if isinstance(anchor, dict):
        text = str(anchor.get("script_text") or "").strip()
        if text:
            return text
    return str(getattr(shot, "action_desc", None) or getattr(shot, "dialogue", None) or "").strip()


def _prompt_text(shot: Any) -> str:
    """提取实际发给模型的提示词文本。

    Args:
        shot: 镜头实例（读取 final_prompt / image_prompt / action_desc）。

    Returns:
        提示词文本；全部缺失时返回空字符串。

    Raises:
        无。
    """
    return " ".join(
        str(value or "")
        for value in (
            getattr(shot, "final_prompt", None),
            getattr(shot, "image_prompt", None),
            getattr(shot, "action_desc", None),
        )
        if str(value or "").strip()
    )


def _bigrams(text: str) -> set[str]:
    """把中文文本切成二元组集合（忽略空白与标点）。

    Args:
        text: 待切分文本。

    Returns:
        二元组集合（如「林晚猛地站起来」→ {林晚, 晚猛, 猛地, ...}）。

    Raises:
        无。
    """
    cleaned = re.sub(r"[\s，。；：、！？（）【】\[\]「」“”\"'·,.;:!?()/-]", "", str(text or ""))
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _anchor_prop_names(text: str) -> list[str]:
    """从锚点文本解析「道具：…」中的道具名。

    Args:
        text: 锚点文本。

    Returns:
        道具名列表（去重，长度 ≥2）。

    Raises:
        无。
    """
    names: list[str] = []
    for match in _PROP_PATTERN.finditer(str(text or "")):
        for raw in re.split(r"[、,，/]", match.group(1)):
            name = raw.strip().strip("「」“”\"' ")
            if len(name) >= 2 and name not in names:
                names.append(name)
    return names


def check_anchor_coverage(shot: Any) -> dict[str, Any]:
    """规则校验剧本锚点是否被提示词完整承载（0 成本、确定性）。

    判定维度：
    - **二元组覆盖率**：锚点中文二元组中出现在提示词的比例；
    - **动作动词覆盖率**：锚点中命中的动作动词是否出现在提示词；
    - **道具覆盖率**：锚点「道具：X」中的道具名是否出现在提示词。

    任一项缺失都会记入 ``reasons``；综合覆盖率低于阈值则 ``passed=False``，
    由调用方降级为 warning（不自动重生成）。

    Args:
        shot: 镜头实例（读取 script_anchor / final_prompt / image_prompt）。

    Returns:
        ``{passed, score, coverage, action_coverage, missing_verbs,
        missing_props, reasons, anchor_chars, prompt_chars}``；
        锚点过短或缺失时返回 ``passed=True`` 并附 ``skipped`` 原因。

    Raises:
        无：任何字段异常都按通过处理，避免误伤渲染结果。
    """
    anchor = _anchor_text(shot)
    prompt = _prompt_text(shot)
    if not anchor:
        return {"passed": True, "score": 1.0, "skipped": "锚点为空"}
    anchor_grams = _bigrams(anchor)
    if len(anchor_grams) < _MIN_ANCHOR_GRAMS:
        return {"passed": True, "score": 1.0, "skipped": "锚点过短"}
    if not prompt:
        return {
            "passed": False,
            "score": 0.0,
            "reasons": ["提示词为空，剧本锚点完全没有进入生成链路"],
            "anchor_chars": len(anchor),
            "prompt_chars": 0,
        }

    prompt_grams = _bigrams(prompt)
    covered = anchor_grams & prompt_grams
    coverage = len(covered) / len(anchor_grams)

    verbs = [verb for verb in ACTION_VERBS if verb in anchor]
    missing_verbs = [verb for verb in verbs if verb not in prompt]
    action_coverage = (
        (len(verbs) - len(missing_verbs)) / len(verbs) if verbs else 1.0
    )

    prop_names = _anchor_prop_names(anchor)
    missing_props = [name for name in prop_names if name not in prompt]
    prop_coverage = (
        (len(prop_names) - len(missing_props)) / len(prop_names) if prop_names else 1.0
    )

    reasons: list[str] = []
    if coverage < ANCHOR_COVERAGE_THRESHOLD:
        reasons.append(
            f"锚点文本覆盖率 {coverage:.2f} < {ANCHOR_COVERAGE_THRESHOLD}（剧本内容未进入提示词）"
        )
    if missing_verbs:
        reasons.append(f"动作动词未进入提示词：{'、'.join(missing_verbs[:6])}")
    if missing_props:
        reasons.append(f"道具未进入提示词：{'、'.join(missing_props[:6])}")

    return {
        "passed": not reasons,
        "score": round(coverage, 4),
        "coverage": round(coverage, 4),
        "action_coverage": round(action_coverage, 4),
        "prop_coverage": round(prop_coverage, 4),
        "missing_verbs": missing_verbs[:10],
        "missing_props": missing_props[:10],
        "reasons": reasons,
        "anchor_chars": len(anchor),
        "prompt_chars": len(prompt),
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中提取 JSON 对象。

    Args:
        text: 模型原始输出（可能含 Markdown 围栏）。

    Returns:
        解析后的字典；无法解析返回 None。

    Raises:
        无。
    """
    blob = str(text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", blob).strip()
    start, end = blob.find("{"), blob.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(blob[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def check_frames_with_vision(
    shot: Any,
    frames: list[str],
    *,
    project_id: int,
) -> dict[str, Any] | None:
    """用多模态模型比对抽帧画面与剧本锚点（可选，默认关闭）。

    仅在 :data:`VISION_ENABLED` 为真且拿到至少 1 张存证帧时执行。模型
    不支持图片输入、网络失败或输出不合规时一律返回 None，由调用方继续
    使用规则校验结论，不阻断渲染主流程。

    Args:
        shot: 镜头实例（读取 script_anchor / dialogue）。
        frames: 首/中/尾存证帧的本地路径列表（可空）。
        project_id: 项目 ID（网关记账与日志关联）。

    Returns:
        ``{passed, score, description, missing, reasons}``；
        未开启、无帧或调用失败返回 None。

    Raises:
        无：所有异常内部消化。
    """
    if not VISION_ENABLED or not frames:
        return None
    image_uris: list[str] = []
    for path in frames[:3]:
        uri = image_to_data_uri(path)
        if uri:
            image_uris.append(uri)
    if not image_uris:
        return None

    anchor = _anchor_text(shot)
    prompt = (
        "你是画面一致性质检员。下面是同一镜头视频的首/中/尾三帧画面。\n"
        f"剧本锚点：{anchor[:400]}\n"
        "请判断画面是否在演剧本锚点描述的内容（人物动作、道具、场景、情绪）。\n"
        '只输出 JSON：{"description":"画面实际发生了什么","match":true|false,'
        '"score":0~1,"missing":["缺失要素"]}'
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend({"type": "image_url", "image_url": {"url": uri}} for uri in image_uris)

    from app.infra.model_gateway.base import GenRequest
    from app.infra.model_gateway.gateway import model_gateway

    try:
        result = await model_gateway.generate(
            "llm",
            "llm_text",
            GenRequest(
                capability="llm",
                messages=[{"role": "user", "content": content}],
                params={"temperature": 0.1, "max_tokens": 600, "response_json": True},
            ),
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001 - 视觉 QC 失败不影响渲染结论
        logger.warning("视觉锚点校验调用失败 shot_id=%s: %s", getattr(shot, "id", "?"), exc)
        return None
    if not result.ok or not result.text:
        logger.info(
            "视觉锚点校验不可用 shot_id=%s err=%s",
            getattr(shot, "id", "?"), result.error,
        )
        return None
    payload = _extract_json(result.text)
    if not payload:
        return None

    try:
        score = float(payload.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    missing = [str(item) for item in (payload.get("missing") or []) if str(item).strip()]
    passed = bool(payload.get("match")) and score >= ANCHOR_VISION_THRESHOLD
    reasons = [f"视觉比对缺失：{'、'.join(missing[:6])}"] if missing else []
    if not passed and not reasons:
        reasons.append(f"视觉比对得分 {score:.2f} < {ANCHOR_VISION_THRESHOLD}")
    return {
        "passed": passed,
        "score": round(score, 4),
        "description": str(payload.get("description") or "")[:300],
        "missing": missing[:10],
        "reasons": reasons,
    }
