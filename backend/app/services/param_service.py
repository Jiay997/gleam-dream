"""F11 参数中心服务：40 项参数定义、项目覆盖、版本快照、审计与重评估。

设计要点（PRD V6.4 F11）：
- **定义与值分离**：``param_defs`` 存元数据与硬边界，``param_configs`` 存项目覆盖；
- **硬边界服务端强校验**：前端滑杆约束不可信，越界即 10001；
- **危险参数留痕**：``is_dangerous=True`` 的变更必须填写 ≥10 字理由；
- **合规参数锁定**：``is_locked=True`` 的键禁止修改，返回 20003；
- **版本不可变**：每次有效保存生成 ``config_versions`` 快照；值未变化不产生新版本；
- **即时生效**：保存后项目 ``config_version`` +1，后续质检/连续性校验读取新值；
- **重评估**：按新阈值对历史校验结果给出“旧裁决→新裁决”对照，需显式采纳才写回。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models.project import Project
from app.models.script import Episode
from app.models.storyboard import Shot
from app.models.v6 import ConfigAudit, ConfigVersion, ContinuityCheck, ParamConfig, ParamDef

#: 40 项参数定义（key, group, label, type, default, min, max, scope, flags, 文案）
#: flags 位：D=危险、L=合规锁定、A=进阶、C=首屏常用
_DEFS: list[dict[str, Any]] = [
    # A 剧本与解析
    {"key": "script.scene_merge_threshold", "group": "A", "label": "场景合并阈值", "type": "float", "default": 0.6, "min": 0.3, "max": 0.95, "scope": "next_generate", "unit": "", "desc": "相邻场景相似度高于该值时合并", "high": "合并更多，场景数减少", "low": "场景切分更细", "flags": "A"},
    {"key": "script.confidence_threshold", "group": "A", "label": "解析置信度阈值", "type": "float", "default": 0.85, "min": 0.5, "max": 0.99, "scope": "next_generate", "unit": "", "desc": "低于该值的解析结果需人工确认", "high": "更多人工确认", "low": "更少拦截", "flags": "C"},
    {"key": "script.max_episodes", "group": "A", "label": "最大集数", "type": "int", "default": 100, "min": 1, "max": 100, "scope": "next_generate", "unit": "集", "desc": "单项目允许的最大集数", "high": "支持更长剧集", "low": "限制项目规模", "flags": "D"},
    {"key": "script.dialogue_retention", "group": "A", "label": "对白保留率", "type": "float", "default": 1.0, "min": 0.5, "max": 1.0, "scope": "next_generate", "unit": "", "desc": "改编时保留原对白的比例", "high": "更贴近原文", "low": "更多改写", "flags": "A"},
    # B 分镜与时长
    {"key": "storyboard.max_shot_ms", "group": "B", "label": "单镜头上限", "type": "int", "default": 15000, "min": 3000, "max": 15000, "scope": "next_storyboard", "unit": "ms", "desc": "单个镜头允许的最长时长", "high": "长镜头更多", "low": "镜头更碎", "flags": "C"},
    {"key": "storyboard.min_shot_ms", "group": "B", "label": "单镜头下限", "type": "int", "default": 1000, "min": 500, "max": 5000, "scope": "next_storyboard", "unit": "ms", "desc": "单个镜头允许的最短时长", "high": "减少碎镜", "low": "允许更短镜头", "flags": "A"},
    {"key": "storyboard.target_shot_count", "group": "B", "label": "目标镜头数", "type": "int", "default": 120, "min": 30, "max": 300, "scope": "next_storyboard", "unit": "个", "desc": "整集期望的镜头数量", "high": "节奏更慢", "low": "节奏更快", "flags": "C"},
    {"key": "storyboard.duration_tolerance_pct", "group": "B", "label": "时长容差", "type": "float", "default": 10.0, "min": 2.0, "max": 30.0, "scope": "next_check", "unit": "%", "desc": "成片时长与目标的允许偏差", "high": "更宽松", "low": "更严格", "flags": "A"},
    # C 资产一致性
    {"key": "asset.face_similarity_threshold", "group": "C", "label": "人脸相似度阈值", "type": "float", "default": 0.72, "min": 0.5, "max": 0.98, "scope": "immediate", "unit": "", "desc": "L2 人脸一致性的通过线", "high": "更严格", "low": "更宽松", "flags": "C"},
    {"key": "asset.outfit_similarity_threshold", "group": "C", "label": "服装相似度阈值", "type": "float", "default": 0.75, "min": 0.5, "max": 0.98, "scope": "immediate", "unit": "", "desc": "服装连续性通过线", "high": "更严格", "low": "更宽松", "flags": "A"},
    {"key": "asset.scene_similarity_threshold", "group": "C", "label": "场景相似度阈值", "type": "float", "default": 0.68, "min": 0.5, "max": 0.98, "scope": "immediate", "unit": "", "desc": "场景空间一致性通过线", "high": "更严格", "low": "更宽松", "flags": "A"},
    {"key": "asset.prop_similarity_threshold", "group": "C", "label": "道具相似度阈值", "type": "float", "default": 0.70, "min": 0.5, "max": 0.98, "scope": "immediate", "unit": "", "desc": "道具状态一致性通过线", "high": "更严格", "low": "更宽松", "flags": "A"},
    {"key": "asset.max_redraw_times", "group": "C", "label": "最大重抽次数", "type": "int", "default": 3, "min": 1, "max": 3, "scope": "next_generate", "unit": "次", "desc": "单个资产允许的重抽次数", "high": "更多尝试", "low": "更快收敛", "flags": "D"},
    # D 生成与质检
    {"key": "generation.qc_pass_score", "group": "D", "label": "质检通过分", "type": "float", "default": 0.80, "min": 0.5, "max": 0.99, "scope": "next_check", "unit": "", "desc": "自动质检的通过线", "high": "更严格", "low": "更宽松", "flags": "C"},
    {"key": "generation.max_retry", "group": "D", "label": "最大重试次数", "type": "int", "default": 3, "min": 1, "max": 5, "scope": "next_generate", "unit": "次", "desc": "单任务失败后的重试上限", "high": "更抗抖动", "low": "更快失败", "flags": "A"},
    {"key": "generation.concurrent_tasks", "group": "D", "label": "并发生成数", "type": "int", "default": 4, "min": 1, "max": 8, "scope": "next_generate", "unit": "个", "desc": "同时执行的生成任务数", "high": "更快但更耗资源", "low": "更稳但更慢", "flags": "D"},
    {"key": "generation.human_review_ratio", "group": "D", "label": "人工复核比例", "type": "float", "default": 0.20, "min": 0.0, "max": 1.0, "scope": "next_check", "unit": "", "desc": "抽查人工复核的镜头比例", "high": "更多人工复核", "low": "更少人工复核", "flags": "A"},
    {"key": "generation.degraded_threshold", "group": "D", "label": "降级判定分", "type": "float", "default": 0.60, "min": 0.3, "max": 0.9, "scope": "next_check", "unit": "", "desc": "低于该分标记为降级产出", "high": "更多降级标记", "low": "更少降级标记", "flags": "A"},
    # E 配音
    {"key": "voice.rate", "group": "E", "label": "默认语速", "type": "float", "default": 1.0, "min": 0.5, "max": 2.0, "scope": "next_voice", "unit": "x", "desc": "角色未单独设置时的默认语速", "high": "语速更快", "low": "语速更慢", "flags": "C"},
    {"key": "voice.pitch", "group": "E", "label": "默认音调", "type": "int", "default": 0, "min": -6, "max": 6, "scope": "next_voice", "unit": "半音", "desc": "角色未单独设置时的默认音调", "high": "音调更高", "low": "音调更低", "flags": "A"},
    {"key": "voice.gap_ms", "group": "E", "label": "句间气口", "type": "int", "default": 120, "min": 0, "max": 1000, "scope": "next_voice", "unit": "ms", "desc": "相邻台词之间的默认停顿", "high": "停顿更长", "low": "衔接更紧", "flags": "C"},
    {"key": "voice.drift_absorb_ms", "group": "E", "label": "漂移吸收阈值", "type": "int", "default": 120, "min": 0, "max": 500, "scope": "immediate", "unit": "ms", "desc": "小于该时长的重配差异自动吸收", "high": "更多差异被吸收", "low": "更早触发重算", "flags": "A"},
    {"key": "voice.drift_recalc_pct", "group": "E", "label": "漂移重算比例", "type": "float", "default": 10.0, "min": 1.0, "max": 50.0, "scope": "immediate", "unit": "%", "desc": "超过该比例需重算镜头时长", "high": "更宽松", "low": "更严格", "flags": "A"},
    # F 后期与导出
    {"key": "compose.crossfade_ms", "group": "F", "label": "默认转场时长", "type": "int", "default": 500, "min": 0, "max": 3000, "scope": "next_compose", "unit": "ms", "desc": "新建转场的默认时长", "high": "转场更柔", "low": "转场更利落", "flags": "A"},
    {"key": "compose.bgm_gain_db", "group": "F", "label": "BGM 增益", "type": "int", "default": -12, "min": -30, "max": 0, "scope": "next_compose", "unit": "dB", "desc": "背景音乐相对台词的电平", "high": "BGM 更响", "low": "BGM 更轻", "flags": "C"},
    {"key": "compose.loudness_lufs", "group": "F", "label": "响度标准", "type": "int", "default": -14, "min": -24, "max": -9, "scope": "next_compose", "unit": "LUFS", "desc": "成片整体响度目标", "high": "更响", "low": "更轻", "flags": "A"},
    {"key": "compose.subtitle_size", "group": "F", "label": "字幕字号", "type": "int", "default": 42, "min": 24, "max": 72, "scope": "next_compose", "unit": "px", "desc": "竖屏成片字幕字号", "high": "字幕更大", "low": "字幕更小", "flags": "C"},
    {"key": "export.bitrate_kbps", "group": "F", "label": "导出码率", "type": "int", "default": 8000, "min": 2000, "max": 20000, "scope": "next_compose", "unit": "kbps", "desc": "1080P 成片导出码率", "high": "画质更好体积更大", "low": "体积更小", "flags": "A"},
    {"key": "compose.av_sync_tolerance_ms", "group": "F", "label": "音画同步容差", "type": "int", "default": 80, "min": 40, "max": 200, "scope": "immediate", "unit": "ms", "desc": "音画偏差允许的最大值", "high": "更宽松", "low": "更严格", "flags": "A"},
    {"key": "compose.preview_enabled", "group": "F", "label": "启用快速预览", "type": "bool", "default": True, "min": None, "max": None, "scope": "immediate", "unit": "", "desc": "导出前是否生成低清预览", "high": "开启预览", "low": "关闭预览", "flags": "C"},
    # G 合规与安全
    {"key": "compliance.content_safety_threshold", "group": "G", "label": "内容安全阈值", "type": "float", "default": 0.85, "min": 0.6, "max": 0.99, "scope": "immediate", "unit": "", "desc": "低于该分的内容直接拦截", "high": "更严格", "low": "更宽松", "flags": "L"},
    {"key": "compliance.minor_confidence", "group": "G", "label": "未成年判定线", "type": "float", "default": 0.30, "min": 0.1, "max": 0.6, "scope": "immediate", "unit": "", "desc": "未成年置信度超过该值拒绝合成", "high": "更宽松", "low": "更严格", "flags": "L"},
    {"key": "compliance.watermark_required", "group": "G", "label": "强制水印", "type": "bool", "default": True, "min": None, "max": None, "scope": "immediate", "unit": "", "desc": "AI 生成内容是否强制加水印", "high": "开启水印", "low": "关闭水印", "flags": "L"},
    {"key": "compliance.photo_max_mb", "group": "G", "label": "照片大小上限", "type": "int", "default": 20, "min": 1, "max": 20, "scope": "immediate", "unit": "MB", "desc": "单张照片允许的最大体积", "high": "允许更大照片", "low": "更严格", "flags": "L"},
    # H 连续性与反馈
    {"key": "continuity.l2_face_threshold", "group": "H", "label": "L2 人脸阈值", "type": "float", "default": 0.72, "min": 0.5, "max": 0.98, "scope": "immediate", "unit": "", "desc": "相邻镜头人脸一致性通过线", "high": "更严格", "low": "更宽松", "flags": "C"},
    {"key": "continuity.l2_outfit_threshold", "group": "H", "label": "L2 服装阈值", "type": "float", "default": 0.75, "min": 0.5, "max": 0.98, "scope": "immediate", "unit": "", "desc": "相邻镜头服装一致性通过线", "high": "更严格", "low": "更宽松", "flags": "A"},
    {"key": "continuity.l2_lighting_threshold", "group": "H", "label": "L2 光照阈值", "type": "float", "default": 0.65, "min": 0.4, "max": 0.98, "scope": "immediate", "unit": "", "desc": "相邻镜头光照连续通过线", "high": "更严格", "low": "更宽松", "flags": "A"},
    {"key": "continuity.lr_enabled", "group": "H", "label": "启用逻辑规则", "type": "bool", "default": True, "min": None, "max": None, "scope": "immediate", "unit": "", "desc": "是否执行 LR-1~LR-6 规则", "high": "开启", "low": "关闭", "flags": "C"},
    {"key": "continuity.bible_completeness_min", "group": "H", "label": "Bible 完整度线", "type": "float", "default": 0.80, "min": 0.5, "max": 1.0, "scope": "immediate", "unit": "", "desc": "低于该值提示先补全设定", "high": "更宽松", "low": "更严格", "flags": "A"},
    {"key": "feedback.keep_ratio", "group": "H", "label": "反馈保留比例", "type": "float", "default": 0.80, "min": 0.5, "max": 1.0, "scope": "immediate", "unit": "", "desc": "重生成时保留上一版内容的比例", "high": "更保守", "low": "更大胆", "flags": "A"},
]


def _flag(flags: str, letter: str) -> bool:
    """判断定义标记位是否包含某字母。

    Args:
        flags: 标记串（D/L/A/C）。
        letter: 目标字母。

    Returns:
        是否包含。
    """
    return letter in flags


async def ensure_seed(session: AsyncSession) -> int:
    """幂等写入 40 项参数定义。

    Args:
        session: 数据库会话。

    Returns:
        新增定义条数。
    """
    existing = set(
        (await session.execute(select(ParamDef.param_key))).scalars().all()
    )
    added = 0
    for order, item in enumerate(_DEFS):
        if item["key"] in existing:
            continue
        session.add(
            ParamDef(
                param_key=item["key"],
                param_group=item["group"],
                label=item["label"],
                value_type=item["type"],
                default_value={"value": item["default"]},
                hard_min={"value": item["min"]} if item["min"] is not None else None,
                hard_max={"value": item["max"]} if item["max"] is not None else None,
                suggested_min={"value": item["min"]} if item["min"] is not None else None,
                suggested_max={"value": item["max"]} if item["max"] is not None else None,
                enum_options=[],
                unit=item["unit"] or None,
                effect_scope=item["scope"],
                is_dangerous=_flag(item["flags"], "D"),
                is_locked=_flag(item["flags"], "L"),
                is_advanced=_flag(item["flags"], "A"),
                is_common=_flag(item["flags"], "C"),
                description=item["desc"],
                high_effect=item["high"],
                low_effect=item["low"],
                display_order=order,
            )
        )
        added += 1
    if added:
        await session.commit()
    return added


def _unbox(payload: Any) -> Any:
    """把 ``{"value": x}`` 结构还原为裸值。

    Args:
        payload: 可能是包装结构的 JSON 值。

    Returns:
        裸值。
    """
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


async def effective_config(
    session: AsyncSession,
    project_id: int,
    ep_no: int | None = None,
    include_defs: bool = False,
) -> dict[str, Any]:
    """计算项目（及可选集级）生效参数。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号（可选，覆盖项目级同名参数）。
        include_defs: 是否附带定义元数据。

    Returns:
        含 ``values`` / ``version`` / ``defs``（可选）的字典。
    """
    await ensure_seed(session)
    defs = list(
        (await session.execute(select(ParamDef).order_by(ParamDef.display_order))).scalars().all()
    )
    values: dict[str, Any] = {row.param_key: _unbox(row.default_value) for row in defs}

    rows = (
        await session.execute(
            select(ParamConfig).where(ParamConfig.project_id == project_id)
        )
    ).scalars().all()
    for row in rows:
        if row.scope == "project" or (ep_no is not None and row.scope == "episode" and row.scope_id == str(ep_no)):
            values[row.param_key] = _unbox(row.value)

    project = await session.get(Project, project_id)
    payload: dict[str, Any] = {
        "project_id": project_id,
        "ep_no": ep_no,
        "version": project.config_version if project else 0,
        "values": values,
    }
    if include_defs:
        payload["groups"] = _group_defs(defs)
    return payload


def _group_defs(defs: list[ParamDef]) -> list[dict[str, Any]]:
    """按组聚合参数定义（供参数中心渲染）。

    Args:
        defs: 参数定义列表。

    Returns:
        分组数组。
    """
    groups: dict[str, dict[str, Any]] = {}
    for row in defs:
        bucket = groups.setdefault(
            row.param_group,
            {"group": row.param_group, "items": []},
        )
        bucket["items"].append(
            {
                "param_key": row.param_key,
                "label": row.label,
                "value_type": row.value_type,
                "default_value": _unbox(row.default_value),
                "hard_min": _unbox(row.hard_min) if row.hard_min else None,
                "hard_max": _unbox(row.hard_max) if row.hard_max else None,
                "suggested_min": _unbox(row.suggested_min) if row.suggested_min else None,
                "suggested_max": _unbox(row.suggested_max) if row.suggested_max else None,
                "unit": row.unit,
                "effect_scope": row.effect_scope,
                "is_dangerous": row.is_dangerous,
                "is_locked": row.is_locked,
                "is_advanced": row.is_advanced,
                "is_common": row.is_common,
                "description": row.description,
                "high_effect": row.high_effect,
                "low_effect": row.low_effect,
            }
        )
    return [groups[key] for key in sorted(groups)]


def _validate_value(defn: ParamDef, value: Any) -> Any:
    """按定义校验并归一化参数值。

    Args:
        defn: 参数定义。
        value: 待写入值。

    Returns:
        归一化后的裸值。

    Raises:
        BizError: 10001 类型或边界不合法。
    """
    if defn.is_locked:
        raise BizError(ErrorCode.FORBIDDEN, f"参数「{defn.label}」为合规锁定项，不可修改")
    if defn.value_type == "bool":
        if not isinstance(value, bool):
            raise BizError(ErrorCode.PARAM_INVALID, f"参数「{defn.label}」必须为布尔值")
        return value
    if defn.value_type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise BizError(ErrorCode.PARAM_INVALID, f"参数「{defn.label}」必须为整数")
        candidate: float = float(value)
    elif defn.value_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BizError(ErrorCode.PARAM_INVALID, f"参数「{defn.label}」必须为数值")
        candidate = float(value)
    else:  # enum
        return value

    low = _unbox(defn.hard_min) if defn.hard_min else None
    high = _unbox(defn.hard_max) if defn.hard_max else None
    if low is not None and candidate < float(low):
        raise BizError(ErrorCode.PARAM_INVALID, f"参数「{defn.label}」不得小于 {low}")
    if high is not None and candidate > float(high):
        raise BizError(ErrorCode.PARAM_INVALID, f"参数「{defn.label}」不得大于 {high}")
    return int(candidate) if defn.value_type == "int" else round(candidate, 4)


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    """计算参数快照哈希。

    Args:
        snapshot: 参数键→值。

    Returns:
        64 位十六进制 SHA-256。
    """
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def save_params(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    changes: dict[str, Any],
    reason: str | None,
    scope: str,
    scope_id: str,
) -> dict[str, Any]:
    """保存参数变更并生成版本快照与审计记录。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        changes: 参数键→新值。
        reason: 变更理由。
        scope: 作用域。
        scope_id: 作用域标识。

    Returns:
        含 ``version`` / ``changed`` / ``ignored`` / ``warnings`` 的字典。

    Raises:
        BizError: 10002 项目不存在；10001 值非法/缺理由；20003 合规锁定。
    """
    await ensure_seed(session)
    project = await session.get(Project, project_id)
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

    defs = {
        row.param_key: row
        for row in (await session.execute(select(ParamDef))).scalars().all()
    }
    current = await effective_config(session, project_id)
    validated: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in changes.items():
        defn = defs.get(key)
        if defn is None:
            raise BizError(ErrorCode.PARAM_INVALID, f"未知参数: {key}")
        validated[key] = _validate_value(defn, value)
        suggested_low = _unbox(defn.suggested_min) if defn.suggested_min else None
        suggested_high = _unbox(defn.suggested_max) if defn.suggested_max else None
        numeric = validated[key]
        if isinstance(numeric, (int, float)) and not isinstance(numeric, bool):
            if suggested_low is not None and numeric < float(suggested_low):
                warnings.append(f"{defn.label} 低于建议范围")
            if suggested_high is not None and numeric > float(suggested_high):
                warnings.append(f"{defn.label} 高于建议范围")

    dangerous = [defs[key] for key in validated if defs[key].is_dangerous]
    if dangerous and (not reason or len(reason.strip()) < 10):
        raise BizError(ErrorCode.PARAM_INVALID, "危险参数变更需填写至少 10 字理由")

    ignored: list[str] = []
    applied: dict[str, Any] = {}
    for key, value in validated.items():
        if current["values"].get(key) == value:
            ignored.append(key)
            continue
        applied[key] = value

    if not applied:
        return {
            "version": project.config_version,
            "changed": {},
            "ignored": ignored,
            "warnings": warnings,
            "unchanged": True,
        }

    # 删除同作用域旧覆盖后写入（避免 scope_id 变化带来的残留）
    for key, value in applied.items():
        existing = (
            await session.execute(
                select(ParamConfig).where(
                    ParamConfig.project_id == project_id,
                    ParamConfig.scope == scope,
                    ParamConfig.scope_id == scope_id,
                    ParamConfig.param_key == key,
                )
            )
        ).scalars().first()
        old_value = _unbox(existing.value) if existing else current["values"].get(key)
        if existing is None:
            session.add(
                ParamConfig(
                    project_id=project_id,
                    scope=scope,
                    scope_id=scope_id,
                    param_key=key,
                    value={"value": value},
                    updated_by=int(user_id) if str(user_id).isdigit() else None,
                    config_version=project.config_version + 1,
                )
            )
        else:
            existing.value = {"value": value}
            existing.config_version = project.config_version + 1
        session.add(
            ConfigAudit(
                project_id=project_id,
                config_version=project.config_version + 1,
                param_key=key,
                old_value={"value": old_value} if old_value is not None else None,
                new_value={"value": value},
                is_dangerous=defs[key].is_dangerous,
                reason=reason,
                actor_user_id=int(user_id) if str(user_id).isdigit() else None,
                action="update",
            )
        )

    project.config_version += 1
    await session.flush()
    snapshot = (await effective_config(session, project_id))["values"]
    version = ConfigVersion(
        project_id=project_id,
        version_no=project.config_version,
        snapshot=snapshot,
        changes=[{"param_key": key, "new_value": value} for key, value in applied.items()],
        source="save",
        actor_user_id=int(user_id) if str(user_id).isdigit() else None,
        config_hash=_snapshot_hash(snapshot),
    )
    session.add(version)
    await session.commit()
    return {
        "version": project.config_version,
        "changed": applied,
        "ignored": ignored,
        "warnings": warnings,
        "unchanged": False,
    }


async def list_versions(
    session: AsyncSession, project_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    """列出配置版本（倒序）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        limit: 返回条数。

    Returns:
        版本摘要列表。
    """
    rows = (
        await session.execute(
            select(ConfigVersion)
            .where(ConfigVersion.project_id == project_id)
            .order_by(ConfigVersion.version_no.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "version_no": row.version_no,
            "source": row.source,
            "changes": row.changes,
            "rollback_from": row.rollback_from,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def rollback(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    version_no: int,
    reason: str | None,
) -> dict[str, Any]:
    """回滚到历史版本（以新版本形式落库，历史不可变）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        version_no: 目标版本号。
        reason: 回滚理由。

    Returns:
        新版本信息。

    Raises:
        BizError: 10002 项目/版本不存在；10001 缺理由。
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")
    target = (
        await session.execute(
            select(ConfigVersion).where(
                ConfigVersion.project_id == project_id,
                ConfigVersion.version_no == version_no,
            )
        )
    ).scalars().first()
    if target is None:
        raise BizError(ErrorCode.NOT_FOUND, "目标版本不存在")
    if not reason or len(reason.strip()) < 10:
        raise BizError(ErrorCode.PARAM_INVALID, "回滚需填写至少 10 字理由")

    defs = {
        row.param_key: row
        for row in (await session.execute(select(ParamDef))).scalars().all()
    }
    snapshot = target.snapshot or {}
    await session.execute(
        delete(ParamConfig).where(
            ParamConfig.project_id == project_id, ParamConfig.scope == "project"
        )
    )
    for key, value in snapshot.items():
        defn = defs.get(key)
        if defn is not None and defn.is_locked:
            continue
        session.add(
            ParamConfig(
                project_id=project_id,
                scope="project",
                scope_id="0",
                param_key=key,
                value={"value": value},
                updated_by=int(user_id) if str(user_id).isdigit() else None,
                config_version=project.config_version + 1,
            )
        )
    project.config_version += 1
    await session.flush()
    new_snapshot = (await effective_config(session, project_id))["values"]
    session.add(
        ConfigVersion(
            project_id=project_id,
            version_no=project.config_version,
            snapshot=new_snapshot,
            changes=[{"param_key": key, "new_value": value} for key, value in snapshot.items()],
            source="rollback",
            rollback_from=version_no,
            actor_user_id=int(user_id) if str(user_id).isdigit() else None,
            config_hash=_snapshot_hash(new_snapshot),
        )
    )
    session.add(
        ConfigAudit(
            project_id=project_id,
            config_version=project.config_version,
            param_key="*",
            old_value=None,
            new_value={"rollback_from": version_no},
            is_dangerous=True,
            reason=reason,
            actor_user_id=int(user_id) if str(user_id).isdigit() else None,
            action="rollback",
        )
    )
    await session.commit()
    return {"version": project.config_version, "rollback_from": version_no}


