"""剧本域服务模块（Task 7a）。

承载剧本域全部业务逻辑：

- **上传与异步解析**：校验 txt/md 文档（docx 暂不支持）→ 落存储与
  documents 行 → 后台任务按 8000 字分段调用 LLM 抽取「集-场-行」结构
  与逐字段置信度 → 合并落库 scripts(v1)/episodes/scenes/script_lines/
  parse_confidence_items → 发布 ``document.parsed`` 事件；
- **剧本树查询**：当前版本/指定版本的集-场-行树与只读大纲（P07）；
- **行级编辑**：PATCH 台词行 → 原地更新行 + structure 快照更新 +
  版本号 +1 + operation_logs 留痕；
- **局部改写**：按集/场上下文调用编剧 LLM（五段式提示词，五段式 +
  只输出 JSON）同步替换受影响行 + 新版本；
- **版本 diff**：按行文本对齐比对两个版本的 structure 快照；
- **合规送审**：复用 Task 6a 审核提示词，block 拒绝、flag 自动弱化
  重审一次、passed 进入待确认，全部落 moderation_records/compliance_flags。

版本化模型：
- ``scripts.structure`` JSONB 是每个版本的行级快照（diff 依据）；
- episodes/scenes/script_lines 为项目当前工作副本——新版本创建时把
  episodes.script_id 整体迁移到新版本（行 ID 在 PATCH 下保持稳定）。

置信度模型：字段级置信度项（行级 speaker/content + 场级 location）
低于 :data:`LOW_CONFIDENCE_THRESHOLD`（0.75）标记 needs_manual，
剧本状态置 needs_manual_fix，全部校正完成后自动推进 pending_confirm。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id, sha256_hash
from app.graphs.prompts.compliance import build_compliance_prompt
from app.infra.event_bus import event_bus
from app.infra.model_gateway.base import GenRequest
from app.infra.model_gateway.gateway import model_gateway
from app.infra.storage import storage
from app.models.asset import MediaFile
from app.models.collab import ApprovalRequest
from app.models.project import Project
from app.models.quality import ComplianceFlag, ModerationRecord
from app.models.script import (
    Document,
    Episode,
    ParseConfidenceItem,
    Scene,
    Script,
    ScriptExportLog,
    ScriptLine,
    ScriptTemplateRef,
)
from app.repositories.script_repo import ScriptRepository
from app.schemas.script import (
    EMOTION_TAGS,
    LINE_TYPES,
    TIME_OF_DAY_DB,
    episode_out,
    line_out,
    scene_out,
    script_out,
)
from app.services.template_packs import (
    MAX_SCRIPT_VERSIONS,
    compute_deviated_fields,
    get_script_pack,
)

logger = logging.getLogger("gleamdream.script")

# ---- 上传限制 ----
#: 允许的文件后缀（docx 因 python-docx 未安装暂不支持，返回 50004）
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"txt", "md"})
#: 单文件体积上限（5MB）
MAX_FILE_BYTES: int = 5 * 1024 * 1024
#: 单文档字数上限（20 万字）
MAX_TEXT_CHARS: int = 200_000
# ---- 解析管线 ----
#: 解析分段长度（字符）
PARSE_CHUNK_CHARS: int = 8000
#: 低置信度阈值（低于该值的字段项 needs_manual=true）
LOW_CONFIDENCE_THRESHOLD: float = 0.75
#: LLM 单次调用最大输出 token
LLM_MAX_TOKENS: int = 8000
#: 解析/改写调用温度（结构化抽取用低温）
LLM_PARSE_TEMPERATURE: float = 0.2
LLM_REWRITE_TEMPERATURE: float = 0.7
#: 默认单集目标时长（秒，DB CHECK 60~180）
DEFAULT_EPISODE_DURATION: int = 90
#: 单项目集数上限（PRD F2.6 / AC-F2-08-3，>30 集返回 10001）
MAX_EPISODES: int = 30
#: 剧本导出格式（AC-F2-10-1）
SCRIPT_EXPORT_FORMATS: frozenset[str] = frozenset({"md", "docx", "txt"})
#: 台词语速基准（字符/秒）——与 graphs/nodes/deterministic 保持一致
_CHARS_PER_SECOND: float = 4.0
#: 合规审核在途判定阈值（秒）：status=compliance_review 且 updated_at 距今
#: 超过该时长视为中断残留（客户端断开取消 handler），自动回滚为 draft 放行重审
_REVIEW_STALE_SECONDS: int = 600

# 解析输出 JSON 契约说明（内嵌提示词第五段）
_PARSE_CONTRACT = """{
  "title": "剧名（文本未体现则为 null）",
  "logline": "一句话梗概（文本未体现则为 null）",
  "episodes": [
    {
      "episode_no": 1,
      "title": "集标题（可 null）",
      "summary": "本集剧情概要（可 null）",
      "hook": "结尾钩子（可 null）",
      "confidence": 0.95,
      "scenes": [
        {
          "scene_no": 1,
          "location": "场景地点",
          "time_of_day": "day|night|dawn|dusk|null",
          "int_ext": "int|ext|null",
          "summary": "本场景概要（可 null）",
          "confidence": 0.9,
          "lines": [
            {
              "line_type": "dialogue|narration|action|stage_direction",
              "speaker": "说话人（非对白为 null）",
              "content": "行原文（不改写）",
              "emotion": "calm|joy|anger|sadness|fear|surprise|tension|romance|null",
              "confidence": 0.88
            }
          ]
        }
      ]
    }
  ]
}"""

# 场级改写输出 JSON 契约说明（内嵌提示词第五段）
_REWRITE_CONTRACT = """{
  "lines": [
    {
      "line_type": "dialogue|narration|action|stage_direction",
      "speaker": "说话人（非对白为 null）",
      "content": "行内容（<=500字）",
      "emotion": "calm|joy|anger|sadness|fear|surprise|tension|romance|null"
    }
  ]
}"""


# =====================================================================
# LLM 通用工具
# =====================================================================


def _extract_json(text: str) -> Any:
    """从 LLM 输出文本中提取 JSON 对象（容错围栏与前后缀）。

    Args:
        text: 模型原始输出。

    Returns:
        解析后的 Python 对象。

    Raises:
        ValueError: 无法定位/解析 JSON 时抛出。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise ValueError("输出中未找到合法 JSON 对象")


async def _call_llm_json(
    *,
    system: str,
    user: str,
    project_id: int,
    temperature: float,
    purpose: str,
) -> dict[str, Any]:
    """执行一次「LLM 调用 → JSON 提取」，解析失败重试 1 次。

    重试时把错误信息与上次输出片段拼回提示词；重试后仍失败抛
    BizError(80001)。

    Args:
        system: 五段式系统提示词。
        user: 用户提示词。
        project_id: 项目 ID（网关日志关联）。
        temperature: 采样温度。
        purpose: 调用目的标识（日志与错误信息用，如 script_parse/rewrite）。

    Returns:
        解析后的 JSON 字典。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规。
    """
    last_error = ""
    last_output = ""
    for attempt in range(2):  # 首次 + 重试 1 次
        user_now = user
        if attempt > 0 and last_error:
            user_now = (
                f"{user}\n\n【重试要求】你上一次的输出无法解析为合法 JSON，"
                f"错误：{last_error}\n"
                f"上次输出片段：{last_output[:600]}\n"
                "请严格只输出符合契约的 JSON，不要任何解释文字。"
            )
        request = GenRequest(
            capability="llm",
            prompt=user_now,
            system_prompt=system,
            params={
                "temperature": temperature,
                "max_tokens": LLM_MAX_TOKENS,
                "response_json": True,
            },
        )
        result = await model_gateway.generate("llm", "llm_text", request, project_id=project_id)
        if not result.ok or not result.text:
            last_error = f"模型调用失败：{result.error or '空响应'}"
            last_output = ""
            logger.warning(
                "LLM 调用失败 purpose=%s project_id=%s attempt=%s err=%s",
                purpose, project_id, attempt, last_error,
            )
            continue
        try:
            payload = _extract_json(result.text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)[:300]
            last_output = result.text
            logger.warning(
                "LLM 输出解析失败 purpose=%s project_id=%s attempt=%s err=%s",
                purpose, project_id, attempt, last_error,
            )
            continue
        if not isinstance(payload, dict):
            last_error = "输出不是 JSON 对象"
            last_output = result.text
            continue
        logger.info(
            "LLM JSON 调用成功 purpose=%s project_id=%s attempt=%s model=%s",
            purpose, project_id, attempt, result.meta.get("model_key"),
        )
        return payload
    raise BizError(
        ErrorCode.MODEL_UNAVAILABLE,
        f"模型输出持续不合规（{purpose}），已重试 1 次",
        data={"last_error": last_error[:500]},
    )


def _clamp_confidence(value: Any, default: float = 0.85) -> float:
    """把 LLM 输出的置信度收敛为 [0,1] 浮点数。

    Args:
        value: 原始值（可能缺失/非法）。
        default: 缺失时的默认值。

    Returns:
        收敛后的置信度（0~1）。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, num))


def _normalize_line(raw: Any) -> dict[str, Any] | None:
    """把 LLM 输出的一行收敛为规范行结构（非法值兜底）。

    Args:
        raw: 原始行 dict（可能缺字段/非法枚举）。

    Returns:
        规范行 dict（line_type/speaker/content/emotion/confidence）；
        无 content 时返回 None（丢弃）。
    """
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content") or "").strip()
    if not content:
        return None
    line_type = str(raw.get("line_type") or "").strip()
    if line_type not in LINE_TYPES:
        # 启发式兜底：有 speaker 视为对白，否则按旁白
        line_type = "dialogue" if raw.get("speaker") else "narration"
    emotion = raw.get("emotion")
    if emotion not in EMOTION_TAGS:
        emotion = None
    speaker = raw.get("speaker")
    speaker = str(speaker).strip()[:64] if speaker else None
    if line_type != "dialogue":
        speaker = None
    return {
        "line_type": line_type,
        "speaker": speaker,
        "content": content[:2000],
        "emotion": emotion,
        "confidence": _clamp_confidence(raw.get("confidence")),
    }


def _normalize_scene(raw: Any) -> dict[str, Any] | None:
    """把 LLM 输出的一场收敛为规范场结构。

    Args:
        raw: 原始场 dict。

    Returns:
        规范场 dict（scene_no/location/time_of_day/int_ext/summary/
        confidence/lines）；无有效行时返回 None。
    """
    if not isinstance(raw, dict):
        return None
    try:
        scene_no = max(1, int(raw.get("scene_no") or 0))
    except (TypeError, ValueError):
        scene_no = 1
    lines = [
        norm
        for norm in (_normalize_line(item) for item in (raw.get("lines") or []))
        if norm is not None
    ]
    if not lines:
        return None
    time_of_day = raw.get("time_of_day")
    time_of_day = time_of_day if time_of_day in TIME_OF_DAY_DB else None
    int_ext = raw.get("int_ext")
    int_ext = int_ext if int_ext in ("int", "ext") else None
    location = str(raw.get("location") or "未标注场景").strip()[:255]
    return {
        "scene_no": scene_no,
        "location": location,
        "time_of_day": time_of_day,
        "int_ext": int_ext,
        "summary": (str(raw.get("summary") or "").strip()[:500] or None),
        "confidence": _clamp_confidence(raw.get("confidence")),
        "lines": lines,
    }


def _normalize_episode(raw: Any) -> dict[str, Any] | None:
    """把 LLM 输出的一集收敛为规范集结构。

    Args:
        raw: 原始集 dict。

    Returns:
        规范集 dict（episode_no/title/summary/hook/confidence/scenes）；
        无有效场次时返回 None。
    """
    if not isinstance(raw, dict):
        return None
    try:
        episode_no = max(1, int(raw.get("episode_no") or 0))
    except (TypeError, ValueError):
        episode_no = 1
    scenes = [
        norm
        for norm in (_normalize_scene(item) for item in (raw.get("scenes") or []))
        if norm is not None
    ]
    if not scenes:
        return None
    return {
        "episode_no": episode_no,
        "title": (str(raw.get("title") or "").strip()[:255] or None),
        "summary": (str(raw.get("summary") or "").strip()[:500] or None),
        "hook": (str(raw.get("hook") or "").strip()[:500] or None),
        "confidence": _clamp_confidence(raw.get("confidence")),
        "scenes": scenes,
    }


def _normalize_parse_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把解析 LLM 输出整体收敛为规范结构。

    Args:
        payload: 解析输出 dict。

    Returns:
        {title, logline, episodes: [规范集]}（无有效集时 episodes 为空）。
    """
    episodes = [
        norm
        for norm in (_normalize_episode(item) for item in (payload.get("episodes") or []))
        if norm is not None
    ]
    return {
        "title": (str(payload.get("title") or "").strip()[:255] or None),
        "logline": (str(payload.get("logline") or "").strip()[:300] or None),
        "episodes": episodes,
    }


# =====================================================================
# 上传与解析管线
# =====================================================================


def _split_chunks(text: str, size: int = PARSE_CHUNK_CHARS) -> list[str]:
    """把剧本文本按固定长度分段，尽量在换行处切断。

    Args:
        text: 原始文本。
        size: 目标分段长度（字符）。

    Returns:
        分段列表（每段 ≤ size，至少 1 段）。
    """
    if len(text) <= size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # 在分段尾部附近回退找换行，避免把一行切成两半
            window = text[max(start, end - 300) : end]
            nl = window.rfind("\n")
            if nl >= 0:
                end = max(start, end - 300) + nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _merge_parse_results(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """合并多段解析结果：集号冲突取并集，场序还原（按出现顺序重编号）。

    合并规则：
    - title/logline 取首个非空值；
    - 同一集号在多个分段出现 → 场列表拼接（同一集内），按原始出现顺序
      重新编号 scene_no（1..N），保证集内唯一且还原场序；
    - 集列表按 episode_no 升序。

    Args:
        parts: 各分段规范化后的解析结果列表。

    Returns:
        合并后的规范结构 {title, logline, episodes}。
    """
    merged: dict[int, dict[str, Any]] = {}
    title: str | None = None
    logline: str | None = None
    for part in parts:
        if title is None and part.get("title"):
            title = part["title"]
        if logline is None and part.get("logline"):
            logline = part["logline"]
        for ep in part.get("episodes", []):
            no = int(ep["episode_no"])
            if no in merged:
                merged[no]["scenes"].extend(ep["scenes"])
            else:
                merged[no] = copy.deepcopy(ep)
    episodes: list[dict[str, Any]] = []
    for no in sorted(merged):
        ep = merged[no]
        # 场序还原：按解析出现顺序重编号，同号相邻场保留全部行
        for idx, scene in enumerate(ep["scenes"], start=1):
            scene["scene_no"] = idx
        episodes.append(ep)
    return {"title": title, "logline": logline, "episodes": episodes}


def _build_parse_system_prompt() -> str:
    """构造解析 Agent 的五段式系统提示词。

    Returns:
        系统提示词文本。
    """
    return (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的剧本解析 Agent，一名资深短剧剧本编辑。\n"
        "你的目标是把用户上传的剧本文本抽取为结构化的「集-场-行」JSON，"
        "并为关键字段给出 0~1 的解析置信度。\n"
        "成功标准：① 集以「第 N 集」标题或叙事分段划分；② 场以「第 M 场 /"
        " 场景标题行」划分；③ 行类型判定准确（人物名+冒号 → dialogue；"
        "旁白/画外音 → narration；场景动作描述 → action；括号舞台指示 →"
        " stage_direction）。\n\n"
        "【第二段 · 项目上下文】\n"
        "- 对象：竖屏短剧剧本（每集 1-3 分钟）纯文本；\n"
        "- 解析结果将直接落库为剧本 v1，并按置信度进入人工校正流程。\n\n"
        "【第三段 · 工作区约定】\n"
        "- 只解析给定文本，不补写、不改写任何内容；\n"
        "- 无法判断的字段填 null 并给低置信度；\n"
        "- 集号/场号优先取文本标题中的数字，缺失时按出现顺序推断；\n"
        "- 每个层级给出 confidence（0~1）：越确定越高。\n\n"
        "【第四段 · 预算与边界】\n"
        "- 禁止虚构文本中不存在的集/场/行；\n"
        "- 禁止合并或拆分原文行；\n"
        "- 禁止输出除 JSON 以外的任何文字。\n\n"
        "【第五段 · 输出契约】\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释），结构如下：\n"
        f"{_PARSE_CONTRACT}"
    )


async def _parse_chunk(
    chunk: str,
    *,
    chunk_index: int,
    chunk_total: int,
    hint: str,
    project_id: int,
) -> dict[str, Any]:
    """调用 LLM 解析单个文本分段并规范化输出。

    Args:
        chunk: 本段文本。
        chunk_index: 段序号（从 1 开始）。
        chunk_total: 总段数。
        hint: 跨段提示（如「本段承接上一段，集号可能从 N 之后继续」）。
        project_id: 项目 ID（网关日志关联）。

    Returns:
        规范化后的解析结果 dict。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规。
    """
    user = (
        f"这是整份剧本文本的第 {chunk_index}/{chunk_total} 段。{hint}\n\n"
        "【本段原文】\n"
        f"{chunk}\n\n"
        "请按契约只输出 JSON。"
    )
    payload = await _call_llm_json(
        system=_build_parse_system_prompt(),
        user=user,
        project_id=project_id,
        temperature=LLM_PARSE_TEMPERATURE,
        purpose="script_parse",
    )
    return _normalize_parse_payload(payload)


def _collect_confidence_items(
    merged: dict[str, Any],
) -> list[dict[str, Any]]:
    """从合并结果收集字段级置信度项（行级 + 场级）。

    Args:
        merged: 合并后的规范解析结构。

    Returns:
        置信度项 dict 列表（未落库）：target_type/target_ref/field_name/
        confidence/extracted_value。
    """
    items: list[dict[str, Any]] = []
    for ep in merged.get("episodes", []):
        ep_no = ep["episode_no"]
        for scene in ep["scenes"]:
            sc_no = scene["scene_no"]
            items.append(
                {
                    "target_type": "scene",
                    "target_ref": f"episode_{ep_no}.scene_{sc_no}",
                    "field_name": "location",
                    "confidence": _clamp_confidence(scene.get("confidence")),
                    "extracted_value": scene.get("location"),
                }
            )
            for idx, line in enumerate(scene["lines"], start=1):
                items.append(
                    {
                        "target_type": "line",
                        "target_ref": f"episode_{ep_no}.scene_{sc_no}.line_{idx}",
                        "field_name": "speaker" if line["line_type"] == "dialogue" else "content",
                        "confidence": _clamp_confidence(line.get("confidence")),
                        "extracted_value": line["speaker"] or line["content"],
                    }
                )
    return items


def _structure_from_merged(merged: dict[str, Any]) -> dict[str, Any]:
    """把合并结果转换为 scripts.structure JSONB 形态（无置信度字段）。

    Args:
        merged: 合并后的规范解析结构。

    Returns:
        {title, logline, genre, characters, episodes} 结构化树。
    """
    return {
        "title": merged.get("title"),
        "logline": merged.get("logline"),
        "genre": None,
        "characters": [],
        "episodes": [
            {
                "episode_no": ep["episode_no"],
                "title": ep.get("title"),
                "summary": ep.get("summary"),
                "hook": ep.get("hook"),
                "scenes": [
                    {
                        "scene_no": scene["scene_no"],
                        "location": scene["location"],
                        "time_of_day": scene["time_of_day"],
                        "int_ext": scene["int_ext"],
                        "summary": scene["summary"],
                        "lines": [
                            {
                                "line_type": line["line_type"],
                                "speaker": line["speaker"],
                                "content": line["content"],
                                "emotion": line["emotion"],
                            }
                            for line in scene["lines"]
                        ],
                    }
                    for scene in ep["scenes"]
                ],
            }
            for ep in merged.get("episodes", [])
        ],
    }


def _count_words(structure: dict[str, Any]) -> int:
    """统计结构化剧本的总字数（全部行 content 长度之和）。

    Args:
        structure: 结构化剧本树。

    Returns:
        总字数（≥0）。
    """
    total = 0
    for ep in structure.get("episodes", []):
        for scene in ep.get("scenes", []):
            for line in scene.get("lines", []):
                total += len(str(line.get("content") or ""))
    return total


async def _persist_parsed_script(
    session: AsyncSession, document: Document, merged: dict[str, Any]
) -> Script:
    """把合并后的解析结果落库为剧本 v1 与全部子表/置信项。

    Args:
        session: 异步数据库会话（由调用方提交）。
        document: 来源文档实例。
        merged: 合并后的规范解析结构。

    Returns:
        新建的剧本 v1 实例（status 已按低置信项判定）。

    Raises:
        无（调用方保证 merged 至少含一个有效集）。
    """
    structure = _structure_from_merged(merged)
    items = _collect_confidence_items(merged)
    has_manual = any(item["confidence"] < LOW_CONFIDENCE_THRESHOLD for item in items)
    # 修复（测试报告 #13）：version_no 不再固定为 1——同项目重复上传时
    # 撞 uq_scripts_project_id_version_no 唯一约束导致解析崩溃。改为取当前
    # 项目最大版本号 +1（无历史剧本时为 1）。
    max_version = (
        await session.execute(
            select(func.max(Script.version_no)).where(
                Script.project_id == int(document.project_id)
            )
        )
    ).scalar()
    next_version = int(max_version or 0) + 1
    script = Script(
        project_id=int(document.project_id),
        version_no=next_version,
        source_type="uploaded",
        document_id=int(document.id),
        status="needs_manual_fix" if has_manual else "pending_confirm",
        title=structure.get("title"),
        logline=structure.get("logline"),
        genre=None,
        total_episodes=len(structure["episodes"]),
        total_words=_count_words(structure),
        structure=structure,
        created_by=document.uploader_user_id,
    )
    session.add(script)
    await session.flush()

    for order, ep in enumerate(structure["episodes"]):
        episode = Episode(
            script_id=int(script.id),
            project_id=int(document.project_id),
            episode_no=int(ep["episode_no"]),
            title=ep.get("title"),
            synopsis=ep.get("summary"),
            target_duration_seconds=DEFAULT_EPISODE_DURATION,
            hook_text=ep.get("hook"),
            order_index=order,
            status="draft",
        )
        session.add(episode)
        await session.flush()
        for sc_order, sc in enumerate(ep["scenes"]):
            scene = Scene(
                episode_id=int(episode.id),
                project_id=int(document.project_id),
                scene_no=int(sc["scene_no"]),
                location=sc["location"],
                time_of_day=sc["time_of_day"],
                int_ext=sc["int_ext"],
                summary=sc.get("summary"),
                order_index=sc_order,
            )
            session.add(scene)
            await session.flush()
            for ln_order, ln in enumerate(sc["lines"]):
                session.add(
                    ScriptLine(
                        scene_id=int(scene.id),
                        project_id=int(document.project_id),
                        line_no=ln_order + 1,
                        line_type=ln["line_type"],
                        speaker_name=ln["speaker"],
                        content=ln["content"],
                        emotion_tag=ln["emotion"],
                        order_index=ln_order,
                    )
                )

    for item in items:
        needs_manual = item["confidence"] < LOW_CONFIDENCE_THRESHOLD
        session.add(
            ParseConfidenceItem(
                script_id=int(script.id),
                document_id=int(document.id),
                target_type=item["target_type"],
                target_ref=item["target_ref"],
                field_name=item["field_name"],
                confidence=Decimal(str(round(item["confidence"], 3))),
                extracted_value=item.get("extracted_value"),
                needs_manual=needs_manual,
            )
        )

    # P0 修复：解析完成后必须把脚本设为项目当前剧本，并生成 HITL-1 审批单。
    # 历史缺陷：上传剧本只落 scripts/episodes，不更新 projects.current_script_id、
    # 不创建 script 审批单，导致 P08 剧本确认页空态、项目详情"当前剧本"为空，
    # 后续环节只能靠"最新版本"兜底。
    project = await session.get(Project, int(document.project_id))
    if project is not None:
        project.current_script_id = int(script.id)
        if project.status in ("draft", "scripting"):
            project.status = "script_review"
        pending = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.project_id == int(document.project_id),
                    ApprovalRequest.approval_type == "script",
                    ApprovalRequest.status == "pending",
                )
            )
        ).scalars().first()
        if pending is None:
            now = datetime.now(timezone.utc)
            session.add(
                ApprovalRequest(
                    project_id=int(document.project_id),
                    approval_type="script",
                    target_type="script",
                    target_id=int(script.id),
                    status="pending",
                    requested_at=now,
                    expires_at=now + timedelta(days=7),
                    feedback_scope={"source": "upload"},
                )
            )
    return script