async def restore_defaults(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    param_keys: list[str],
    reason: str | None,
) -> dict[str, Any]:
    """恢复默认值（删除项目覆盖），生成新版本与审计。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        param_keys: 指定参数（空表示全部）。
        reason: 操作理由。

    Returns:
        新版本与恢复的参数键。

    Raises:
        BizError: 10002 项目不存在。
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")
    stmt = delete(ParamConfig).where(
        ParamConfig.project_id == project_id, ParamConfig.scope == "project"
    )
    if param_keys:
        stmt = stmt.where(ParamConfig.param_key.in_(param_keys))
    await session.execute(stmt)
    project.config_version += 1
    await session.flush()
    snapshot = (await effective_config(session, project_id))["values"]
    session.add(
        ConfigVersion(
            project_id=project_id,
            version_no=project.config_version,
            snapshot=snapshot,
            changes=[{"param_key": key, "new_value": "default"} for key in param_keys],
            source="restore_default",
            actor_user_id=int(user_id) if str(user_id).isdigit() else None,
            config_hash=_snapshot_hash(snapshot),
        )
    )
    await session.commit()
    return {"version": project.config_version, "restored": param_keys or "all"}


async def list_audits(
    session: AsyncSession, project_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    """查询参数审计记录（倒序）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        limit: 返回条数。

    Returns:
        审计列表。
    """
    rows = (
        await session.execute(
            select(ConfigAudit)
            .where(ConfigAudit.project_id == project_id)
            .order_by(ConfigAudit.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": row.id,
            "config_version": row.config_version,
            "param_key": row.param_key,
            "old_value": row.old_value,
            "new_value": row.new_value,
            "is_dangerous": row.is_dangerous,
            "reason": row.reason,
            "action": row.action,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def reassess(
    session: AsyncSession, project_id: int, ep_no: int | None, target: str
) -> dict[str, Any]:
    """按当前参数重评估历史连续性/质检裁决（只读对照）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 限定集号。
        target: 对象类型（当前支持 shot）。

    Returns:
        含 ``items`` / ``summary`` / ``config_version`` 的字典。
    """
    config = await effective_config(session, project_id, ep_no)
    checks = (
        await session.execute(
            select(ContinuityCheck)
            .where(ContinuityCheck.project_id == project_id)
            .order_by(ContinuityCheck.id.desc())
            .limit(500)
        )
    ).scalars().all()
    if ep_no is not None:
        episode_id = (
            await session.execute(
                select(Episode.id).where(
                    Episode.project_id == project_id, Episode.episode_no == ep_no
                )
            )
        ).scalar_one_or_none()
        if episode_id is not None:
            shot_ids = set(
                (
                    await session.execute(
                        select(Shot.id).where(
                            Shot.project_id == project_id, Shot.episode_id == episode_id
                        )
                    )
                ).scalars().all()
            )
            checks = [c for c in checks if c.shot_id in shot_ids]

    threshold_map = {
        "face": config["values"].get("continuity.l2_face_threshold", 0.72),
        "outfit": config["values"].get("continuity.l2_outfit_threshold", 0.75),
        "timing_light": config["values"].get("continuity.l2_lighting_threshold", 0.65),
        "scene": config["values"].get("asset.scene_similarity_threshold", 0.68),
        "prop": config["values"].get("asset.prop_similarity_threshold", 0.70),
    }
    items: list[dict[str, Any]] = []
    changed = 0
    for check in checks:
        threshold = float(threshold_map.get(check.dimension, 0.7))
        score = float(check.score) if check.score is not None else None
        if score is None:
            new_verdict = check.verdict
        else:
            new_verdict = "pass" if score >= threshold else ("fail" if score < threshold - 0.05 else "warn")
        if new_verdict != check.verdict:
            changed += 1
        items.append(
            {
                "check_id": check.id,
                "shot_id": check.shot_id,
                "dimension": check.dimension,
                "score": score,
                "old_threshold": float(check.threshold) if check.threshold is not None else None,
                "new_threshold": threshold,
                "old_verdict": check.verdict,
                "new_verdict": new_verdict,
            }
        )
    return {
        "config_version": config["version"],
        "target": target,
        "summary": {"total": len(items), "changed": changed},
        "items": items,
    }


async def adopt_reassess(
    session: AsyncSession, project_id: int, check_ids: list[int]
) -> dict[str, Any]:
    """采纳重评估结论（写回阈值与裁决）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        check_ids: 校验结果 ID 列表。

    Returns:
        更新条数与最新配置版本。

    Raises:
        BizError: 10002 校验结果不存在。
    """
    config = await effective_config(session, project_id)
    threshold_map = {
        "face": config["values"].get("continuity.l2_face_threshold", 0.72),
        "outfit": config["values"].get("continuity.l2_outfit_threshold", 0.75),
        "timing_light": config["values"].get("continuity.l2_lighting_threshold", 0.65),
        "scene": config["values"].get("asset.scene_similarity_threshold", 0.68),
        "prop": config["values"].get("asset.prop_similarity_threshold", 0.70),
    }
    rows = (
        await session.execute(
            select(ContinuityCheck).where(
                ContinuityCheck.project_id == project_id,
                ContinuityCheck.id.in_(check_ids),
            )
        )
    ).scalars().all()
    if not rows:
        raise BizError(ErrorCode.NOT_FOUND, "校验结果不存在")
    for check in rows:
        threshold = float(threshold_map.get(check.dimension, 0.7))
        check.threshold = threshold
        if check.score is not None:
            score = float(check.score)
            check.verdict = "pass" if score >= threshold else ("fail" if score < threshold - 0.05 else "warn")
        check.config_version = config["version"]
        check.reviewer = "human_override"
    await session.commit()
    return {"updated": len(rows), "config_version": config["version"]}