async def _parse_document_flow(document_id: int) -> None:
    """后台解析任务主体：parsing → 分段 LLM 解析 → 落库 → 事件。

    独立于请求会话运行（自建短会话）；任何失败把 parse_status 置
    failed 并记录 parse_error，绝不向调用方传播。

    Args:
        document_id: 待解析文档 ID。

    Raises:
        无（全部异常在内部兜底处理）。
    """
    async with async_session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None or document.parse_status not in ("pending", "failed"):
            return
        project_id = int(document.project_id)
        media = (
            await session.get(MediaFile, int(document.media_file_id))
            if document.media_file_id
            else None
        )
        document.parse_status = "parsing"
        document.parse_error = None
        await session.commit()

    try:
        if media is None:
            raise ValueError("文档缺少存储文件引用，无法解析")
        raw = storage.object_path(media.object_key).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        text = text.strip()
        if not text:
            raise ValueError("文档内容为空")

        chunks = _split_chunks(text)
        parts: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            hint = (
                "本段是文本开头。"
                if index == 1
                else "本段可能从文本中间开始/结束，仅解析本段内完整出现的集与场；"
                "无法确定集号时按出现顺序延续上一段。"
            )
            parts.append(
                await _parse_chunk(
                    chunk,
                    chunk_index=index,
                    chunk_total=len(chunks),
                    hint=hint,
                    project_id=project_id,
                )
            )

        merged = _merge_parse_results(parts)
        if not merged["episodes"]:
            raise ValueError("未能从文本中解析出任何剧集结构")

        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None:
                return
            script = await _persist_parsed_script(session, document, merged)
            document.parse_status = "parsed"
            document.parse_error = None
            await session.commit()
            script_id = int(script.id)

        await event_bus.publish(
            project_id,
            "document.parsed",
            {"document_id": document_id, "script_id": script_id, "version_no": int(script.version_no)},
        )
        logger.info(
            "文档解析完成 document_id=%s script_id=%s episodes=%s",
            document_id, script_id, len(merged["episodes"]),
        )
    except BizError as exc:
        await _mark_parse_failed(document_id, f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("文档解析失败 document_id=%s", document_id)
        # 修复（测试报告 #13）：对外仅暴露业务可读摘要，不再把 str(exc)（可能
        # 携带 SQLAlchemy 堆栈/SQL 细节）直接写入 parse_error；完整堆栈仅入日志。
        await _mark_parse_failed(document_id, _sanitize_parse_error(exc))


def _sanitize_parse_error(exc: Exception) -> str:
    """把解析异常清洗为可安全展示给用户的错误摘要。

    - ValueError：视为业务校验错误，透出原始 message（如「文档内容为空」）；
    - IntegrityError：唯一约束冲突给出重试指引（正常已被 version_no 自增规避）；
    - 其余异常：仅返回类型名，细节以日志为准，避免泄露内部实现。

    Args:
        exc: 后台解析任务抛出的异常。

    Returns:
        可写入 documents.parse_error 的单行摘要（≤500 字符）。
    """
    if isinstance(exc, ValueError):
        return str(exc)[:500]
    if isinstance(exc, IntegrityError):
        return "重复上传冲突：该项目已有相同版本的剧本，请刷新页面后重试"
    return f"解析失败（{type(exc).__name__}），请稍后重试；若持续失败请联系管理员"[:500]


async def _mark_parse_failed(document_id: int, message: str) -> None:
    """把文档标记为解析失败（独立短会话，失败仅记日志）。

    Args:
        document_id: 文档 ID。
        message: 错误摘要。

    Raises:
        无。
    """
    try:
        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None:
                return
            document.parse_status = "failed"
            document.parse_error = message[:1000]
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("标记解析失败状态异常 document_id=%s", document_id)


async def upload_script_document(
    session: AsyncSession,
    *,
    project_id: int,
    user_id: int,
    file_name: str,
    data: bytes,
) -> dict[str, Any]:
    """上传剧本文档：校验 → 落存储 → 建 documents 行 → 触发异步解析。

    校验规则：
    - 后缀仅支持 txt/md；docx 返回 50004（暂不支持，请转 txt/md）；
    - 体积 ≤5MB、字数 ≤200000，超限返回 40005。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID（ACL 由路由层校验）。
        user_id: 上传者用户 ID。
        file_name: 原文件名。
        data: 文件二进制内容。

    Returns:
        202 data：{document_id, status: "pending", file_name, parse_status}。

    Raises:
        BizError: 50004 后缀不支持/文件非法；40005 超限；80003 存储失败。
    """
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if suffix == "docx":
        raise BizError(ErrorCode.UPLOAD_FILE_INVALID, "暂不支持 docx，请转为 txt/md 后重新上传")
    if suffix not in ALLOWED_EXTENSIONS:
        raise BizError(ErrorCode.UPLOAD_FILE_INVALID, "仅支持 txt/md 剧本文档")
    if len(data) > MAX_FILE_BYTES:
        raise BizError(ErrorCode.EPISODE_LIMIT_EXCEEDED, "文件超过 5MB 上限")
    # 2026-11 QA 修复（边界）：空文件此前被受理（202），解析后产出空剧本。
    # 入口直接拒绝，避免无效文档进入解析/落库链路。
    if not data.strip():
        raise BizError(ErrorCode.UPLOAD_FILE_INVALID, "文件内容为空，请上传有效的剧本文件")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gbk", errors="replace")
    if not text.strip():
        raise BizError(ErrorCode.UPLOAD_FILE_INVALID, "文件内容为空，请上传有效的剧本文件")
    if len(text) > MAX_TEXT_CHARS:
        raise BizError(ErrorCode.EPISODE_LIMIT_EXCEEDED, "文档字数超过 20 万上限")

    object_key = f"scripts/{project_id}/{new_request_id()}.{suffix}"
    storage.save_bytes(object_key, data)

    media = MediaFile(
        owner_user_id=user_id,
        project_id=project_id,
        storage_provider="oss",
        bucket="local-storage",
        object_key=object_key,
        media_type="document",
        mime_type="text/plain; charset=utf-8",
        file_size_bytes=len(data),
        checksum_sha256=sha256_hash(text),
    )
    document = Document(
        project_id=project_id,
        uploader_user_id=user_id,
        file_name=file_name[:255],
        doc_type="txt",
        parse_status="pending",
    )
    session.add(media)
    await session.flush()
    document.media_file_id = int(media.id)
    session.add(document)
    await session.commit()

    asyncio.create_task(_parse_document_flow(int(document.id)))
    return {
        "document_id": int(document.id),
        "status": "pending",
        "file_name": document.file_name,
        "parse_status": "pending",
    }


# =====================================================================
# 剧本树查询
# =====================================================================


async def build_script_tree(session: AsyncSession, script: Script) -> dict[str, Any]:
    """构建剧本的集-场-行完整树（行从工作副本表读取）。

    Args:
        session: 请求级数据库会话。
        script: 剧本版本实例。

    Returns:
        {script: {...}, episodes: [{...scenes: [{...lines: [...]}]}]}。
    """
    repo = ScriptRepository(session)
    episodes = await repo.episodes_of(int(script.id))
    scenes = await repo.scenes_of([int(ep.id) for ep in episodes])
    lines = await repo.lines_of([int(sc.id) for sc in scenes])

    lines_by_scene: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        lines_by_scene.setdefault(int(line.scene_id), []).append(line_out(line))
    scenes_by_episode: dict[int, list[dict[str, Any]]] = {}
    for scene in scenes:
        scenes_by_episode.setdefault(int(scene.episode_id), []).append(
            scene_out(scene, lines_by_scene.get(int(scene.id), []))
        )
    return {
        "script": script_out(script),
        "episodes": [
            episode_out(ep, scenes_by_episode.get(int(ep.id), [])) for ep in episodes
        ],
    }


async def get_current_script_tree(session: AsyncSession, project_id: int) -> dict[str, Any]:
    """查询项目当前（最新版本）剧本树。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。

    Returns:
        剧本树 dict。

    Raises:
        BizError: 10002 项目尚无剧本。
    """
    repo = ScriptRepository(session)
    script = await repo.latest_script(project_id)
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，请先生成或上传")
    return await build_script_tree(session, script)


async def get_script_tree_by_id(session: AsyncSession, script_id: int) -> dict[str, Any]:
    """按剧本版本 ID 查询剧本树。

    Args:
        session: 请求级数据库会话。
        script_id: 剧本版本 ID。

    Returns:
        剧本树 dict。

    Raises:
        BizError: 10002 剧本不存在。
    """
    repo = ScriptRepository(session)
    script = await repo.get_script(script_id)
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本不存在")
    return await build_script_tree(session, script)


async def get_outline_tree(session: AsyncSession, project_id: int) -> dict[str, Any]:
    """查询项目剧本的只读集-场大纲树（每集时长与钩子，供 P07）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。

    Returns:
        {script: {...}, episodes: [{episode_no, title, target_duration_seconds,
        hook, summary, scenes: [{scene_no, location, time_of_day, int_ext,
        summary, line_count}]}]}。

    Raises:
        BizError: 10002 项目尚无剧本。
    """
    repo = ScriptRepository(session)
    script = await repo.latest_script(project_id)
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，请先生成或上传")
    episodes = await repo.episodes_of(int(script.id))
    scenes = await repo.scenes_of([int(ep.id) for ep in episodes])
    lines = await repo.lines_of([int(sc.id) for sc in scenes])
    line_count_by_scene: dict[int, int] = {}
    for line in lines:
        line_count_by_scene[int(line.scene_id)] = line_count_by_scene.get(int(line.scene_id), 0) + 1
    scenes_by_episode: dict[int, list[Scene]] = {}
    for scene in scenes:
        scenes_by_episode.setdefault(int(scene.episode_id), []).append(scene)
    return {
        "script": script_out(script),
        "episodes": [
            {
                "episode_no": int(ep.episode_no),
                "title": ep.title,
                "target_duration_seconds": ep.target_duration_seconds,
                "hook": ep.hook_text,
                "summary": ep.synopsis,
                "status": ep.status,
                "scenes": [
                    {
                        "scene_no": int(sc.scene_no),
                        "location": sc.location,
                        "time_of_day": sc.time_of_day,
                        "int_ext": sc.int_ext,
                        "summary": sc.summary,
                        "line_count": line_count_by_scene.get(int(sc.id), 0),
                    }
                    for sc in scenes_by_episode.get(int(ep.id), [])
                ],
            }
            for ep in episodes
        ],
    }


# =====================================================================
# 版本化与行级编辑
# =====================================================================


async def _create_new_version(
    session: AsyncSession,
    old_script: Script,
    *,
    new_structure: dict[str, Any],
    status: str | None = None,
) -> Script:
    """创建剧本新版本：新 scripts 行 + episodes.script_id 迁移。

    新版本复制 old_script 元数据，structure 使用调用方更新后的快照，
    total_words/total_episodes 重新统计；episodes.script_id 整体迁移到
    新版本（episodes 行 ID 不变，树查询始终指向当前版本）。

    Args:
        session: 请求级数据库会话（由调用方提交）。
        old_script: 当前剧本版本实例。
        new_structure: 更新后的结构化剧本快照。
        status: 新版本状态（缺省沿用旧版本）。

    Returns:
        新剧本版本实例（已 flush）。
    """
    repo = ScriptRepository(session)
    new_script = Script(
        project_id=int(old_script.project_id),
        version_no=await repo.next_version_no(int(old_script.project_id)),
        parent_script_id=int(old_script.id),
        source_type=old_script.source_type,
        document_id=old_script.document_id,
        status=status or old_script.status,
        title=old_script.title,
        logline=old_script.logline,
        genre=old_script.genre,
        total_episodes=len(new_structure.get("episodes", [])),
        total_words=_count_words(new_structure),
        structure=new_structure,
        created_by=old_script.created_by,
    )
    session.add(new_script)
    await session.flush()

    episodes = await repo.episodes_of(int(old_script.id))
    for episode in episodes:
        episode.script_id = int(new_script.id)
    # 本项目 session 关闭了 autoflush，必须显式 flush：否则后续
    # ``episodes_of(new_script.id)`` 查不到刚迁移的旧集，重建工作副本时
    # 会因 episode_no 唯一约束与新集冲突。
    await session.flush()
    # D32 伴生修复：新版本即当前版本——同步 current_script_id。
    # 否则续写/扩写/局部改写后，按 current_script_id 解析分集的路径
    # （分镜生成等）拿不到新版本分集，报「集号不存在」。
    project = await session.get(Project, int(old_script.project_id))
    if project is not None and project.current_script_id == int(old_script.id):
        project.current_script_id = int(new_script.id)
    return new_script


def _locate_structure_line(
    structure: dict[str, Any],
    *,
    episode_no: int,
    scene_no: int,
    order_index: int,
) -> dict[str, Any] | None:
    """在结构化剧本中按集号/场号/行序定位行字典。

    Args:
        structure: 结构化剧本树。
        episode_no: 集号。
        scene_no: 场号。
        order_index: 行在lines 数组中的下标（与 DB order_index 一致）。

    Returns:
        行 dict；未找到返回 None。
    """
    for ep in structure.get("episodes", []):
        if int(ep.get("episode_no") or 0) != episode_no:
            continue
        for scene in ep.get("scenes", []):
            if int(scene.get("scene_no") or 0) != scene_no:
                continue
            lines = scene.get("lines") or []
            if 0 <= order_index < len(lines):
                return lines[order_index]
    return None


async def patch_script_line(
    session: AsyncSession,
    *,
    line_id: int,
    content: str | None,
    speaker: str | None,
    emotion: str | None,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """局部更新台词行并创建剧本新版本（结构快照同步更新）。

    执行序列：
    1. 行 → 场 → 集 → 剧本定位（ACL 由路由层保证）；
    2. 行就地更新（行 ID 稳定，前端引用不断链）；
    3. structure 深拷贝并同步更新对应行；
    4. 创建新版本（version_no+1）+ operation_logs（diff 摘要）。

    Args:
        session: 请求级数据库会话。
        line_id: 剧本行 ID。
        content: 新内容（None 不更新）。
        speaker: 新说话人（None 不更新）。
        emotion: 新情绪标签（None 不更新）。
        user_id: 操作者用户 ID。
        request_id: 请求 ID（留痕）。

    Returns:
        {line: 更新后行, new_version: 新版本号, script_id: 新版本 ID}。

    Raises:
        BizError: 10002 行不存在；10001 emotion 非法或无可更新字段。
    """
    repo = ScriptRepository(session)
    line = await repo.get_line(line_id)
    if line is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本行不存在")
    scene = await session.get(Scene, int(line.scene_id))
    episode = await session.get(Episode, int(scene.episode_id)) if scene else None
    script = await repo.get_script(int(episode.script_id)) if episode else None
    if scene is None or episode is None or script is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本行所属结构不存在")

    if emotion is not None and emotion not in EMOTION_TAGS:
        raise BizError(ErrorCode.PARAM_INVALID, f"emotion 仅支持 {'/'.join(sorted(EMOTION_TAGS))}")
    if content is None and speaker is None and emotion is None:
        raise BizError(ErrorCode.PARAM_INVALID, "至少提供 content/speaker/emotion 之一")

    changes: dict[str, dict[str, Any]] = {}
    if content is not None and content != line.content:
        changes["content"] = {"old": line.content, "new": content}
        line.content = content
    if speaker is not None and speaker != line.speaker_name:
        changes["speaker"] = {"old": line.speaker_name, "new": speaker}
        line.speaker_name = speaker[:64]
    if emotion is not None and emotion != line.emotion_tag:
        changes["emotion"] = {"old": line.emotion_tag, "new": emotion}
        line.emotion_tag = emotion

    old_version = int(script.version_no)
    structure = copy.deepcopy(script.structure or {})
    struct_line = _locate_structure_line(
        structure,
        episode_no=int(episode.episode_no),
        scene_no=int(scene.scene_no),
        order_index=int(line.order_index),
    )
    if struct_line is not None:
        if content is not None:
            struct_line["content"] = line.content
        if speaker is not None:
            struct_line["speaker"] = line.speaker_name
        if emotion is not None:
            struct_line["emotion"] = line.emotion_tag
    new_script = await _create_new_version(session, script, new_structure=structure)
    await repo.add_operation_log(
        actor_user_id=user_id,
        project_id=int(script.project_id),
        entity_type="script_line",
        entity_id=int(line.id),
        diff={
            "summary": f"编辑台词行（第 {episode.episode_no} 集 场 {scene.scene_no}）",
            "changes": changes,
            "version_from": old_version,
            "version_to": int(new_script.version_no),
        },
        request_id=request_id,
    )
    await session.commit()
    return {
        "line": line_out(line),
        "new_version": int(new_script.version_no),
        "script_id": int(new_script.id),
    }


# =====================================================================
# 局部改写（同步简化管线）
# =====================================================================


def _build_rewrite_system_prompt() -> str:
    """构造场级改写编剧 Agent 的五段式系统提示词。

    Returns:
        系统提示词文本。
    """
    return (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的编剧 Agent，一名资深竖屏短剧编剧。\n"
        "你的目标是按用户反馈局部改写指定场次的剧本行，让该场更抓人、"
        "节奏更紧凑（单集 1-3 分钟）。\n"
        "成功标准：① 只输出该场改写后的完整行列表；② 保持人物名与前情"
        "一致；③ 行类型覆盖对白/旁白/动作（dialogue/narration/action）。\n\n"
        "【第二段 · 项目上下文】\n"
        "- 输入包含：剧名/梗概、所在集概要、本场现有行与用户反馈；\n"
        "- 改写结果将作为新版本落库，可被继续编辑。\n\n"
        "【第三段 · 工作区约定】\n"
        "- 仅改写给定场次，不涉及其他场次；\n"
        "- 行数 4~12 行，每行 content ≤500 字；\n"
        "- 用户反馈未提及的行尽量保留原意微调，不做无关发挥。\n\n"
        "【第四段 · 预算与边界】\n"
        "- 禁止：涉政、色情、暴力血腥、涉未成年人恋爱或性暗示内容；\n"
        "- 禁止：影射真实公众人物、抄袭已公映作品台词；\n"
        "- 不确定时降低表达强度而非臆测。\n\n"
        "【第五段 · 输出契约】\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释文字）：\n"
        f"{_REWRITE_CONTRACT}"
    )


def _render_scene_context(
    structure: dict[str, Any],
    *,
    episode_no: int,
    scene_no: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从结构树中渲染指定场的改写上下文。

    Args:
        structure: 结构化剧本树。
        episode_no: 集号。
        scene_no: 场号。

    Returns:
        (episode dict, scene dict)。

    Raises:
        BizError: 40002 结构中不存在该集/场。
    """
    for ep in structure.get("episodes", []):
        if int(ep.get("episode_no") or 0) != episode_no:
            continue
        for scene in ep.get("scenes", []):
            if int(scene.get("scene_no") or 0) == scene_no:
                return ep, scene
    raise BizError(
        ErrorCode.SCRIPT_STRUCTURE_INVALID,
        f"结构中不存在第 {episode_no} 集 场 {scene_no}",
    )


def _normalize_lines_contract(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把改写输出收敛为规范行列表（至少 1 行，否则视为不合规）。

    Args:
        payload: 改写 LLM 输出 dict。

    Returns:
        规范行 dict 列表。

    Raises:
        ValueError: 无有效行时抛出（触发重试）。
    """
    lines = [
        norm
        for norm in (_normalize_line(item) for item in (payload.get("lines") or []))
        if norm is not None
    ]
    if not lines:
        raise ValueError("lines 为空或全部不合规")
    return lines


async def _rewrite_scene_lines(
    *,
    project_id: int,
    structure: dict[str, Any],
    episode_no: int,
    scene_no: int,
    feedback: str,
) -> list[dict[str, Any]]:
    """调用编剧 LLM 改写单场并返回规范新行列表。

    Args:
        project_id: 项目 ID（网关日志关联）。
        structure: 当前结构化剧本树。
        episode_no: 集号。
        scene_no: 场号。
        feedback: 用户反馈原文。

    Returns:
        新行列表（规范结构）。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规。
    """
    ep, scene = _render_scene_context(structure, episode_no=episode_no, scene_no=scene_no)
    rendered: list[str] = []
    for idx, line in enumerate(scene.get("lines", []), start=1):
        speaker = line.get("speaker")
        prefix = f" {speaker}：" if speaker else " "
        rendered.append(f"{idx}. [{line.get('line_type')}]{prefix}{line.get('content')}")
    lines_text = "\n".join(rendered)
    user = (
        "【剧本信息】\n"
        f"- 剧名：{structure.get('title') or '（未命名）'}\n"
        f"- 梗概：{structure.get('logline') or '（无）'}\n"
        f"- 所在集：第 {episode_no} 集《{ep.get('title') or ''}》"
        f"（{ep.get('summary') or ''}）\n\n"
        f"【本场（场 {scene_no} · {scene.get('location')}）现有行】\n"
        f"{lines_text}\n\n"
        f"【用户反馈】\n{feedback}\n\n"
        "请按契约只输出该场改写后的 JSON（lines 数组）。"
    )
    payload = await _call_llm_json(
        system=_build_rewrite_system_prompt(),
        user=user,
        project_id=project_id,
        temperature=LLM_REWRITE_TEMPERATURE,
        purpose="script_rewrite",
    )
    return _normalize_lines_contract(payload)


async def _replace_scene_lines(
    session: AsyncSession,
    *,
    scene: Scene,
    new_lines: list[dict[str, Any]],
    project_id: int,
) -> None:
    """用新行列表替换场次下的全部 DB 行（删旧插新，行号重排）。

    Args:
        session: 请求级数据库会话（由调用方提交）。
        scene: 目标场次实例。
        new_lines: 规范新行列表。
        project_id: 项目 ID。

    Raises:
        无。
    """
    repo = ScriptRepository(session)
    await repo.delete_lines_of_scene(int(scene.id))
    # 立即 flush 使 DELETE 先于后续 INSERT 执行（SQLAlchemy unit of work
    # 默认同表 insert 先于 delete，会撞 uq_script_lines_scene_id_line_no）
    await session.flush()
    for order, line in enumerate(new_lines):
        session.add(
            ScriptLine(
                scene_id=int(scene.id),
                project_id=project_id,
                line_no=order + 1,
                line_type=line["line_type"],
                speaker_name=line["speaker"],
                content=line["content"],
                emotion_tag=line["emotion"],
                order_index=order,
            )
        )


async def rewrite_script(
    session: AsyncSession,
    *,
    project_id: int,
    episode_no: int,
    scene_no: int | None,
    feedback: str,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """局部改写（同步简化管线，不进图、不涉积分）。

    执行序列：
    1. 取项目当前版本剧本与目标集/场；
    2. 逐场调用编剧 LLM（五段式提示词 + 只输出 JSON）改写；
    3. 替换受影响场 DB 行 + 更新 structure 快照 + 新版本；
    4. operation_logs 留痕；LLM 失败抛 80001（不产生新版本）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID（ACL 由路由层保证）。
        episode_no: 目标集号。
        scene_no: 目标场号（None 改写整集全部场）。
        feedback: 改写反馈（≥5 字）。
        user_id: 操作者用户 ID。
        request_id: 请求 ID。

    Returns:
        200 data：{new_version, script_id, changed_lines: [新行 dict],
        changed_scene_count}。

    Raises:
        BizError: 10002 项目尚无剧本；40002 集号不存在；40002 场号不存在；
            80001 模型不可用或输出不合规。
    """
    repo = ScriptRepository(session)
    script = await repo.latest_script(project_id)
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，无法改写")
    structure = copy.deepcopy(script.structure or {})
    episodes = await repo.episodes_of(int(script.id))
    episode = next((ep for ep in episodes if int(ep.episode_no) == episode_no), None)
    if episode is None:
        raise BizError(
            ErrorCode.SCRIPT_STRUCTURE_INVALID,
            f"剧本中不存在第 {episode_no} 集",
        )
    scenes = await repo.scenes_of([int(episode.id)])
    if scene_no is not None:
        scenes = [sc for sc in scenes if int(sc.scene_no) == scene_no]
        if not scenes:
            raise BizError(
                ErrorCode.SCRIPT_STRUCTURE_INVALID,
                f"第 {episode_no} 集中不存在场 {scene_no}",
            )

    old_version = int(script.version_no)
    changed_lines: list[dict[str, Any]] = []
    for scene in scenes:
        new_lines = await _rewrite_scene_lines(
            project_id=project_id,
            structure=structure,
            episode_no=episode_no,
            scene_no=int(scene.scene_no),
            feedback=feedback,
        )
        await _replace_scene_lines(session, scene=scene, new_lines=new_lines, project_id=project_id)
        # 同步 structure 快照中该场的行
        for ep in structure.get("episodes", []):
            if int(ep.get("episode_no") or 0) != episode_no:
                continue
            for sc in ep.get("scenes", []):
                if int(sc.get("scene_no") or 0) != int(scene.scene_no):
                    continue
                sc["lines"] = [
                    {
                        "line_type": line["line_type"],
                        "speaker": line["speaker"],
                        "content": line["content"],
                        "emotion": line["emotion"],
                    }
                    for line in new_lines
                ]
        changed_lines.extend(new_lines)

    new_script = await _create_new_version(session, script, new_structure=structure)
    await repo.add_operation_log(
        actor_user_id=user_id,
        project_id=project_id,
        entity_type="script",
        entity_id=int(new_script.id),
        diff={
            "summary": f"AI 局部改写（第 {episode_no} 集"
            f"{f' 场 {scene_no}' if scene_no is not None else ' 全部场'}）",
            "feedback": feedback,
            "episode_no": episode_no,
            "scene_no": scene_no,
            "changed_line_count": len(changed_lines),
            "version_from": old_version,
            "version_to": int(new_script.version_no),
        },
        request_id=request_id,
    )
    await session.commit()
    return {
        "new_version": int(new_script.version_no),
        "script_id": int(new_script.id),
        "changed_lines": changed_lines,
        "changed_scene_count": len(scenes),
    }


# =====================================================================
# 版本 diff
# =====================================================================


def _iter_structure_lines(structure: dict[str, Any]) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """把结构树按 (episode_no, scene_no) 分桶收集行列表。

    Args:
        structure: 结构化剧本树。

    Returns:
        (episode_no, scene_no) → 行 dict 列表 的映射。
    """
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for ep in structure.get("episodes", []):
        ep_no = int(ep.get("episode_no") or 0)
        for scene in ep.get("scenes", []):
            sc_no = int(scene.get("scene_no") or 0)
            buckets.setdefault((ep_no, sc_no), []).extend(scene.get("lines", []))
    return buckets


async def diff_script_versions(
    session: AsyncSession,
    *,
    script: Script,
    against_version: int,
) -> dict[str, Any]:
    """按行文本对齐比对当前版本与指定历史版本的剧本结构。

    对齐策略：按 (episode_no, scene_no) 分桶，桶内按行位置 zip——
    位置相同文本不同 → changed；仅新版本有 → added；仅旧版本有 →
    removed。历史版本的行级快照取自 scripts.structure JSONB。

    Args:
        session: 请求级数据库会话。
        script: 当前剧本版本实例。
        against_version: 对照版本号。

    Returns:
        {script_id, version, against, added, removed,
        changed: [{line_id, episode_no, scene_no, old, new}]}。

    Raises:
        BizError: 10002 对照版本不存在；10001 对照版本等于当前版本。
    """
    repo = ScriptRepository(session)
    if against_version == int(script.version_no):
        raise BizError(ErrorCode.PARAM_INVALID, "对照版本不能与当前版本相同")
    old_script = await repo.script_by_version(int(script.project_id), against_version)
    if old_script is None:
        raise BizError(ErrorCode.NOT_FOUND, f"版本 {against_version} 不存在")

    old_buckets = _iter_structure_lines(old_script.structure or {})
    new_buckets = _iter_structure_lines(script.structure or {})

    # 当前行 ID 定位表：(episode_no, scene_no, 位置) → line.id
    episodes = await repo.episodes_of(int(script.id))
    scenes = await repo.scenes_of([int(ep.id) for ep in episodes])
    lines = await repo.lines_of([int(sc.id) for sc in scenes])
    ep_by_id = {int(ep.id): int(ep.episode_no) for ep in episodes}
    sc_by_id = {int(sc.id): (ep_by_id.get(int(sc.episode_id)), int(sc.scene_no)) for sc in scenes}
    line_ids: dict[tuple[int, int, int], int] = {}
    for line in lines:
        key = sc_by_id.get(int(line.scene_id))
        if key and key[0] is not None:
            line_ids[(key[0], key[1], int(line.order_index))] = int(line.id)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for bucket in sorted(set(old_buckets) | set(new_buckets)):
        old_lines = old_buckets.get(bucket, [])
        new_lines = new_buckets.get(bucket, [])
        ep_no, sc_no = bucket
        for idx in range(max(len(old_lines), len(new_lines))):
            old_item = old_lines[idx] if idx < len(old_lines) else None
            new_item = new_lines[idx] if idx < len(new_lines) else None
            if old_item is None and new_item is not None:
                added.append({
                    "episode_no": ep_no,
                    "scene_no": sc_no,
                    "line_type": new_item.get("line_type"),
                    "speaker": new_item.get("speaker"),
                    "content": new_item.get("content"),
                })
            elif old_item is not None and new_item is None:
                removed.append({
                    "episode_no": ep_no,
                    "scene_no": sc_no,
                    "line_type": old_item.get("line_type"),
                    "speaker": old_item.get("speaker"),
                    "content": old_item.get("content"),
                })
            elif str(old_item.get("content") or "") != str(new_item.get("content") or ""):
                changed.append({
                    "line_id": line_ids.get((ep_no, sc_no, idx)),
                    "episode_no": ep_no,
                    "scene_no": sc_no,
                    "old": old_item.get("content"),
                    "new": new_item.get("content"),
                })
    return {
        "script_id": int(script.id),
        "version": int(script.version_no),
        "against": int(against_version),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


# =====================================================================
# 合规送审
# =====================================================================


_FLAG_TYPES: frozenset[str] = frozenset({
    "politics", "porn", "minor", "violence", "infringement",
    "sensitive_word", "celebrity_face", "real_person_likeness",
})


def _normalize_findings(findings: Any) -> list[dict[str, Any]]:
    """把审核 LLM 输出的发现列表收敛为合规结构。

    Args:
        findings: 原始 findings 列表（可能缺字段/非法枚举）。

    Returns:
        规范 findings 列表（flag_type 非法归入 sensitive_word，
        level 非法归入 medium）。
    """
    normalized: list[dict[str, Any]] = []
    for raw in findings or []:
        if not isinstance(raw, dict):
            continue
        flag_type = raw.get("flag_type")
        if flag_type not in _FLAG_TYPES:
            flag_type = "sensitive_word"
        level = raw.get("level")
        if level not in ("low", "medium", "high", "block"):
            level = "medium"
        normalized.append(
            {
                "flag_type": flag_type,
                "level": level,
                "location": str(raw.get("location") or "").strip()[:200] or "未定位",
                "description": str(raw.get("description") or "").strip()[:500] or None,
                "suggestion": str(raw.get("suggestion") or "").strip()[:500] or None,
            }
        )
    return normalized


def _parse_finding_location(location: str) -> tuple[int, int] | None:
    """从审核发现的定位文本解析 (episode_no, scene_no)。

    Args:
        location: 定位文本（如「第 1 集 场 2 第 3 行」）。

    Returns:
        (集号, 场号)；解析不出时返回 None。
    """
    ep_match = re.search(r"第\s*(\d+)\s*集", location)
    if not ep_match:
        return None
    episode_no = int(ep_match.group(1))
    sc_match = re.search(r"(?:场|第)\s*(\d+)\s*场", location)
    if not sc_match:
        sc_match = re.search(r"场\s*(\d+)", location)
    scene_no = int(sc_match.group(1)) if sc_match else 1
    return episode_no, scene_no


async def _run_compliance_review(
    structure: dict[str, Any], *, project_id: int
) -> dict[str, Any]:
    """调用审核 LLM 对结构化剧本做合规扫描（复用 Task 6a 提示词）。

    Args:
        structure: 结构化剧本树。
        project_id: 项目 ID（网关日志关联）。

    Returns:
        {verdict: pass|flag|block, findings: [规范发现], summary}。

    Raises:
        BizError: 80001 模型不可用或输出不合规。
    """
    system, user = build_compliance_prompt(structure)
    payload = await _call_llm_json(
        system=system,
        user=user,
        project_id=project_id,
        temperature=0.1,
        purpose="script_review",
    )
    verdict = payload.get("verdict")
    if verdict not in ("pass", "flag", "block"):
        verdict = "flag"
    return {
        "verdict": verdict,
        "findings": _normalize_findings(payload.get("findings")),
        "summary": str(payload.get("summary") or "").strip()[:300] or "（无摘要）",
    }


async def _save_review_artifacts(
    session: AsyncSession,
    *,
    script: Script,
    report: dict[str, Any],
    round_no: int,
    latency_ms: int,
) -> None:
    """把一轮审核结果落库：compliance_flags + moderation_records。

    Args:
        session: 请求级数据库会话（由调用方提交）。
        script: 被审核剧本版本。
        report: 规范审核报告。
        round_no: 审核轮次（1=首审，2=弱化重审）。
        latency_ms: 本轮 LLM 调用近似耗时（网关 meta 缺省时为 0）。

    Raises:
        无。
    """
    verdict_map = {"pass": "pass", "flag": "review", "block": "block"}
    request_snapshot = {
        "script_id": int(script.id),
        "version_no": int(script.version_no),
        "round_no": round_no,
        "reviewer": "llm_compliance_agent",
    }
    response_snapshot = {
        "verdict": report["verdict"],
        "findings": report["findings"],
        "summary": report["summary"],
    }
    moderation = ModerationRecord(
        project_id=int(script.project_id),
        flag_id=None,
        provider="llm_compliance_agent",
        api_name="script.submit-review",
        verdict=verdict_map[report["verdict"]],
        request_snapshot=request_snapshot,
        response_snapshot=response_snapshot,
        cost_credits=0,
        latency_ms=max(0, int(latency_ms)),
        trace_id=new_request_id(),
    )
    session.add(moderation)
    await session.flush()

    for finding in report["findings"]:
        session.add(
            ComplianceFlag(
                project_id=int(script.project_id),
                target_type="script",
                target_id=int(script.id),
                flag_type=finding["flag_type"],
                level=finding["level"],
                position={"round_no": round_no, "location": finding["location"]},
                description=finding["description"],
                suggestion=finding["suggestion"],
                status="blocked" if report["verdict"] == "block" else "open",
                source="model",
            )
        )


async def _ensure_script_approval(
    session: AsyncSession,
    script: Script,
) -> ApprovalRequest | None:
    """确保当前剧本版本存在待确认的 HITL-1 审批单（幂等）。

    手动「提交审核」此前只执行合规审核，不创建审批单，导致剧本页提示
    “前往审批台确认”而审批台没有任何待办。本方法补齐这一环：
    - 仅对 draft / scripting / script_review 阶段的剧本创建审批单，避免
      已进入资产及后续环节的项目被回退；
    - 已有同项目 pending 的 script 审批单时复用并指向最新剧本版本，
      刷新有效期，不重复建单。

    Args:
        session: 请求级数据库会话（调用方负责 commit）。
        script: 合规审核通过、状态为 pending_confirm 的剧本版本。

    Returns:
        审批单实例；项目不存在或当前阶段不应创建时为 None。

    Raises:
        无。
    """
    project = await session.get(Project, int(script.project_id))
    if project is None:
        return None
    # 已进入资产/分镜/生产阶段的项目不因重新送审而回退流程
    if project.status not in ("draft", "scripting", "script_review"):
        return None

    project.current_script_id = int(script.id)
    if project.status in ("draft", "scripting"):
        project.status = "script_review"

    pending = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.project_id == int(script.project_id),
                ApprovalRequest.approval_type == "script",
                ApprovalRequest.status == "pending",
            )
        )
    ).scalars().first()
    now = datetime.now(timezone.utc)
    if pending is None:
        pending = ApprovalRequest(
            project_id=int(script.project_id),
            approval_type="script",
            target_type="script",
            target_id=int(script.id),
            status="pending",
            requested_at=now,
            expires_at=now + timedelta(days=7),
            feedback_scope={"source": "manual_review"},
        )
        session.add(pending)
        await session.flush()
    else:
        # 同一项目只保留一张待审单：指向最新剧本版本并刷新有效期
        pending.target_type = "script"
        pending.target_id = int(script.id)
        pending.requested_at = now
        pending.expires_at = now + timedelta(days=7)
        pending.feedback_scope = {"source": "manual_review"}
    return pending


async def submit_script_review(
    session: AsyncSession,
    *,
    script_id: int,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """提交剧本合规审核（LLM 审核 Agent，同步执行）。

    执行序列：
    1. 剧本置 compliance_review（失败兜底回滚状态）；
    2. 首审（复用 Task 6a 审核提示词）：
       - pass → pending_confirm；
       - block → rejected + compliance_flags（blocked）；
       - flag → 自动弱化重审一次：按发现定位受影响场，逐场以建议为
         反馈调用编剧改写（产生新版本），再用新结构重审；
    3. 重审 block → rejected；pass/flag → 末轮降级放行 pending_confirm；
    4. 每轮均落 moderation_records + compliance_flags。

    Args:
        session: 请求级数据库会话。
        script_id: 剧本版本 ID。
        user_id: 操作者用户 ID。
        request_id: 请求 ID。

    Returns:
        {result: passed|flag|block, script_id, version_no, script_status,
        flags: [规范发现], softened: bool, approval_id: int|None}；
        pending_confirm 时 approval_id 为新建/复用的 HITL-1 审批单 ID。

    Raises:
        BizError: 10002 剧本不存在；40001 剧本正在合规审核中（重复提交守卫）；
            80001 模型不可用（状态回滚为提交前）。
    """
    repo = ScriptRepository(session)
    script = await repo.get_script(script_id)
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本不存在")
    if script.status == "compliance_review":
        # 审核进行中守卫：前端超时/重复点击时避免并发双重审核（重复 LLM 调用与重复版本）。
        # updated_at 距今 10 分钟内的视为审核在途直接拒绝；超过 10 分钟视为
        # 中断残留（客户端断开导致 handler 被 uvicorn 取消、except 回滚未执行），
        # 自动回滚为 draft 放行重新提交。
        updated_at = script.updated_at
        now = datetime.now(timezone.utc)
        stale = (
            updated_at is None
            or updated_at.tzinfo is None
            or (now - updated_at).total_seconds() > _REVIEW_STALE_SECONDS
        )
        if not stale:
            raise BizError(
                ErrorCode.PROJECT_STATE_INVALID,
                "剧本正在合规审核中，请稍候刷新页面查看审核结果",
            )
        script.status = "draft"
        await session.commit()
    # 已通过合规审核（pending_confirm）但审批单缺失时：无需重复调用 LLM，
    # 直接幂等补齐 HITL-1 审批单，用于修复“剧本已待确认但审批台无待办”的旧数据。
    # 若已有 pending 审批单，则保持原行为（重新执行合规审核），避免改变既有语义。
    if script.status == "pending_confirm":
        project = await session.get(Project, int(script.project_id))
        pending = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.project_id == int(script.project_id),
                    ApprovalRequest.approval_type == "script",
                    ApprovalRequest.status == "pending",
                )
            )
        ).scalars().first()
        if (
            project is not None
            and project.status in ("draft", "scripting", "script_review")
            and pending is None
        ):
            approval = await _ensure_script_approval(session, script)
            await session.commit()
            return {
                "result": "passed",
                "script_id": int(script.id),
                "version_no": int(script.version_no),
                "script_status": script.status,
                "flags": [],
                "softened": False,
                "approval_id": int(approval.id) if approval is not None else None,
            }
    original_status = script.status
    script.status = "compliance_review"
    await session.commit()

    try:
        report = await _run_compliance_review(script.structure or {}, project_id=int(script.project_id))
        await _save_review_artifacts(session, script=script, report=report, round_no=1, latency_ms=0)
        softened = False

        if report["verdict"] == "flag":
            # 自动弱化重审一次：定位受影响场并按建议改写（失败则跳过弱化直接重审）
            targets: list[tuple[int, int]] = []
            for finding in report["findings"]:
                loc = _parse_finding_location(finding["location"])
                if loc is not None and loc not in targets:
                    targets.append(loc)
            structure = copy.deepcopy(script.structure or {})
            if targets:
                # 预加载当前版本的集/场，按 (episode_no, scene_no) 建索引
                all_episodes = await repo.episodes_of(int(script.id))
                all_scenes = await repo.scenes_of([int(ep.id) for ep in all_episodes])
                ep_no_by_id = {int(ep.id): int(ep.episode_no) for ep in all_episodes}
                scene_by_key = {
                    (ep_no_by_id.get(int(sc.episode_id)), int(sc.scene_no)): sc
                    for sc in all_scenes
                }
                soften_feedback = (
                    "内容安全弱化改写，要求："
                    + "; ".join(
                        filter(None, (f["suggestion"] for f in report["findings"]))
                    )[:1500]
                )
                for episode_no, scene_no in targets:
                    try:
                        new_lines = await _rewrite_scene_lines(
                            project_id=int(script.project_id),
                            structure=structure,
                            episode_no=episode_no,
                            scene_no=scene_no,
                            feedback=soften_feedback,
                        )
                    except BizError:
                        continue  # 单场弱化失败不阻断重审
                    for ep in structure.get("episodes", []):
                        if int(ep.get("episode_no") or 0) != episode_no:
                            continue
                        for sc in ep.get("scenes", []):
                            if int(sc.get("scene_no") or 0) != scene_no:
                                continue
                            sc["lines"] = [
                                {
                                    "line_type": line["line_type"],
                                    "speaker": line["speaker"],
                                    "content": line["content"],
                                    "emotion": line["emotion"],
                                }
                                for line in new_lines
                            ]
                    target_scene = scene_by_key.get((episode_no, scene_no))
                    if target_scene is not None:
                        await _replace_scene_lines(
                            session,
                            scene=target_scene,
                            new_lines=new_lines,
                            project_id=int(script.project_id),
                        )
                script = await _create_new_version(session, script, new_structure=structure)
                softened = True

            report2 = await _run_compliance_review(
                script.structure or {}, project_id=int(script.project_id)
            )
            await _save_review_artifacts(
                session, script=script, report=report2, round_no=2, latency_ms=0
            )
            # 末轮降级放行：block 才拒绝，pass/flag 均放行（记录 review）
            if report2["verdict"] == "block":
                script.status = "rejected"
                final_result = "block"
                final_flags = report2["findings"]
            else:
                script.status = "pending_confirm"
                final_result = "passed"
                final_flags = report2["findings"]
        elif report["verdict"] == "block":
            script.status = "rejected"
            final_result = "block"
            final_flags = report["findings"]
        else:
            script.status = "pending_confirm"
            final_result = "passed"
            final_flags = report["findings"]

        await repo.add_operation_log(
            actor_user_id=user_id,
            project_id=int(script.project_id),
            entity_type="script",
            entity_id=int(script.id),
            diff={
                "summary": "提交合规审核",
                "first_verdict": report["verdict"],
                "softened": softened,
                "final_status": script.status,
                "version_from": int(script.version_no),
            },
            request_id=request_id,
        )
        # 审核通过（pending_confirm）时同步生成 HITL-1 审批单：
        # 审批台据此出现待办，用户提交后跳转即可继续确认与后续流转。
        approval = (
            await _ensure_script_approval(session, script)
            if script.status == "pending_confirm"
            else None
        )
        await session.commit()
        return {
            "result": final_result,
            "script_id": int(script.id),
            "version_no": int(script.version_no),
            "script_status": script.status,
            "flags": final_flags,
            "softened": softened,
            "approval_id": int(approval.id) if approval is not None else None,
        }
    except BizError:
        # LLM 失败：回滚本轮状态变更后向上抛 80001
        await session.rollback()
        fresh = await repo.get_script(script_id)
        if fresh is not None:
            fresh.status = original_status
            await session.commit()
        raise


# =====================================================================
# 置信度人工校正
# =====================================================================


async def patch_confidence_item(
    session: AsyncSession,
    *,
    item_id: int,
    corrected_value: str,
    user_id: int,
) -> dict[str, Any]:
    """人工校正经解析置信项；全部处理完后推进剧本状态。

    执行序列：
    1. 校正值写入 item（needs_manual=false，记录校正人/时间）；
    2. 若该文档已无待人工项 → 关联剧本（needs_manual_fix 时）推进
       pending_confirm。

    Args:
        session: 请求级数据库会话。
        item_id: 置信度项 ID。
        corrected_value: 人工校正值。
        user_id: 校正人用户 ID。

    Returns:
        {item: 校正后置信项, script_status: 关联剧本当前状态（可空）}。

    Raises:
        BizError: 10002 置信项不存在。
    """
    repo = ScriptRepository(session)
    item = await repo.get_confidence_item(item_id)
    if item is None:
        raise BizError(ErrorCode.NOT_FOUND, "置信度项不存在")
    item.corrected_value = corrected_value[:2000]
    item.needs_manual = False
    item.reviewed_by = user_id
    item.reviewed_at = datetime.now(timezone.utc)

    script_status: str | None = None
    if item.document_id is not None:
        pending = await repo.manual_pending_count(int(item.document_id))
        script = await repo.latest_script_by_document(int(item.document_id))
        if pending == 0 and script is not None and script.status == "needs_manual_fix":
            script.status = "pending_confirm"
        if script is not None:
            script_status = script.status
    await session.commit()
    return {
        "item": {
            "id": int(item.id),
            "target_ref": item.target_ref,
            "field_name": item.field_name,
            "confidence": float(item.confidence),
            "extracted_value": item.extracted_value,
            "corrected_value": item.corrected_value,
            "needs_manual": bool(item.needs_manual),
            "reviewed_at": item.reviewed_at.isoformat() if item.reviewed_at else None,
        },
        "script_status": script_status,
    }


# =====================================================================
# V4.0 剧本版本管理（US-F2-11 / AC-F2-11-1~3）
# =====================================================================


async def list_script_versions(
    session: AsyncSession, project_id: int
) -> dict[str, Any]:
    """列项目的剧本版本（供版本对比与「设为当前」入口）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。

    Returns:
        ``{items: [版本摘要...], total}``，版本按 version_no 倒序。

    Raises:
        BizError: 10002 项目尚无剧本。
    """
    repo = ScriptRepository(session)
    versions = await repo.list_versions(project_id)
    if not versions:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本版本")
    latest_id = int(versions[0].id)
    return {
        "items": [
            {
                **script_out(script),
                "is_current": int(script.id) == latest_id,
            }
            for script in versions
        ],
        "total": len(versions),
        "version_limit": MAX_SCRIPT_VERSIONS,
    }


async def _rebuild_working_copy(
    session: AsyncSession, new_script: Script, structure: dict[str, Any]
) -> None:
    """按 structure 快照重建项目工作副本（episodes/scenes/script_lines）。

    用途：历史版本恢复时把旧版本内容还原为可编辑的当前版本。旧工作副本
    （原先挂在最新版本下的 episodes 及其级联子行）被删除，随后按 structure
    重新创建——**行 ID 会变化**，前端需重新拉取剧本树（恢复属显式操作）。

    Args:
        session: 请求级数据库会话（调用方提交）。
        new_script: 新剧本版本实例。
        structure: 目标结构化剧本树。
    """
    from sqlalchemy import delete

    repo = ScriptRepository(session)
    # 调用方（恢复/改写等）已通过 _create_new_version 把旧工作副本的
    # episodes.script_id 整体迁移到新版本，因此这里必须按 new_script.id
    # 查询，否则查不到旧行、随后按快照重建会与既有 episode_no 撞唯一约束。
    old_episodes = await repo.episodes_of(int(new_script.id))
    # D32 修复：episodes 行被 storyboards/shots 以 ondelete=CASCADE 引用，
    # 直接删除会把已生成的分镜板与镜头（含渲染成果）一并级联删除。
    # 这里先记录旧集号 → 旧 ID 映射，待新 episode 行创建后把分镜迁移
    # 重挂到同集号的新 episode 上，再删除旧行（此时已无挂载，CASCADE 安全）。
    old_map: dict[int, int] = {
        int(ep.episode_no): int(ep.id) for ep in old_episodes
    }
    project_id = int(new_script.project_id)
    new_map: dict[int, int] = {}
    # 旧行与新行同属一个 script 版本，episode_no 唯一约束会阻止「同号新建」。
    # 先给旧行加一个大偏移腾出号段，待新行创建并迁移分镜后再删除旧行。
    if old_map:
        await session.execute(
            text("UPDATE episodes SET episode_no = episode_no + 100000 WHERE id = ANY(:ids)"),
            {"ids": list(old_map.values())},
        )
    for ep_index, ep in enumerate(structure.get("episodes", [])):
        episode_no = int(ep.get("episode_no") or ep_index + 1)
        summary = ep.get("summary") or ep.get("synopsis")
        hook = ep.get("hook") or ep.get("hook_suggestion")
        episode = Episode(
            script_id=int(new_script.id),
            project_id=project_id,
            episode_no=episode_no,
            title=ep.get("title"),
            synopsis=summary,
            target_duration_seconds=(
                int(ep["est_duration_sec"]) if ep.get("est_duration_sec") else DEFAULT_EPISODE_DURATION
            ),
            hook_text=hook,
            hook_suggestion=(str(hook)[:50] if hook else None),
            est_duration_sec=(
                int(ep["est_duration_sec"]) if ep.get("est_duration_sec") else DEFAULT_EPISODE_DURATION
            ),
            order_index=ep_index,
            status="draft",
        )
        session.add(episode)
        await session.flush()
        new_map[episode_no] = int(episode.id)
        for sc_index, sc in enumerate(ep.get("scenes", [])):
            scene = Scene(
                episode_id=int(episode.id),
                project_id=project_id,
                scene_no=int(sc.get("scene_no") or sc_index + 1),
                title=sc.get("title"),
                location=sc.get("location"),
                time_of_day=(
                    sc.get("time_of_day") if sc.get("time_of_day") in TIME_OF_DAY_DB else None
                ),
                int_ext=sc.get("int_ext") if sc.get("int_ext") in ("int", "ext") else None,
                summary=sc.get("summary"),
                order_index=sc_index,
            )
            session.add(scene)
            await session.flush()
            for line_index, line in enumerate(sc.get("lines", [])):
                line_type = (
                    line.get("line_type") if line.get("line_type") in LINE_TYPES else "action"
                )
                emotion = line.get("emotion")
                session.add(
                    ScriptLine(
                        scene_id=int(scene.id),
                        project_id=project_id,
                        line_no=line_index + 1,
                        line_type=line_type,
                        speaker_name=(str(line.get("speaker"))[:64] if line.get("speaker") else None),
                        content=str(line.get("content") or ""),
                        emotion_tag=emotion if emotion in EMOTION_TAGS else None,
                        order_index=line_index,
                    )
                )
    # D32 修复（续）：把旧 episode 上的分镜板与镜头重挂到同集号新 episode，
    # 再删除旧行（此时已无 storyboards/shots 挂载，CASCADE 不会误删成果）
    migrated = 0
    for old_no, old_id in old_map.items():
        new_id = new_map.get(old_no)
        if new_id is None:
            continue
        result = await session.execute(
            text("UPDATE storyboards SET episode_id = :nid WHERE episode_id = :oid"),
            {"nid": new_id, "oid": old_id},
        )
        migrated += result.rowcount or 0
        await session.execute(
            text("UPDATE shots SET episode_id = :nid WHERE episode_id = :oid"),
            {"nid": new_id, "oid": old_id},
        )
    if old_episodes:
        await session.execute(
            delete(Episode).where(Episode.id.in_([int(ep.id) for ep in old_episodes]))
        )
        if migrated:
            logger.info(
                "工作副本重建：分镜成果已迁移 project_id=%s storyboards_migrated=%s",
                project_id, migrated,
            )
    await session.flush()


async def restore_script_version(
    session: AsyncSession,
    *,
    project_id: int,
    version_id: int,
    user_id: int,
    request_id: str,
    label: str | None = None,
) -> dict[str, Any]:
    """把历史版本「设为当前」：以该版本内容生成新版本（AC-F2-11-2）。

    版本链不破坏：新版本 ``parent_script_id`` 指向当前最新版本，原历史
    版本仍可访问；新版本号 = 当前最大版本号 + 1。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        version_id: 被恢复的历史版本 ID。
        user_id: 操作人用户 ID。
        request_id: 请求 ID（留痕）。
        label: 新版本命名备注（缺省「恢复自 vN」）。

    Returns:
        ``{script: 新版本摘要, restored_from: 来源版本号}``。

    Raises:
        BizError: 10002 版本不存在或不属于该项目；40001 已是最新版本。
    """
    repo = ScriptRepository(session)
    target = await repo.get_script(version_id)
    if target is None or int(target.project_id) != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "剧本版本不存在")
    current = await repo.latest_script(project_id)
    if current is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本版本")
    if int(current.id) == int(target.id):
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "该版本已是当前版本")

    structure = copy.deepcopy(target.structure or {})
    new_script = await _create_new_version(
        session, current, new_structure=structure,
        status="draft" if current.status == "locked" else current.status,
    )
    new_script.version_label = (label or f"恢复自 v{int(target.version_no)}")[:50]
    new_script.confidence_overall = target.confidence_overall
    await _rebuild_working_copy(session, new_script, structure)

    await repo.add_operation_log(
        actor_user_id=user_id,
        project_id=project_id,
        entity_type="script_version",
        entity_id=int(new_script.id),
        diff={
            "summary": f"恢复历史版本 v{int(target.version_no)} 为当前",
            "restored_from": int(target.version_no),
            "version_from": int(current.version_no),
            "version_to": int(new_script.version_no),
        },
        request_id=request_id,
    )
    await _enforce_version_limit(session, project_id)
    await session.commit()
    return {
        "script": script_out(new_script),
        "restored_from": int(target.version_no),
    }


async def _enforce_version_limit(session: AsyncSession, project_id: int) -> None:
    """版本数超上限时按时间淘汰最旧版本（AC-F2-11-3）。

    仅淘汰**非当前**版本；被淘汰版本若仍被其他表弱引用则由 FK 置空。

    Args:
        session: 请求级数据库会话（调用方提交）。
    """
    repo = ScriptRepository(session)
    total = await repo.count_versions(project_id)
    while total > MAX_SCRIPT_VERSIONS:
        oldest = await repo.oldest_version(project_id)
        if oldest is None:
            break
        latest = await repo.latest_script(project_id)
        if latest is not None and int(latest.id) == int(oldest.id):
            break
        await session.delete(oldest)
        await session.flush()
        total -= 1
        logger.info(
            "剧本版本超限淘汰 project_id=%s 淘汰版本 v%s", project_id, oldest.version_no
        )


async def update_version_label(
    session: AsyncSession,
    *,
    project_id: int,
    version_id: int,
    label: str,
    user_id: int,
) -> dict[str, Any]:
    """给剧本版本命名备注（AC-F2-11-1 版本列表可辨识）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        version_id: 版本 ID。
        label: 备注（≤50 字）。
        user_id: 操作人用户 ID（预留审计）。

    Returns:
        ``{script: 版本摘要}``。

    Raises:
        BizError: 10002 版本不存在或不属于该项目。
    """
    script = await ScriptRepository(session).get_script(version_id)
    if script is None or int(script.project_id) != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "剧本版本不存在")
    script.version_label = label[:50]
    await session.commit()
    return {"script": script_out(script)}


# =====================================================================
# V4.0 剧本导出（US-F2-10 / AC-F2-10-1~3）
# =====================================================================


def _render_script_markdown(
    script: Script, structure: dict[str, Any], low_confidence_count: int
) -> str:
    """把结构化剧本渲染为 Markdown 文本。

    Args:
        script: 剧本版本实例。
        structure: 结构化剧本树。
        low_confidence_count: 待人工校正字段数（>0 时首行插入提示）。

    Returns:
        Markdown 文本。
    """
    lines: list[str] = []
    if low_confidence_count > 0:
        lines.append(f"⚠ 本版本含待人工校正字段（{low_confidence_count} 处）")
        lines.append("")
    lines.append(f"# {script.title or '未命名剧本'}")
    lines.append("")
    if script.logline:
        lines.append(f"> {script.logline}")
        lines.append("")
    lines.append(f"- 版本：v{int(script.version_no)}　集数：{int(script.total_episodes)}　字数：{int(script.total_words)}")
    lines.append("")
    for ep in structure.get("episodes", []):
        lines.append(f"## 第 {ep.get('episode_no')} 集 {ep.get('title') or ''}".rstrip())
        lines.append("")
        if ep.get("summary"):
            lines.append(f"*{ep['summary']}*")
            lines.append("")
        if ep.get("hook"):
            lines.append(f"**结尾钩子**：{ep['hook']}")
            lines.append("")
        for scene in ep.get("scenes", []):
            header = f"### 第 {scene.get('scene_no')} 场"
            if scene.get("location"):
                header += f"｜{scene['location']}"
            if scene.get("int_ext"):
                header += f"｜{'内景' if scene['int_ext'] == 'int' else '外景'}"
            if scene.get("time_of_day"):
                header += f"｜{scene['time_of_day']}"
            lines.append(header)
            lines.append("")
            for line in scene.get("lines", []):
                line_type = line.get("line_type")
                speaker = line.get("speaker")
                content = line.get("content") or ""
                if line_type == "dialogue":
                    lines.append(f"**{speaker or '旁白'}**：{content}")
                elif line_type == "narration":
                    lines.append(f"（旁白）{content}")
                elif line_type == "stage_direction":
                    lines.append(f"［舞台提示］{content}")
                else:
                    lines.append(f"〔动作〕{content}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_script_plain(
    script: Script, structure: dict[str, Any], low_confidence_count: int
) -> str:
    """把结构化剧本渲染为纯文本（txt 导出）。

    Args:
        script: 剧本版本实例。
        structure: 结构化剧本树。
        low_confidence_count: 待人工校正字段数。

    Returns:
        纯文本内容。
    """
    lines: list[str] = []
    if low_confidence_count > 0:
        lines.append(f"⚠ 本版本含待人工校正字段（{low_confidence_count} 处）")
        lines.append("")
    lines.append(f"{script.title or '未命名剧本'}（v{int(script.version_no)}）")
    if script.logline:
        lines.append(script.logline)
    lines.append("=" * 32)
    for ep in structure.get("episodes", []):
        lines.append("")
        lines.append(f"第 {ep.get('episode_no')} 集 {ep.get('title') or ''}".rstrip())
        for scene in ep.get("scenes", []):
            lines.append("")
            lines.append(
                f"第 {scene.get('scene_no')} 场 {scene.get('location') or ''} "
                f"{'内景' if scene.get('int_ext') == 'int' else ''}"
                f"{'外景' if scene.get('int_ext') == 'ext' else ''}".rstrip()
            )
            for line in scene.get("lines", []):
                speaker = line.get("speaker") or ""
                content = line.get("content") or ""
                if line.get("line_type") == "dialogue":
                    lines.append(f"  {speaker}：{content}")
                else:
                    lines.append(f"  （{line.get('line_type')}）{content}")
    return "\n".join(lines).strip() + "\n"


async def export_script(
    session: AsyncSession,
    *,
    project_id: int,
    version_id: int | None,
    fmt: str,
    user_id: int,
) -> tuple[bytes, str, str]:
    """导出剧本为 md/docx/txt（AC-F2-10-1~3，免费）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        version_id: 目标版本 ID；None 表示当前（最新）版本。
        fmt: 导出格式 md/docx/txt。
        user_id: 导出用户 ID（留痕）。

    Returns:
        ``(文件字节, 文件名, MIME 类型)``。

    Raises:
        BizError: 10001 格式非法；10002 项目无剧本/版本不存在；70002 生成中断。
    """
    if fmt not in SCRIPT_EXPORT_FORMATS:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"导出格式仅支持 {'/'.join(sorted(SCRIPT_EXPORT_FORMATS))}",
            error_detail="script_export_format_invalid",
        )
    repo = ScriptRepository(session)
    script = (
        await repo.get_script(version_id) if version_id is not None
        else await repo.latest_script(project_id)
    )
    if script is None or int(script.project_id) != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "剧本不存在，无法导出")

    low_confidence = 0
    if script.status == "needs_manual_fix":
        pending = (
            await session.execute(
                select(ParseConfidenceItem).where(
                    ParseConfidenceItem.script_id == int(script.id),
                    ParseConfidenceItem.needs_manual.is_(True),
                )
            )
        ).scalars().all()
        low_confidence = len(pending)

    structure = script.structure or {}
    base_name = f"{(script.title or '剧本').strip()}-剧本-v{int(script.version_no)}"
    safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", base_name)[:180]

    if fmt == "md":
        content = _render_script_markdown(script, structure, low_confidence)
        payload = content.encode("utf-8")
        mime = "text/markdown; charset=utf-8"
        file_name = f"{safe_name}.md"
    elif fmt == "txt":
        content = _render_script_plain(script, structure, low_confidence)
        payload = content.encode("utf-8")
        mime = "text/plain; charset=utf-8"
        file_name = f"{safe_name}.txt"
    else:  # docx
        content = _render_script_markdown(script, structure, low_confidence)
        payload = _markdown_to_docx_bytes(content, script.title or "剧本")
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        file_name = f"{safe_name}.docx"

    session.add(
        ScriptExportLog(
            user_id=user_id,
            project_id=project_id,
            version_id=int(script.id),
            format=fmt,
            file_name=file_name,
            size_bytes=len(payload),
            low_confidence_count=low_confidence,
        )
    )
    await session.commit()
    return payload, file_name, mime


def _markdown_to_docx_bytes(markdown: str, title: str) -> bytes:
    """把 Markdown 渲染结果转为最小可用 DOCX 字节流。

    python-docx 未列入依赖，故按 OOXML 最小结构手工拼装
    （word/document.xml + 关系 + 内容类型），保证 Word/WPS 可打开；
    只支持标题（#/##/###）与段落两种语义，足够承载剧本结构。

    Args:
        markdown: :func:`_render_script_markdown` 的产物。
        title: 文档标题（写入 core properties）。

    Returns:
        DOCX 文件字节。
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    paragraphs: list[str] = []
    for raw in markdown.splitlines():
        text = raw.strip()
        if not text:
            paragraphs.append("<w:p/>")
            continue
        if text.startswith("### "):
            style = '<w:pPr><w:pStyle w:val="Heading3"/></w:pPr>'
            body = escape(text[4:])
        elif text.startswith("## "):
            style = '<w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
            body = escape(text[3:])
        elif text.startswith("# "):
            style = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            body = escape(text[2:])
        else:
            style = ""
            body = escape(text)
        paragraphs.append(f'<w:p>{style}<w:r><w:t xml:space="preserve">{body}</w:t></w:r></w:p>')

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
    logger.info("剧本 DOCX 已生成 title=%s bytes=%s", title, buffer.tell())
    return buffer.getvalue()


# =====================================================================
# V4.0 模板包套用（US-F2-09 / AC-F2-09-1~3）
# =====================================================================


async def apply_script_template(
    session: AsyncSession,
    *,
    project: Any,
    template_id: str,
    user_id: int,
    deviated_fields: list[str] | None = None,
) -> dict[str, Any]:
    """把模板包参数回填到项目并记录引用快照（AC-F2-09-1~3）。

    回填字段：``genre / style_preset_key / target_episode_count``（写项目
    主表）+ ``word_target / pacing / ending``（写 ``projects.config``）。
    参数快照落 ``script_template_refs``，模板后续下线不影响本项目
    （读取走快照，AC-F2-09-3）。

    Args:
        session: 请求级数据库会话。
        project: 项目 ORM 实例（ACL 已校验）。
        template_id: 模板包 ID。
        user_id: 套用用户 ID。
        deviated_fields: 用户手改过的字段（写入偏离记录）。

    Returns:
        ``{template, project_params, deviated_fields, ref_id}``。

    Raises:
        BizError: 10002 模板包不存在或已下线。
    """
    pack = get_script_pack(template_id)
    if pack is None or not pack.get("is_active", True):
        raise BizError(
            ErrorCode.NOT_FOUND, "模板包不存在或已下线", error_detail="template_unavailable"
        )

    applied = {
        "genre": pack["genre"],
        "art_style": pack["art_style"],
        "word_target": pack["word_target"],
        "episode_target": pack["episode_target"],
        "pacing": pack["pacing"],
        "ending": pack["ending"],
    }

    project.genre = str(pack["genre"])[:32]
    project.style_preset_key = str(pack["art_style"])
    project.target_episode_count = int(pack["episode_target"])
    config = dict(project.config or {})
    config["word_target"] = pack["word_target"]
    config["pacing"] = pack["pacing"]
    config["ending"] = pack["ending"]
    config["template_id"] = template_id
    project.config = config
    project.lock_version += 1

    current = {
        "genre": project.genre,
        "art_style": project.style_preset_key,
        "word_target": config.get("word_target"),
        "episode_target": int(project.target_episode_count),
        "pacing": config.get("pacing"),
        "ending": config.get("ending"),
    }
    deviated = sorted(set(deviated_fields or []) | set(compute_deviated_fields(applied, current)))

    ref = (
        await session.execute(
            select(ScriptTemplateRef).where(
                ScriptTemplateRef.project_id == int(project.id),
                ScriptTemplateRef.template_id == template_id,
            )
        )
    ).scalar_one_or_none()
    if ref is None:
        ref = ScriptTemplateRef(
            project_id=int(project.id),
            template_id=template_id,
            template_name=pack.get("name"),
            applied_params=applied,
            deviated_fields=deviated,
            applied_by=user_id,
        )
        session.add(ref)
    else:
        ref.template_name = pack.get("name")
        ref.applied_params = applied
        ref.deviated_fields = deviated
        ref.applied_by = user_id
    await session.flush()
    await session.commit()
    return {
        "template": pack,
        "project_params": current,
        "deviated_fields": deviated,
        "ref_id": int(ref.id),
    }


# =====================================================================
# V4.0 续写与扩写（US-F2-08 / AC-F2-08-1~3）
# =====================================================================


def _build_continuation_prompts(
    script: Script,
    structure: dict[str, Any],
    *,
    count: int,
    outline_text: str,
    start_no: int,
) -> tuple[str, str]:
    """构造续写系统/用户提示词（只基于既有大纲与最后一集展开）。

    Args:
        script: 当前剧本版本。
        structure: 当前结构化剧本树。
        count: 续写集数。
        outline_text: 用户续写要点。
        start_no: 续写起始集号（当前集数 + 1）。

    Returns:
        ``(system, user)`` 提示词文本。
    """
    system = (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的编剧 Agent。用户已有确认剧本，需要你**续写**新集，"
        "不得改动任何既有集。\n"
        "成功标准：① 只输出新增集的 JSON；② 人物名与前情严格一致；"
        "③ 每集自带结尾钩子，适配竖屏短剧节奏。\n\n"
        "【第二段 · 项目上下文】\n"
        f"- 剧名：{script.title or '未命名'}\n"
        f"- 梗概：{script.logline or '（无）'}\n"
        f"- 既有集数：{int(script.total_episodes)}\n"
        f"- 续写起始集号：第 {start_no} 集\n\n"
        "【第三段 · 工作区约定】\n"
        f"- 需要输出 {count} 集，集号从 {start_no} 连续递增；\n"
        "- 每集 1~6 场，每场 4~12 行；单行 content ≤500 字；\n"
        "- 只输出新增集，禁止复述或改写既有集。\n\n"
        "【第四段 · 预算与边界】\n"
        "- 禁止：涉政、色情、暴力血腥、涉未成年人恋爱或性暗示内容；\n"
        "- 禁止：影射真实公众人物、抄袭已公映作品台词。\n\n"
        "【第五段 · 输出契约】\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释文字）：\n"
        "{\n"
        '  "episodes": [\n'
        "    {\n"
        '      "episode_no": <int>,\n'
        '      "title": "<集标题>",\n'
        '      "summary": "<本集概要>",\n'
        '      "hook": "<结尾钩子>",\n'
        '      "scenes": [\n'
        "        {\n"
        '          "scene_no": <int>,\n'
        '          "location": "<地点>",\n'
        '          "time_of_day": "day|night|dawn|dusk|null",\n'
        '          "int_ext": "int|ext|null",\n'
        '          "summary": "<场概要>",\n'
        '          "lines": [\n'
        '            {"line_type": "dialogue|narration|action|stage_direction", '
        '"speaker": "<说话人或 null>", "content": "<行内容>", "emotion": null}\n'
        "          ]\n"
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}"
    )
    last_episode = (structure.get("episodes") or [{}])[-1]
    characters = structure.get("characters") or []
    user = (
        f"既有最后一集：第 {last_episode.get('episode_no')} 集 "
        f"{last_episode.get('title') or ''}\n"
        f"概要：{last_episode.get('summary') or '（无）'}\n"
        f"结尾钩子：{last_episode.get('hook') or '（无）'}\n"
        f"人物表：{json.dumps(characters, ensure_ascii=False)[:2000]}\n\n"
        f"续写要点：{outline_text}\n"
        f"请续写 {count} 集（第 {start_no} 集起）。"
    )
    return system, user


async def continue_script(
    session: AsyncSession,
    *,
    project_id: int,
    script_id: int,
    count: int,
    outline_text: str,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """续写 N 集（AC-F2-08-1~3）：原集零改动，仅追加新集。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        script_id: 基准剧本版本 ID。
        count: 续写集数。
        outline_text: 续写要点。
        user_id: 操作人用户 ID。
        request_id: 请求 ID（留痕）。

    Returns:
        ``{script, added_episodes: [集号...], new_version}``。

    Raises:
        BizError: 10002 剧本不存在；10001 集数超上限（>30）；
            40001 剧本状态不允许续写；80001 模型不可用。
    """
    repo = ScriptRepository(session)
    script = await repo.get_script(script_id)
    if script is None or int(script.project_id) != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "剧本不存在")
    if script.status not in ("confirmed", "locked", "pending_confirm", "draft"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"剧本状态 {script.status} 不允许续写",
            data={"status": script.status},
        )
    # 集数上限校验以「元数据 + 实际工作副本」的较大值为准：
    # 只读 script.total_episodes 会在导入/续写后元数据滞后时放行超限请求，
    # 也会因集号不连续导致新集号与既有集冲突。
    db_episodes = await repo.episodes_of(int(script.id))
    structure_episodes = (script.structure or {}).get("episodes") or []
    current_episodes = max(
        int(script.total_episodes), len(db_episodes), len(structure_episodes)
    )
    if current_episodes + count > MAX_EPISODES:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"续写后总集数将超过上限 {MAX_EPISODES}（当前 {current_episodes} 集）",
            error_detail="episode_limit_exceeded",
            data={"current": current_episodes, "requested": count, "limit": MAX_EPISODES},
        )

    structure = copy.deepcopy(script.structure or {})
    start_no = current_episodes + 1
    system, user = _build_continuation_prompts(
        script, structure, count=count, outline_text=outline_text, start_no=start_no
    )
    payload = await _call_llm_json(
        system=system, user=user, project_id=project_id,
        temperature=LLM_REWRITE_TEMPERATURE, purpose="script_continue",
    )
    episodes = payload.get("episodes") if isinstance(payload, dict) else None
    if not isinstance(episodes, list) or not episodes:
        raise BizError(ErrorCode.MODEL_UNAVAILABLE, "续写结果为空，请稍后重试")

    added: list[int] = []
    normalized_episodes = structure.get("episodes") or []
    for offset, raw in enumerate(episodes[:count]):
        ep = _normalize_episode(raw)
        if ep is None:
            continue
        ep_no = start_no + offset
        ep["episode_no"] = ep_no
        normalized_episodes.append(ep)
        added.append(ep_no)
    if not added:
        raise BizError(ErrorCode.MODEL_UNAVAILABLE, "续写结果结构不合规，请稍后重试")
    structure["episodes"] = normalized_episodes

    new_script = await _create_new_version(
        session, script, new_structure=structure,
        status="draft" if script.status == "locked" else script.status,
    )
    new_script.version_label = f"续写 {len(added)} 集"[:50]
    await _rebuild_working_copy(session, new_script, structure)
    await repo.add_operation_log(
        actor_user_id=user_id,
        project_id=project_id,
        entity_type="script_version",
        entity_id=int(new_script.id),
        diff={
            "summary": f"续写 {len(added)} 集（第 {added[0]}~{added[-1]} 集）",
            "added_episodes": added,
            "version_from": int(script.version_no),
            "version_to": int(new_script.version_no),
        },
        request_id=request_id,
    )
    await _enforce_version_limit(session, project_id)
    await session.commit()
    return {
        "script": script_out(new_script),
        "added_episodes": added,
        "new_version": int(new_script.version_no),
    }


#: 后台续写任务强引用集（防 GC）
_CONTINUE_TASKS: set[asyncio.Task[None]] = set()


def spawn_continue_script(
    *,
    project_id: int,
    script_id: int,
    count: int,
    outline_text: str,
    user_id: int,
    request_id: str,
) -> asyncio.Task[None]:
    """以独立后台任务执行续写（D31 修复：202 受理 + 事件通知）。

    同步 LLM 续写实测可达 80 秒以上，在请求线程内执行会导致前端连接
    超时中断且无进度反馈。本包装改用独立短会话在后台执行，完成后发布
    ``script.continued`` 事件（失败发布 ``script.continue_failed``），
    前端经 SSE 或轮询剧本版本列表感知结果。

    Args:
        project_id: 项目 ID。
        script_id: 基准剧本版本 ID。
        count: 续写集数。
        outline_text: 续写要点。
        user_id: 操作人用户 ID。
        request_id: 请求 ID（留痕）。

    Returns:
        已调度的后台任务（调用方无需 await）。
    """

    async def _run() -> None:
        """后台执行体：独立会话调用 continue_script 并发布结果事件。"""
        try:
            async with async_session_factory() as session:
                data = await continue_script(
                    session,
                    project_id=project_id,
                    script_id=script_id,
                    count=count,
                    outline_text=outline_text,
                    user_id=user_id,
                    request_id=request_id,
                )
            await event_bus.publish(
                project_id,
                "script.continued",
                {
                    "new_version": data["new_version"],
                    "added_episodes": data["added_episodes"],
                    "title": (data.get("script") or {}).get("title"),
                },
            )
        except Exception as exc:  # noqa: BLE001 后台任务兜底：结果经事件告知前端
            message = exc.message if isinstance(exc, BizError) else str(exc)[:200]
            logger.warning("后台续写失败 project_id=%s: %s", project_id, message)
            await event_bus.publish(
                project_id,
                "script.continue_failed",
                {"message": message},
            )

    task = asyncio.create_task(_run())
    _CONTINUE_TASKS.add(task)
    task.add_done_callback(_CONTINUE_TASKS.discard)
    return task


def _build_expand_prompts(
    script: Script,
    episode: dict[str, Any],
    scene: dict[str, Any],
    instruction: str,
) -> tuple[str, str]:
    """构造单场扩写提示词（只改该场，行数增加但保持人物与前情）。

    Args:
        script: 剧本版本。
        episode: 所属集 dict。
        scene: 目标场 dict。
        instruction: 用户扩写指令（≤200 字）。

    Returns:
        ``(system, user)`` 提示词文本。
    """
    system = (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的编剧 Agent。请**扩写**指定场次，其余场次不得改动。\n"
        "成功标准：① 只输出该场的完整行列表；② 画面与动作更具体、情绪递进更细；"
        "③ 人物与前情一致。\n\n"
        "【第二段 · 项目上下文】\n"
        f"- 剧名：{script.title or '未命名'}\n"
        f"- 所在集：第 {episode.get('episode_no')} 集 {episode.get('title') or ''}\n"
        f"- 本场地点：{scene.get('location') or '未指定'}\n\n"
        "【第三段 · 工作区约定】\n"
        "- 扩写后行数 6~16 行（原行数基础上合理增加）；\n"
        "- 单行 content ≤500 字；不得新增场次。\n\n"
        "【第四段 · 预算与边界】\n"
        "- 禁止：涉政、色情、暴力血腥、涉未成年人恋爱或性暗示内容。\n\n"
        "【第五段 · 输出契约】\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释文字）：\n"
        f"{_REWRITE_CONTRACT}"
    )
    user = (
        f"本场现有内容：\n{json.dumps(scene.get('lines') or [], ensure_ascii=False)[:4000]}\n\n"
        f"扩写指令：{instruction}"
    )
    return system, user


async def expand_scene(
    session: AsyncSession,
    *,
    project_id: int,
    script_id: int,
    episode_no: int,
    scene_no: int,
    instruction: str,
    user_id: int,
    request_id: str,
) -> dict[str, Any]:
    """单场扩写（AC-F2-08-2）：仅该场产生新版本，并给出分集时长重算提示。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        script_id: 基准剧本版本 ID。
        episode_no: 目标集号。
        scene_no: 目标场号。
        instruction: 扩写指令（≤200 字）。
        user_id: 操作人用户 ID。
        request_id: 请求 ID（留痕）。

    Returns:
        ``{script, changed_lines, duration_change_seconds, duration_hint}``。

    Raises:
        BizError: 10002 剧本不存在；40002 集/场不存在；80001 模型不可用。
    """
    repo = ScriptRepository(session)
    script = await repo.get_script(script_id)
    if script is None or int(script.project_id) != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "剧本不存在")

    structure = copy.deepcopy(script.structure or {})
    episode, scene = _render_scene_context(
        structure, episode_no=episode_no, scene_no=scene_no
    )
    system, user = _build_expand_prompts(script, episode, scene, instruction)
    payload = await _call_llm_json(
        system=system, user=user, project_id=project_id,
        temperature=LLM_REWRITE_TEMPERATURE, purpose="script_expand",
    )
    new_lines = _normalize_lines_contract(payload if isinstance(payload, dict) else {})
    old_chars = sum(len(str(line.get("content") or "")) for line in scene.get("lines") or [])
    new_chars = sum(len(str(line.get("content") or "")) for line in new_lines)
    scene["lines"] = new_lines

    new_script = await _create_new_version(
        session, script, new_structure=structure, status=script.status
    )
    new_script.version_label = f"扩写 第{episode_no}集第{scene_no}场"[:50]
    await _rebuild_working_copy(session, new_script, structure)
    await repo.add_operation_log(
        actor_user_id=user_id,
        project_id=project_id,
        entity_type="script_version",
        entity_id=int(new_script.id),
        diff={
            "summary": f"扩写第 {episode_no} 集第 {scene_no} 场",
            "changed_lines": len(new_lines),
            "version_from": int(script.version_no),
            "version_to": int(new_script.version_no),
        },
        request_id=request_id,
    )
    await _enforce_version_limit(session, project_id)
    await session.commit()

    delta_seconds = int(round((new_chars - old_chars) / _CHARS_PER_SECOND))
    hint = None
    if delta_seconds > 0:
        hint = (
            f"本集预估时长变化约 +{delta_seconds}s，已触发分集时长重算提示"
            "（不自动调整分集边界）"
        )
    return {
        "script": script_out(new_script),
        "changed_lines": len(new_lines),
        "duration_change_seconds": delta_seconds,
        "duration_hint": hint,
    }
