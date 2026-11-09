"""分镜域服务层模块（Task 7b）。

实现分镜全生命周期业务（202 异步任务模型 + 后台 asyncio task）：
- **分镜生成**：:func:`generate_storyboard` 发起后台管线——取该集剧本行 →
  LLM 导演（五段式提示词）→ 确定性组装 shots（duration_ms/image_prompt）→
  简单 QC（对白镜头时长截断 + 景别跳跃检测，warning 落 qc_reports）→
  status=pending_review → 发布 ``storyboard.generated``；同集重复调用幂等；
- **镜头编辑**：PATCH 局部更新、拖拽重排（order_index 重算 + 钩子位移提示）、
  锚点绑定（资产须 locked，否则 50001；绑定后组装 final_prompt 注入外观）；
- **分镜锁定与审批**：lock 校验全部镜头至少 1 个锚点（缺 → 60005 附缺锚点
  清单）→ 分镜与镜头全部锁定 → 创建 storyboard 审批单（+7 天过期）→
  发布 ``approval.created``；decide 端点实现审批决策——approve → 项目
  流转 rendering（分镜保持 locked）；reject（feedback ≥10 字）→ 解锁回
  pending_review，反馈留痕审批单；
- **预览与布局**：草稿预览（keyframe/subtitle/时长合计）、画布布局 upsert。

工程适配说明：``qc_reports.target_type`` / ``approval_requests.target_type``
的 DB CHECK 约束不含 ``storyboard``（仅 shot/asset/script/timeline/output
与 script/asset/shot/timeline），故镜头级 QC 报告按 ``target_type=shot``
落库、审批单 target_type 置 NULL（approval_type=storyboard + target_id
仍精确定位分镜板）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.infra.event_bus import event_bus
from app.models.collab import ApprovalRequest, StylePreset
from app.models.asset import Asset, AssetVersion
from app.models.project import Project
from app.models.quality import QcReport
from app.models.script import Episode, Scene, Script, ScriptLine
from app.models.storyboard import (
    DirectorNote,
    Shot,
    Storyboard,
    StoryboardLayout,
    StoryboardOp,
    StoryboardTemplate,
)
from app.repositories.asset_repo import AssetRepository
from app.repositories.storyboard_repo import StoryboardRepository
from app.schemas.storyboard import shot_out, storyboard_out
from app.services import motion_service
from app.services.analytics_service import GATE_STORYBOARD, track_gate
from app.services.asset_service import call_llm_contract
from app.services.shot_frame_spec import AssetRef, derive_composition_spec
from app.services.template_packs import MAX_OP_HISTORY

logger = logging.getLogger("gleamdream.services.storyboard")

# ---- 后台任务强引用集（防 GC 丢失：loop 仅弱引用运行中的 Task） ----
_BG_TASKS: set[asyncio.Task[None]] = set()


def _spawn_bg_task(coro: Any) -> asyncio.Task[None]:
    """创建后台任务并持有强引用（防 GC 丢失）。

    Args:
        coro: 后台协程。

    Returns:
        已调度的 asyncio.Task。
    """
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task

# ---- 枚举白名单（与 DB CHECK 约束一致） ----
_SHOT_SIZES = ("extreme_wide", "wide", "medium", "close_up", "extreme_close_up", "over_shoulder")
_CAMERA_MOVES = (
    "static", "pan", "tilt", "dolly_in", "dolly_out", "tracking", "orbit", "handheld", "crane",
)
_EMOTION_TAGS = ("calm", "joy", "anger", "sadness", "fear", "surprise", "tension", "romance")

# 发起分镜生成的禁止状态（D15 修复：受理前置状态校验，杜绝"受理成功但静默丢弃"）。
# archived 已由 ACL 归档只读守卫（deps.require_project_acl）拦截为 40004，此处为
# 服务层兜底；paused 下编排类动作一律 40001（与剧本生成守卫口径一致，GR-1）。
_STORYBOARD_FORBIDDEN_STATUSES = frozenset({"archived", "paused"})

# 景别跳跃检测的景别序（数值差 ≥2 视为跳跃）
_SHOT_SIZE_ORDER: dict[str, int] = {
    "extreme_wide": 0, "wide": 1, "medium": 2, "over_shoulder": 2,
    "close_up": 3, "extreme_close_up": 4,
}

# 对白镜头时长上限（毫秒，超过截断并记 warning）
_DIALOGUE_DURATION_MAX_MS: int = 6000

# 审批单过期天数
_APPROVAL_EXPIRE_DAYS: int = 7

# 分镜生成剧本行文本上限（字符）。
# 2026-11 起加入场次头（location/time/summary），上限由 6000 放宽到 8000，
# 避免尾部场次被截断导致导演看不到完整因果。
_DIRECTOR_TEXT_LIMIT: int = 8000


def _utcnow() -> datetime:
    """获取当前 UTC 时间（timezone-aware）。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


def _normalize_asset_name(value: str | None) -> str:
    """归一化角色/场景名称，便于做宽松匹配。

    Args:
        value: 原始名称（可能含空格、括号、标点）。

    Returns:
        仅保留文字与数字的归一化名称。
    """
    separators = set(" \t\r\n（）()【】[]·：:，,。.-_/")
    return "".join(ch for ch in str(value or "").strip().lower() if ch not in separators)


def _bigram_overlap(left: str, right: str) -> float:
    """计算两个字符串的二元组（2-gram）重合率。

    用于兜底匹配「措辞不同但同指一物」的名称，例如资产名
    「独立书店柜台」与剧本 location「独立书店临街柜台内，傍晚…」：
    双向包含匹配会因插入「临街」而完全失效，但二元组重合率仍很高。

    Args:
        left: 归一化字符串 A。
        right: 归一化字符串 B。

    Returns:
        重合率（0~1）：交集大小 / 较短串的二元组数量；短串不足 2 字返回 0。
    """
    if len(left) < 2 or len(right) < 2:
        return 0.0
    left_grams = {left[i:i + 2] for i in range(len(left) - 1)}
    right_grams = {right[i:i + 2] for i in range(len(right) - 1)}
    base = min(len(left_grams), len(right_grams))
    if not base:
        return 0.0
    return len(left_grams & right_grams) / float(base)


def _match_asset(assets: list[Asset], name: str | None, asset_type: str) -> Asset | None:
    """按名称在已锁定资产中匹配角色/场景。

    优先精确归一化匹配，其次双向包含匹配（兼容「林晚」「林晚（女主）」
    这类命名差异），最后用二元组重合率兜底（兼容「独立书店柜台」与
    「独立书店临街柜台内，傍晚…」这类措辞差异）。

    Args:
        assets: 项目已锁定资产列表。
        name: 剧本/导演给出的名称。
        asset_type: 目标资产类型（character/scene/prop）。

    Returns:
        命中的资产实例；未命中返回 None。
    """
    key = _normalize_asset_name(name)
    if not key:
        return None
    candidates = [a for a in assets if str(a.asset_type) == asset_type]
    for asset in candidates:
        if _normalize_asset_name(asset.name) == key:
            return asset
    for asset in candidates:
        normalized = _normalize_asset_name(asset.name)
        if normalized and (key in normalized or normalized in key):
            return asset
    # 二元组兜底（2026-11 QA 修复）：location 往往带修饰语（临街/的小/
    # 室内…），包含匹配失效后场景锚点为空 → 首帧没有场景基底、镜内场景
    # 突变。仅对 scene 启用（角色/道具名很短，二元组匹配容易误配），
    # 取重合率最高且达到阈值的候选，阈值 0.5 避免「咖啡馆」这类通用词
    # 把不同场景串在一起。
    if asset_type != "scene":
        return None
    best: Asset | None = None
    best_ratio = 0.0
    for asset in candidates:
        ratio = _bigram_overlap(key, _normalize_asset_name(asset.name))
        if ratio > best_ratio:
            best_ratio = ratio
            best = asset
    if best is not None and best_ratio >= 0.5:
        return best
    return None


def _match_prop_assets(
    assets: list[Asset],
    names: list[str] | None,
    text_blob: str,
) -> list[Asset]:
    """匹配镜头涉及的道具资产。

    匹配优先级：LLM 明确给出的道具名（精确/包含）→ 文本命中兜底。
    单字名称不参与匹配（避免"纸""杯"这类通用字误命中），非 prop 类型
    一律排除。

    Args:
        assets: 项目资产列表。
        names: LLM 输出的道具名列表（可空）。
        text_blob: 画面描述 + 对白 + 动作文本（用于兜底匹配）。

    Returns:
        命中的道具资产列表（按资产原顺序去重）。

    Raises:
        无：入参结构异常时返回空列表，不阻断分镜生成。
    """
    props = [
        asset
        for asset in assets
        if str(getattr(asset, "asset_type", "")) == "prop"
        and len(str(getattr(asset, "name", "") or "").strip()) >= 2
    ]
    if not props:
        return []

    wanted = [str(name).strip() for name in (names or []) if str(name).strip()]
    matched: list[Asset] = []
    for asset in props:
        name = str(asset.name).strip()
        if any(name == want or name in want or want in name for want in wanted):
            matched.append(asset)
    if matched:
        return matched

    blob = str(text_blob or "")
    if not blob:
        return []
    for asset in props:
        if str(asset.name).strip() in blob:
            matched.append(asset)
    return matched


#: 剧情功能标签合法集合（剧本锚点 plot_function 校验用）
_PLOT_FUNCTIONS: frozenset[str] = frozenset({
    "hook", "development", "escalation", "twist", "transition",
})


async def _safe_anchor_version_ids(session: AsyncSession, assets: list[Asset]) -> list[int]:
    """解析一组资产的安全锚点版本 ID（参考帧准入过滤）。

    缺陷修复（2026-10-29 多宫格画面）：历史实现直接取
    ``asset.current_version_id``，表情集（九宫格）等宫格类版本会成为
    渲染锚点。

    缺陷修复（2026-11-03 背面视图当锚点）：``is_current`` 只标记最新
    审批版本，角色补生成三视图后 ``turnaround_back`` 会顶掉 ``portrait``，
    下游拿背面图做首帧与 I2V 参考（实测首帧出现"无脸背影/无头躯干"）。
    现改为查询该资产**全部带图版本**，交由
    :func:`reference_guard.pick_safe_reference` 按
    ``portrait → turnaround_front → side → back`` 优先级择优。

    Args:
        session: 异步数据库会话。
        assets: 资产 ORM 实例列表（角色/场景/道具）。

    Returns:
        安全版本 ID 列表（去重升序；无可安全使用的版本时返回空列表）。

    Raises:
        无：单资产查询异常按缺失处理，不阻断分镜生成。
    """
    from app.services import reference_guard

    version_ids: set[int] = set()
    for asset in assets:
        if asset is None:
            continue
        try:
            rows = (
                (
                    await session.execute(
                        select(AssetVersion)
                        .where(AssetVersion.asset_id == int(asset.id))
                        .order_by(AssetVersion.version_no.asc())
                    )
                )
                .scalars()
                .all()
            )
        except Exception:  # noqa: BLE001 - 查询失败按无版本处理
            rows = []
        selected, warnings = reference_guard.pick_safe_reference(
            list(rows), asset_type=str(asset.asset_type or "character")
        )
        for message in warnings:
            logger.warning("锚点准入告警 asset=%s: %s", asset.name, message)
        if selected is not None:
            version_ids.add(int(selected.id))
    return sorted(version_ids)


def _build_script_anchor(
    item: DirectorShot,
    ref_lines: list[Any],
    idx: int,
    total: int,
    action_lines: list[str] | None = None,
) -> dict[str, str]:
    """构造镜头剧本锚点（LLM 输出优先，确定性兜底）。

    兜底规则（LLM 未输出 script_anchor 或字段非法时）：
    - ``script_text``：本镜台词行 + 动作行原文（含「道具：…/机位：…」），
      都为空时用画面描述；剧本锚点是「画面是否跑偏」的唯一比对基准，
      因此动作行必须一并写入（2026-11 修复）；
    - ``plot_function``：首镜 hook、末镜 hook（结尾钩子）、其余 development；
    - ``cause_from_prev``：首镜「开场」，其余「承接上一镜剧情」。

    Args:
        item: LLM 导演输出的单镜头契约。
        ref_lines: 本镜引用的台词行 ORM 列表（可空）。
        idx: 镜头下标（0 起）。
        total: 镜头总数。
        action_lines: 本镜绑定的剧本动作行原文（可空）。

    Returns:
        ``{script_text, plot_function, cause_from_prev}`` 字典（plot_function
        恒为合法枚举值）。
    """
    llm_anchor = item.script_anchor if isinstance(item.script_anchor, dict) else {}
    script_text = str(llm_anchor.get("script_text") or "").strip()
    action_part = "；".join(text for text in (action_lines or []) if text)
    if not script_text:
        spoken_part = ""
        if ref_lines:
            spoken_part = "；".join(
                f"{line.speaker_name or '旁白'}：{line.content}".strip("：")
                for line in ref_lines
                if str(line.content or "").strip()
            )
        script_text = "；".join(part for part in (spoken_part, action_part) if part)
    elif action_part and action_part not in script_text:
        # 2026-11 修复（QA 发现）：导演只摘台词写 script_text 时，动作行原文
        # 不会进入锚点，导致「剧本锚点↔画面」校验缺少动作基准。这里把动作行
        # 追加进锚点（台词在前、动作在后），保证锚点完整承载可拍信息。
        script_text = f"{script_text}；{action_part}"
    if not script_text:
        script_text = str(item.description or "").strip()
    plot_function = str(llm_anchor.get("plot_function") or "").strip()
    if plot_function not in _PLOT_FUNCTIONS:
        if idx == 0:
            plot_function = "hook"
        elif idx == total - 1:
            plot_function = "hook"
        else:
            plot_function = "development"
    cause = str(llm_anchor.get("cause_from_prev") or "").strip()
    if not cause:
        cause = "开场" if idx == 0 else "承接上一镜剧情"
    return {"script_text": script_text[:300], "plot_function": plot_function, "cause_from_prev": cause[:60]}


# =====================================================================
# LLM 导演契约与提示词
# =====================================================================


class DirectorShot(BaseModel):
    """LLM 导演输出的单镜头契约（枚举严格校验，违规触发自修）。

    Attributes:
        order_index: 镜头序（从 1 起）。
        shot_size: 景别（6 值枚举）。
        camera_move: 运镜（9 值枚举）。
        duration_sec: 时长（秒，2-6；允许小数，组装时四舍五入到毫秒）。
        description: 画面动作描述。
        emotion_tag: 情绪标签（8 值枚举或空）。
        dialogue_ref: 剧本行号（兼容旧契约：单句对白）。
        dialogue_refs: 剧本行号列表（P1-8：一个镜头可承载多句对白）。
        action_refs: 剧本动作行号列表（2026-11 修复：动作行不再在分镜层丢失）。
        character_names: 出场角色名列表。
        scene_name: 场景名。
        prop_names: 该镜出现的道具名列表（用于精确绑定道具资产）。
        composition: 可选构图提示，含 focus/subjects/props 的位置与归属。
        duration_ms: 归一化后的精确时长（内部字段，LLM 不输出）。
    """

    order_index: int = Field(ge=1)
    shot_size: str
    camera_move: str
    # LLM 经常输出 2.5 / 3.5 秒；旧版 int 契约会直接判为非法并触发整轮重试，
    # 实测三次重试后仍失败、分镜生成静默落空。改为 float，组装时 rounding。
    duration_sec: float = Field(ge=1, le=15)
    description: str
    emotion_tag: str | None = None
    dialogue_ref: int | None = None
    dialogue_refs: list[int] = []
    # 动作行号列表：此前动作行只存在于剧本文本，导演契约没有引用字段，
    # 组装层又把 refs 过滤成 spoken，导致「具象动作 + 道具 + 机位」原文整段
    # 丢失、只剩 ≤60 字 description。新增该字段后由 _bind_action_refs 兜底。
    action_refs: list[int] = []  # 参数注释：本镜引用的剧本动作行号列表
    character_names: list[str] = []
    scene_name: str | None = None
    prop_names: list[str] = []          # 参数注释：该镜出现的道具名（项目资产名）
    composition: dict[str, Any] | None = None  # 参数注释：LLM 构图提示（可选覆盖）
    # 剧本锚点（2026-10-29 剧情一致性治本项）：LLM 可选输出；缺失时组装层
    # 按 dialogue_refs 对应剧本行确定性兜底，保证每镜必有锚点才进入生成。
    script_anchor: dict[str, Any] | None = None  # 参数注释：剧本锚点（script_text/plot_function/cause_from_prev）
    duration_ms: int | None = None


# 单镜头时长契约区间（秒）：组装时钳位，保证 duration_ms ∈ [3000, 5000]
# 动态性要求：镜头过短只剩一个静止画面，过长则容易生成拖沓的"慢动作 PPT"。
_SHOT_DURATION_SEC_MIN: int = 3
_SHOT_DURATION_SEC_MAX: int = 5


class DirectorPlan(BaseModel):
    """LLM 导演输出契约（镜头序列）。

    Attributes:
        shots: 镜头列表。
    """

    shots: list[DirectorShot] = []


#: 运镜多样性均衡的替换池（避免全片 static 的"幻灯片感"，P2-5 修复）
_MOTION_POOL: tuple[str, ...] = ("dolly_in", "pan", "dolly_out", "tracking")

#: static 镜头占比上限：超过则自动替换为运动镜头（P2-5 修复）
_STATIC_RATIO_MAX = 0.5


def _diversify_camera_moves(shots: list[DirectorShot]) -> list[DirectorShot]:
    """运镜多样性均衡：static 占比 ≤50% 且全片至少 2 种运动镜头。

    P2-5 修复：LLM/模板回退大量产出 static，终验成片 8/11 镜静止
    （仅约 3% 缓推），镜头语言单一。这里在分镜组装前对计划做确定性
    均衡——按均匀间隔把部分 static 镜替换为运动镜头，替换值避开
    前一镜运镜，不引入随机性（同一剧本多次生成结果稳定）。

    Args:
        shots: 导演计划镜头列表（就地修改 camera_move）。

    Returns:
        同一列表（便于链式调用）。

    Raises:
        无：少于 3 镜直接返回，不做均衡。
    """

    def _is_static(shot: DirectorShot) -> bool:
        """判断镜头是否为静止运镜（含非法枚举值回退 static 的情况）。

        Args:
            shot: 导演计划镜头。

        Returns:
            True 表示该镜为 static。
        """
        return (
            shot.camera_move or "static"
        ) not in _CAMERA_MOVES or shot.camera_move == "static"

    total = len(shots)
    if total < 3:
        return shots
    static_idx = [i for i, shot in enumerate(shots) if _is_static(shot)]
    max_static = max(1, int(total * _STATIC_RATIO_MAX))
    need = max(0, len(static_idx) - max_static)
    picked: set[int] = set()
    if need > 0:
        if need == 1:
            picked.add(static_idx[len(static_idx) // 2])
        else:
            span = len(static_idx) - 1
            picked.update(
                static_idx[round(k * span / (need - 1))] for k in range(need)
            )
    # 运动镜头种类不足 2 时，再补一个 static 换成缺失种类
    motion_kinds = {
        shot.camera_move
        for shot in shots
        if shot.camera_move in _CAMERA_MOVES and shot.camera_move != "static"
    }
    if len(motion_kinds) < 2:
        rest = [i for i in static_idx if i not in picked]
        if rest:
            picked.add(rest[len(rest) // 2])
    for order_k, idx in enumerate(sorted(picked)):
        prev = shots[idx - 1].camera_move if idx > 0 else None
        candidate = _MOTION_POOL[(order_k + idx) % len(_MOTION_POOL)]
        if candidate == prev:
            candidate = _MOTION_POOL[(order_k + idx + 1) % len(_MOTION_POOL)]
        shots[idx].camera_move = candidate
    return shots


#: 景别相邻替换的候选顺序：优先向近景（情绪推进）/远景（信息交代）两向取邻
_SHOT_SIZE_NEIGHBORS: dict[str, tuple[str, ...]] = {
    "extreme_wide": ("wide", "extreme_close_up", "medium"),
    "wide": ("medium", "extreme_wide", "close_up"),
    "medium": ("close_up", "wide", "over_shoulder"),
    "over_shoulder": ("medium", "close_up", "wide"),
    "close_up": ("medium", "extreme_close_up", "over_shoulder"),
    "extreme_close_up": ("close_up", "medium", "wide"),
}


def _diversify_shot_sizes(shots: list[DirectorShot]) -> list[DirectorShot]:
    """景别序列规划：消除相邻镜头同景别连续堆砌（P0 分镜优化）。

    短剧成片中相邻镜头若连续使用同一景别（如"中景接中景"），剪辑节奏
    会呈"平推"感。导演 LLM 虽被约束景别可选值，但不保证序列多样性。
    这里在分镜组装前做确定性后处理：发现相邻同景别即替换为景别尺度上
    的邻近景别，优先取「与前后镜均不同」的候选，且跳过对话正反打
    （over_shoulder 天然成对，仅当三连同景别才干预），不引入随机性。

    Args:
        shots: 导演计划镜头列表（就地修改 shot_size）。

    Returns:
        同一列表（便于链式调用）。

    Raises:
        无：少于 2 镜直接返回，不做规划。
    """
    total = len(shots)
    if total < 2:
        return shots

    def _size(shot: DirectorShot) -> str:
        """读取镜头的有效景别（非法枚举值回退 medium 参与比较）。

        Args:
            shot: 导演计划镜头。

        Returns:
            景别枚举字符串。
        """
        return shot.shot_size if shot.shot_size in _SHOT_SIZES else "medium"

    for i in range(1, total):
        prev_size = _size(shots[i - 1])
        cur = shots[i]
        if _size(cur) != prev_size:
            continue
        # 对话正反打：over_shoulder 连续两镜是刻意的过肩交替，放行；
        # 仅当本镜是"三连同景别"的第 2 镜之后（前两镜均为 over_shoulder）才干预。
        if prev_size == "over_shoulder" and i >= 2 and _size(shots[i - 2]) != "over_shoulder":
            continue
        # 2026-11 修复（景别语义优先）：剧本明确要求特写 / 绑定道具特写的镜头
        # 不能被均衡改写（否则道具特写被拉成全景，载体信息直接丢失）。
        if "特写" in str(cur.description or "") or (cur.prop_names or []):
            logger.info(
                "景别均衡跳过语义特写镜 order_index=%s size=%s desc=%s",
                cur.order_index, _size(cur), str(cur.description or "")[:30],
            )
            continue
        next_size = _size(shots[i + 1]) if i + 1 < total else None
        replacement = None
        for cand in _SHOT_SIZE_NEIGHBORS.get(prev_size, ("medium", "close_up", "wide")):
            if cand == prev_size or (next_size is not None and cand == next_size):
                continue
            replacement = cand
            break
        if replacement is None:
            replacement = _SHOT_SIZES[(list(_SHOT_SIZES).index(prev_size) + 2) % len(_SHOT_SIZES)]
        cur.shot_size = replacement
    return shots


def _build_director_prompts(
    script_text: str,
    style_prefix: str,
    pacing_hint: str | None = None,
    template_rules: list[str] | None = None,
    target_shot_count: int | None = None,
    target_duration_ms: int | None = None,
    max_shot_duration_ms: int | None = None,
    character_appearance: str | None = None,
) -> tuple[str, str]:
    """构造分镜导演的五段式提示词（系统 + 用户）。

    Args:
        script_text: 该集剧本文本（含场次头与行号前缀）。
        style_prefix: 画风预设 prompt 前缀。
        pacing_hint: 节奏模式提示（V4.0，AC-F4-07-1；None 用默认节奏）。
        template_rules: 分镜模板机位规则条目（V4.0，AC-F4-10-1）。
        target_shot_count: 目标镜头数（P1-7：用户可控，空用默认 6~12）。
        target_duration_ms: 目标单集总时长（毫秒，空用提示词默认区间）。
        max_shot_duration_ms: 单镜头时长上限（毫秒，空用节奏档位上限）。
        character_appearance: 已锁定角色的外形基准（姓名 + 着装/发型摘要）。

    Returns:
        (系统提示词, 用户提示词) 二元组。
    """
    rules_block = ""
    if template_rules:
        rules_block = (
            "\n【模板机位规则（须遵守；与硬性质检规则冲突时硬校验优先）】\n"
            + "\n".join(f"- {rule}" for rule in template_rules)
        )
    # 2026-11 新增：把已锁定角色的外形基准交给导演，避免 description 里
    # 自行编造着装/发型（跨镜头角色形象漂移的源头之一）。
    appearance_block = ""
    if character_appearance:
        appearance_block = (
            "\n【人物外形基准（description 中的着装/发型必须沿用，禁止自行改服或换发型）】\n"
            f"{character_appearance}"
        )

    # P1-7 修复：镜头数/总时长/单镜上限由请求参数显式约束，不再固定 6~12 镜
    if target_shot_count:
        shot_count_hint = f"本集必须拆解为 {target_shot_count} 个镜头（允许 ±1）"
    else:
        shot_count_hint = "本集拆解为 6~12 个镜头"
    duration_hint = (
        f"本集镜头总时长约 {target_duration_ms / 1000:.0f} 秒"
        if target_duration_ms
        else "单集时长 60~180 秒"
    )
    max_sec = (
        max(_SHOT_DURATION_SEC_MIN, min(_SHOT_DURATION_SEC_MAX, round(max_shot_duration_ms / 1000)))
        if max_shot_duration_ms
        else _SHOT_DURATION_SEC_MAX
    )

    system = (
        "【角色与目标】你是竖屏短剧的导演（director）。任务：把单集剧本拆解为可执行的"
        f"镜头序列（{shot_count_hint}），每个镜头给出电影语言级的关键帧画面描述，供文生图使用。\n"
        f"【项目上下文】画风基调：{style_prefix or '写实电影感'}；画幅 9:16 竖屏；"
        f"{duration_hint}；短剧节奏要求前 3 秒抓人、结尾留钩子。\n"
        f"【节奏模式】{pacing_hint or '均衡：镜头均值 3000-4500ms，特写占比约 30%'}\n"
        "【工作区约定】1. 每镜头 duration_sec 取 3~"
        f"{max_sec}"
        " 秒（对白镜头按语速估算，不超过 "
        f"{max_sec}"
        "）；"
        "2. shot_size 仅允许 extreme_wide/wide/medium/close_up/extreme_close_up/over_shoulder，"
        "开场用 wide 建立环境，情绪爆发用 close_up/extreme_close_up；"
        "3. camera_move 仅允许 static/pan/tilt/dolly_in/dolly_out/tracking/orbit/handheld/crane；"
        "4. emotion_tag 仅允许 calm/joy/anger/sadness/fear/surprise/tension/romance 或 null；"
        "5. description 为 ≤120 字的纯视觉描述（人物动作、表情姿态、机位、光影、场景氛围），"
        "不要写对白内容；动作行原文（action_refs）才是动作与道具的第一来源，"
        "description 只做本镜画面摘要；description 必须用角色真名作主语，"
        "禁止用「他/她/两人/对方」指代（下游按角色名绑定角色参考图，代词会丢锚点）；"
        "description 中出场角色的着装必须沿用【人物外形基准】，禁止自行改服或换发型，"
        "换场后的首镜必须重新写明着装；"
        "6. dialogue_refs 填该镜头承载的全部对白行号（见输入行号前缀，可能多句），无对白填空数组；"
        "action_refs 填本镜对应的剧本【动作】行行号（见输入 [动作] 行，画面动作/道具/机位都在其中），"
        "无动作行填空数组——动作行若不被引用，下游将丢失具象动作与道具信息，视为不合格；"
        "禁止把场次摘要（「承接：…」这类因果句）当作分镜动作，description 也不得照抄场次摘要；"
        "7. character_names 只用剧本原角色名，禁止引入剧本未出现的角色；"
        "8. scene_name 用场次名称，同一场次的镜头必须连续，不得跨场混排；"
        "9. prop_names 只填项目道具资产名（无则空数组）；composition 可选，给出 "
        "subjects 中角色的 x（0~1）与 facing（left/right/camera/away），道具给 owner。"
        "\n10. 景别序列规划：相邻镜头景别不得连续相同（对话正反打的 over_shoulder 成对除外），"
        "按 远→全→中→近→特 递进安排景别节奏；"
        "11. 轴线规则：同场次遵守 180° 轴线，相邻镜头人物 facing 相对（一人 left 一人 right），"
        "不得无故越轴；人物视线朝向须与画外对象方位一致，动作方向前后镜连贯。"
        "12. 总时长预算（F21）：全部镜头 Σduration_sec 必须贴近剧本单集目标时长——"
        "30 秒规格 → 6~8 镜、Σ=28~32 秒；60 秒规格 → 12~15 镜；90 秒以上 → 18~25 镜。"
        "超预算时优先合并同场次对白到单镜或删除纯氛围过渡镜，禁止为凑镜头数而稀释节奏。"
        "\n13. 单动作约束（画面流畅度硬规则）：每个镜头只承载【一个】核心动作，"
        "description 中禁止出现多个连续动作（如「转身→跑过去→开门」必须拆成三镜）；"
        "奔跑、打斗等大幅度动作必须单独成镜，且 3-5 秒内画面围绕该动作展开。"
        "\n14. 剧本锚点（script_anchor，每镜必填）：script_text 填本镜对应的剧本原文句子"
        "（从输入剧本行复制，含行号引用的台词或动作行原文）；plot_function 填剧情功能标签，"
        "仅允许 hook（钩子/开场抓人）/development（发展）/escalation（冲突升级）/twist（反转）"
        "/transition（过渡）；cause_from_prev 用 ≤20 字说明本镜与前镜的因果关系"
        "（首镜填「开场」，须承接前情）。无 script_anchor 的镜头不得进入生成。"
        "\n15. 服装/发型连续性：description 中的角色着装与发型必须沿用剧本写明的描述"
        "与【人物外形基准】，换装只发生在剧本 stage_direction 声明处，跨场不得自行改服或换发型；"
        "同一角色在任一镜头描述中的着装用词必须与基准逐字一致（不得写成同义改写，"
        "如基准是「米白色粗针织开衫」就不能写成「米色毛衣」）；"
        "\n16. 道具连续性：prop_names 与剧本动作行「道具：」标注保持一致，"
        "道具的出现/损坏/移动状态须在 description 中延续，不得跨镜凭空复原或消失；"
        "\n17. 钩子密度：首镜（前 3 秒）必须呈现核心冲突或悬念，"
        "此后每连续 3 个镜头内至少 1 个 plot_function 为 hook/escalation/twist 的镜头；"
        "不足时调整镜头切分而非堆砌镜头数。"
        f"{rules_block}{appearance_block}\n"
        "【完整性要求】剧本中的每一句对白都必须至少被一个镜头引用一次（一句不漏、一句不重复）。"
        "对白密集处允许一个镜头引用多句。\n"
        "【预算与边界】禁止生成涉政、色情、暴力血腥、涉未成年人内容；禁止影射真实公众人物；"
        "禁止输出 JSON 以外的任何文字。\n"
        '【输出契约】严格按以下 JSON 结构输出：{"shots":[{"order_index":1,'
        '"shot_size":"wide","camera_move":"dolly_in","duration_sec":3,"description":"画面描述",'
        '"emotion_tag":"tension","dialogue_refs":[2,3],"action_refs":[4],"character_names":["角色名"],'
        '"scene_name":"场景名","prop_names":["道具名"],'
        '"script_anchor":{"script_text":"剧本原文句","plot_function":"hook",'
        '"cause_from_prev":"开场"},"composition":{"focus":"character"}}]}'
    )
    user = f"请把以下单集剧本拆解为镜头序列：\n\n{script_text}"
    return system, user


def _bind_dialogue_refs(
    shots: list[DirectorShot],
    lines_by_index: dict[int, ScriptLine],
    scene_name_by_id: dict[int, str],
) -> tuple[list[DirectorShot], list[int]]:
    """补齐未被引用的对白行，保证每句对白都绑定到镜头（P1-8 修复）。

    历史现象：LLM 只给部分对白标记 ``dialogue_ref``（13 句仅 5 句绑定），
    其余对白在分镜/配音/字幕链路中丢失。这里先归一化每镜的引用集合，再把
    漏绑对白按「同场次同镜头 → 场景名匹配 → 顺序就近」分配，必要时合并到
    同一镜头的多句对白。

    P0 修复：旁白（``narration``）同样需要绑定。此前只处理 ``dialogue``，
    导致画外音不进分镜、字幕与成片，30 秒短剧开场独白整段丢失。

    Args:
        shots: 导演输出的镜头列表（原地修改引用字段）。
        lines_by_index: 行号 → 剧本行映射。
        scene_name_by_id: 场景 ID → 场景名映射。

    Returns:
        ``(shots, 补齐的行号列表)``。
    """
    # 需要进入"台词轨"的行类型：对白 + 画外旁白（action 只用于画面描述）
    spoken_types = ("dialogue", "narration")
    used: set[int] = set()
    for shot in shots:
        refs: list[int] = []
        if shot.dialogue_ref:
            refs.append(int(shot.dialogue_ref))
        refs.extend(int(r) for r in (shot.dialogue_refs or []))
        valid = sorted(
            {
                r
                for r in refs
                if r in lines_by_index and lines_by_index[r].line_type in spoken_types
            }
        )
        shot.dialogue_refs = valid
        shot.dialogue_ref = valid[0] if valid else None
        used.update(valid)

    unbound = [
        idx
        for idx, line in lines_by_index.items()
        if line.line_type in spoken_types and idx not in used
    ]
    if not unbound or not shots:
        return shots, []

    scene_name_by_line: dict[int, str] = {
        idx: scene_name_by_id.get(int(line.scene_id), "")
        for idx, line in lines_by_index.items()
        if line.line_type in spoken_types
    }
    scene_id_by_line = {
        idx: int(line.scene_id) for idx, line in lines_by_index.items()
    }
    for idx in sorted(unbound):
        line = lines_by_index[idx]
        line_scene_id = int(line.scene_id)
        target: DirectorShot | None = None
        # 1) 同场次镜头：优先合并到已承载同场对白的镜头
        for shot in shots:
            if any(scene_id_by_line.get(r) == line_scene_id for r in shot.dialogue_refs):
                target = shot
                break
        # 2) 场景名匹配的镜头
        if target is None:
            scene_name = scene_name_by_line.get(idx, "")
            if scene_name:
                target = next((s for s in shots if (s.scene_name or "").strip() == scene_name), None)
        # 3) 顺序就近的镜头
        if target is None:
            position = int((idx - 1) / max(1, len(lines_by_index)) * len(shots))
            target = shots[min(len(shots) - 1, max(0, position))]
        if idx not in target.dialogue_refs:
            target.dialogue_refs.append(idx)
        if target.dialogue_ref is None:
            target.dialogue_ref = idx
        used.add(idx)
    return shots, sorted(unbound)


#: 剧本动作行里的中文机位标注 → shots.camera_move 枚举映射
_CAMERA_HINT_MAP: dict[str, str] = {
    "固定": "static",
    "推近": "dolly_in",
    "推进": "dolly_in",
    "拉远": "dolly_out",
    "拉出": "dolly_out",
    "环绕": "orbit",
    "跟随": "tracking",
    "跟拍": "tracking",
    "摇移": "pan",
    "横摇": "pan",
    "竖摇": "tilt",
    "升降": "crane",
    "手持": "handheld",
}


def _parse_action_annotations(text: str | None) -> tuple[list[str], str | None]:
    """从剧本动作行解析「道具：…」与「机位：…」标注。

    编剧提示词要求动作行以「道具：X；机位：Y」标注关键道具与运镜
    （见 screenwriter.py 的 _VISUAL_CRAFT），但这些标注此前只作为普通
    文本存在，道具绑定与运镜完全依赖导演 LLM 二次猜测。这里做确定性解析，
    让剧本原文直接驱动道具资产绑定与 camera_move。

    Args:
        text: 动作行原文（可空）。

    Returns:
        ``(道具名列表, 机位枚举或 None)``；解析失败返回 ``([], None)``。

    Raises:
        无：任何异常输入都返回空结果。
    """
    import re

    blob = str(text or "")
    props: list[str] = []
    for match in re.finditer(r"道具(?:：|:)([^；;\n。]+)", blob):
        for raw in re.split(r"[、,，/]", match.group(1)):
            name = raw.strip().strip("「」“”\"' ")
            if len(name) >= 2 and name not in props:
                props.append(name)
    camera: str | None = None
    camera_match = re.search(r"机位(?:：|:)([^；;\n。]+)", blob)
    if camera_match:
        phrase = camera_match.group(1).strip()
        for key, value in _CAMERA_HINT_MAP.items():
            if key in phrase:
                camera = value
                break
    return props, camera


def _bind_action_refs(
    shots: list[DirectorShot],
    lines_by_index: dict[int, Any],
    scene_name_by_id: dict[int, str],
) -> tuple[list[DirectorShot], list[int], dict[int, int]]:
    """把剧本「动作行」绑定到镜头，避免具象动作/道具/机位在分镜层丢失。

    背景（2026-11 修复）：此前导演契约只有 dialogue_refs，且组装层的
    refs 被过滤为 dialogue/narration，剧本里的 [动作] 行（含「具象动作 +
    道具：… + 机位：…」原文）永远进不了 shots.action_desc，每镜只剩
    导演的 ≤60 字 description，下游只能靠模型猜测动作。

    绑定策略（确定性、可复现，优先级从高到低）：
    1. LLM 显式给出的 ``action_refs``（仅接受 action 类型行号，去重）；
    2. 同场次内、与本镜台词行号距离 ≤3 的未使用动作行（就近优先）；
    3. 同场次剩余未使用动作行按镜头顺序补齐（每镜最多 2 条）。

    Args:
        shots: 导演输出的镜头列表（原地写入 action_refs）。
        lines_by_index: 行号 → 剧本行映射（含 action 行）。
        scene_name_by_id: 场景 ID → 场景名（无台词镜头的场次归属兜底）。

    Returns:
        ``(shots, 新增绑定的行号列表, {镜头在列表中的下标: 场次ID})``；
        场次 ID 供组装层在无动作行时用场景摘要兜底。

    Raises:
        无：结构异常按未绑定处理，不阻断分镜生成。
    """
    action_by_idx = {
        int(idx): line
        for idx, line in lines_by_index.items()
        if str(getattr(line, "line_type", "")) == "action"
    }

    scene_by_idx = {
        int(idx): int(getattr(line, "scene_id", 0) or 0)
        for idx, line in lines_by_index.items()
    }
    name_by_scene = {
        int(scene_id): _normalize_asset_name(name)
        for scene_id, name in scene_name_by_id.items()
    }

    used: set[int] = set()
    bound: list[int] = []
    # ---- 1. LLM 显式引用（同名动作行只归属第一个镜头，避免重复）----
    for shot in shots:
        valid: list[int] = []
        for raw in sorted({int(r) for r in (shot.action_refs or []) if str(r).isdigit()}):
            if raw in action_by_idx and raw not in used:
                used.add(raw)
                valid.append(raw)
                bound.append(raw)
        shot.action_refs = valid

    # ---- 镜头场次归属：台词行 → 显式动作行 → 场景名 → 前一镜 → 首场兜底 ----
    # 2026-11 QA 修复：无台词空镜/首镜此前场次归属为 0，导致场景资产绑定失败、
    # 提示词缺场景（镜头凭空落在白墙）。动作行本身携带 scene_id，可作第二来源；
    # 仍未命中时取剧本第一个场次（首镜合理归属），避免整镜无场景。
    default_scene_id = min(scene_by_idx.values()) if scene_by_idx else 0
    scene_of_position: dict[int, int] = {}
    last_scene: int = 0
    for position, shot in enumerate(shots):
        ref_scenes = [
            scene_by_idx[int(r)] for r in (shot.dialogue_refs or []) if int(r) in scene_by_idx
        ]
        if not ref_scenes:
            ref_scenes = [
                scene_by_idx[int(r)] for r in (shot.action_refs or []) if int(r) in scene_by_idx
            ]
        scene_id = 0
        if ref_scenes:
            scene_id = max(set(ref_scenes), key=ref_scenes.count)
        if not scene_id and shot.scene_name:
            wanted = _normalize_asset_name(shot.scene_name)
            for candidate_id, candidate_name in name_by_scene.items():
                if candidate_name and (wanted in candidate_name or candidate_name in wanted):
                    scene_id = candidate_id
                    break
        if not scene_id:
            scene_id = last_scene or default_scene_id
        scene_of_position[position] = scene_id
        last_scene = scene_id

    # ---- 2. 同场次就近分配 ----
    def _nearest_ref(shot: DirectorShot) -> int:
        """取镜头台词行号的中位数作为就近分配锚点。

        Args:
            shot: 导演镜头。

        Returns:
            锚点行号；无台词时返回 0。
        """
        refs = sorted(int(r) for r in (shot.dialogue_refs or []))
        return refs[len(refs) // 2] if refs else 0

    for position, shot in enumerate(shots):
        if shot.action_refs:
            continue
        scene_id = scene_of_position[position]
        candidates = [
            idx
            for idx, line in action_by_idx.items()
            if idx not in used and scene_by_idx.get(idx) == scene_id
        ]
        if not candidates:
            continue
        anchor = _nearest_ref(shot)
        nearest = min(candidates, key=lambda idx: (abs(idx - anchor), idx)) if anchor else min(candidates)
        if anchor and abs(nearest - anchor) > 3:
            continue
        used.add(nearest)
        shot.action_refs = [nearest]
        bound.append(nearest)

    # ---- 3. 同场次剩余动作行按顺序补齐（每镜最多 2 条）----
    for position, shot in enumerate(shots):
        if len(shot.action_refs) >= 2:
            continue
        scene_id = scene_of_position[position]
        remaining = sorted(
            idx
            for idx, line in action_by_idx.items()
            if idx not in used and scene_by_idx.get(idx) == scene_id
        )
        while remaining and len(shot.action_refs) < 2:
            picked = remaining.pop(0)
            used.add(picked)
            shot.action_refs.append(picked)
            bound.append(picked)

    return shots, sorted(set(bound)), scene_of_position


def _apply_shot_targets(
    shots: list[DirectorShot],
    *,
    target_shot_count: int | None,
    target_duration_ms: int | None,
    duration_min_ms: int,
    duration_max_ms: int,
    max_shot_duration_ms: int | None,
) -> list[DirectorShot]:
    """按目标镜头数与目标总时长裁剪/归一化镜头（P1-7 修复）。

    Args:
        shots: 已补齐对白引用的镜头列表。
        target_shot_count: 目标镜头数（空表示不裁剪）。
        target_duration_ms: 目标总时长（空表示不归一化）。
        duration_min_ms: 单镜时长下限（来自节奏档位）。
        duration_max_ms: 单镜时长上限（来自节奏档位）。
        max_shot_duration_ms: 用户指定的单镜上限（可空）。

    Returns:
        处理后的镜头列表（保持原顺序）。

    Raises:
        无。
    """
    result = list(shots)
    max_ms = max(1000, int(duration_max_ms))
    if max_shot_duration_ms:
        max_ms = min(max_ms, max(1000, int(max_shot_duration_ms)))
    min_ms = max(1000, min(int(duration_min_ms), max_ms))

    # ---- 镜头数裁剪：对白/旁白镜头与首尾镜头优先保留，其余均匀采样 ----
    # P0 修复：被裁镜头的动作描述不再直接丢弃，而是合并到最近保留镜头，
    # 避免短剧结尾（留纸条/风铃）等关键动作在 30 秒版中整段消失。
    if target_shot_count and len(result) > int(target_shot_count):
        mandatory = [i for i, shot in enumerate(result) if shot.dialogue_refs]
        # 首镜承担建置、末镜承担收尾，必须保留
        mandatory = sorted(set(mandatory + [0, len(result) - 1]))
        others = [i for i in range(len(result)) if i not in mandatory]
        slots = max(0, int(target_shot_count) - len(mandatory))
        if slots and others:
            if slots >= len(others):
                sampled = others
            elif slots == 1:
                sampled = [others[len(others) // 2]]
            else:
                step = (len(others) - 1) / (slots - 1)
                sampled = sorted({others[int(round(i * step))] for i in range(slots)})
            keep = sorted(mandatory + sampled)
            kept_shots = [result[i] for i in keep]
            # 被裁镜头内容向最近的前一个保留镜头归并（无前驱时向后归并）
            kept_index = 0
            for original_index, shot in enumerate(result):
                if original_index in keep:
                    kept_index = keep.index(original_index)
                    continue
                anchor_index = max(0, kept_index)
                anchor = kept_shots[anchor_index]
                extra = (shot.description or "").strip()
                if extra and extra not in (anchor.description or ""):
                    anchor.description = f"{(anchor.description or '').strip()}；{extra}"[:120]
                for ref in shot.dialogue_refs:
                    if ref not in anchor.dialogue_refs:
                        anchor.dialogue_refs.append(ref)
                if anchor.dialogue_ref is None and anchor.dialogue_refs:
                    anchor.dialogue_ref = anchor.dialogue_refs[0]
            result = kept_shots

    # ---- 总时长归一化：先按比例缩放，再钳位并二次分配余量 ----
    if target_duration_ms and result:
        durations = [
            _clamp_int(
                int(shot.duration_ms or round(float(shot.duration_sec) * 1000)), min_ms, max_ms
            )
            for shot in result
        ]
        total = sum(durations)
        target = max(len(result) * min_ms, int(target_duration_ms))
        if total > 0:
            scale = target / total
            durations = [_clamp_int(int(round(d * scale)), min_ms, max_ms) for d in durations]
            for _ in range(4):
                diff = target - sum(durations)
                if abs(diff) <= 50:
                    break
                adjustable = [
                    i
                    for i, value in enumerate(durations)
                    if (diff > 0 and value < max_ms) or (diff < 0 and value > min_ms)
                ]
                if not adjustable:
                    break
                share = int(diff / len(adjustable))
                if share == 0:
                    share = 1 if diff > 0 else -1
                for i in adjustable:
                    durations[i] = _clamp_int(durations[i] + share, min_ms, max_ms)
        for shot, value in zip(result, durations):
            shot.duration_ms = value
            shot.duration_sec = max(1, int(round(value / 1000)))
    else:
        for shot in result:
            value = _clamp_int(
                int(shot.duration_ms or round(float(shot.duration_sec) * 1000)), min_ms, max_ms
            )
            shot.duration_ms = value
    return result


def _clamp_int(value: int, low: int, high: int) -> int:
    """把整数限制在闭区间内。

    Args:
        value: 原始值。
        low: 下限。
        high: 上限。

    Returns:
        钳位后的整数。
    """
    return max(low, min(high, int(value)))


def _validate_shot_count(
    shots: list[DirectorShot],
    *,
    target_shot_count: int | None,
) -> tuple[list[DirectorShot], dict[str, Any] | None]:
    """镜头数后校验（Q-019）：产出偏离目标 ±1 时自动修正并记录。

    ``_apply_shot_targets`` 只处理「产出多于目标」的裁剪；LLM 少产出
    （如目标 5 镜只回 2 镜）时此前不做任何处理，成片节奏失真。本函数：

    - 产出在 ``[target-1, target+1]`` 区间内 → 不干预；
    - 产出多于 target+1 → 不再裁剪（裁剪属 :func:`_apply_shot_targets`），
      仅记录 ``over``；
    - 产出少于 target-1 → 反复拆分「多句对白或时长 ≥4s」的最长镜头
      （对白引用均分、时长对半、描述两镜共用），直至进入容差区间或
      无可拆镜头。

    Args:
        shots: 已完成对白绑定的镜头列表。
        target_shot_count: 目标镜头数（空表示不校验）。

    Returns:
        ``(修正后镜头列表, 校验记录)``；未启用目标数时记录为 None。
        记录形状 ``{"target", "generated", "final", "action"}``，
        action ∈ ok/split/over。
    """
    if not target_shot_count:
        return shots, None
    target = int(target_shot_count)
    generated = len(shots)
    if target - 1 <= generated <= target + 1:
        return shots, {"target": target, "generated": generated, "final": generated, "action": "ok"}

    adjusted = list(shots)
    action = "over" if generated > target else "split"
    # 少产出：拆分长镜头补足（对白多句优先，其次长时长镜头）
    while len(adjusted) < target - 1:
        splittable = [
            s for s in adjusted
            if len(s.dialogue_refs) >= 2 or int(s.duration_ms or 0) >= 4000
        ]
        if not splittable:
            break
        victim = max(splittable, key=lambda s: (len(s.dialogue_refs), int(s.duration_ms or 0)))
        idx = adjusted.index(victim)
        refs = list(victim.dialogue_refs)
        if len(refs) >= 2:
            mid = len(refs) // 2
            refs_a, refs_b = refs[:mid], refs[mid:]
        else:
            refs_a, refs_b = [], refs
        dur = int(victim.duration_ms or 0) or int(round(float(victim.duration_sec or 3) * 1000))
        dur_a = max(1000, dur // 2)
        dur_b = max(1000, dur - dur_a)
        shot_a = victim.model_copy(update={
            "dialogue_refs": refs_a,
            "dialogue_ref": refs_a[0] if refs_a else victim.dialogue_ref,
            "duration_ms": dur_a,
            "duration_sec": max(1, int(round(dur_a / 1000))),
        })
        shot_b = victim.model_copy(update={
            "dialogue_refs": refs_b,
            "dialogue_ref": refs_b[0] if refs_b else None,
            "duration_ms": dur_b,
            "duration_sec": max(1, int(round(dur_b / 1000))),
        })
        adjusted[idx:idx + 1] = [shot_a, shot_b]
    record = {"target": target, "generated": generated, "final": len(adjusted), "action": action}
    if action == "split" and len(adjusted) != generated:
        logger.warning(
            "镜头数不足已自动拆分 target=%s generated=%s final=%s", target, generated, len(adjusted)
        )
    return adjusted, record


async def _episode_script_text(session: AsyncSession, episode: Episode) -> str:
    """拼接某集的剧本文本（场次标题 + 行号前缀行）。

    Args:
        session: 异步数据库会话。
        episode: 分集 ORM 实例。

    Returns:
        截断后的剧本文本（上限 _DIRECTOR_TEXT_LIMIT 字符）。
    """
    scenes = (
        await session.execute(
            select(Scene)
            .where(Scene.episode_id == episode.id)
            .order_by(Scene.scene_no.asc())
        )
    ).scalars().all()
    lines = (
        await session.execute(
            select(ScriptLine)
            .join(Scene, Scene.id == ScriptLine.scene_id)
            .where(Scene.episode_id == episode.id)
            .order_by(ScriptLine.scene_id.asc(), ScriptLine.line_no.asc())
        )
    ).scalars().all()
    parts: list[str] = [f"第 {episode.episode_no} 集《{episode.title or ''}》：{episode.synopsis or ''}"]
    scene_by_id = {int(s.id): s for s in scenes}
    scene_titles = {s.id: (s.title or s.location or f"场次{s.scene_no}") for s in scenes}
    # 2026-11 修复（导演缺上下文）：此前只给「场景名 + 行内容」，场次的
    # location 完整描述（时间/氛围）与 summary（承接因果）全部丢失，导演
    # 只能看到台词和动作行。这里在首个行之前插入场次头，把三者一并交给
    # 导演；行号仍与 lines_by_index 完全一致（按 scene_id + line_no 顺序）。
    current_scene_id: int | None = None
    for idx, line in enumerate(lines, start=1):
        scene_id = int(line.scene_id)
        if scene_id != current_scene_id:
            current_scene_id = scene_id
            scene = scene_by_id.get(scene_id)
            if scene is not None:
                head = [
                    f"\n【场次 {scene.scene_no}】{(scene.location or scene.title or '').strip()}",
                    str(scene.time_of_day or ""),
                    str(scene.int_ext or ""),
                ]
                if str(scene.summary or "").strip():
                    head.append(f"场摘要：{str(scene.summary).strip()}")
                parts.append(" | ".join(item for item in head if item))
        scene_name = scene_titles.get(line.scene_id, "")
        if line.line_type == "dialogue":
            parts.append(f"{idx}. [{scene_name}][对白] {line.speaker_name or '未知'}：{line.content}")
        elif line.line_type == "narration":
            parts.append(f"{idx}. [{scene_name}][旁白] {line.content}")
        else:
            parts.append(f"{idx}. [{scene_name}][动作] {line.content}")
    return "\n".join(parts)[:_DIRECTOR_TEXT_LIMIT]


# =====================================================================
# 分镜生成（generate）
# =====================================================================


async def _project_style_prefix(session: AsyncSession, project_id: int) -> str:
    """查询项目画风预设的 prompt 前缀。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        prompt 前缀字符串（未命中返回空串）。
    """
    project = await session.get(Project, project_id)
    if project is None or not project.style_preset_key:
        return ""
    row = (
        await session.execute(
            select(StylePreset).where(StylePreset.preset_key == project.style_preset_key)
        )
    ).scalar_one_or_none()
    return str(row.prompt_prefix) if row is not None and row.prompt_prefix else ""


async def generate_storyboard(
    session: AsyncSession,
    project: Project,
    episode_no: int,
    user_id: int,
    pacing_mode: str | None = None,
    template_id: str | None = None,
    target_shot_count: int | None = None,
    target_duration_ms: int | None = None,
    max_shot_duration_ms: int | None = None,
) -> dict[str, Any]:
    """发起分镜生成（202 异步任务模型；同集已有分镜板时幂等返回）。

    校验链：项目有剧本 → 集号存在 → 幂等检查。通过后启动后台管线：
    LLM 导演 → 确定性组装 shots → 简单 QC → pending_review → 事件。

    V4.0：``pacing_mode`` / ``template_id`` 先落 ``director_notes`` 再拆解，
    使拆解参数（镜头均值/特写占比/机位规则）按所选模式取值（AC-F4-07-1、
    AC-F4-10-1）。

    Args:
        session: 请求级数据库会话（仅校验，后台任务自建会话）。
        project: 项目 ORM 实例（ACL 已由路由层保证）。
        episode_no: 集号。
        user_id: 发起用户 ID。
        pacing_mode: 叙事节奏模式（fast/balanced/slow，可空）。
        template_id: 分镜模板 ID（可空）。
        target_shot_count: 目标镜头数（P1-7，可空；空则用默认 6~12 镜）。
        target_duration_ms: 目标单集时长（毫秒，可空）。
        max_shot_duration_ms: 单镜头时长上限（毫秒，可空）。

    Returns:
        ``{"idempotent": bool, ...}``——idempotent=True 时携带已有
        storyboard 摘要；False 为受理回执。

    Raises:
        BizError: 10002 项目尚无剧本或集号不存在；40001 项目处于
            paused 等禁止生成状态（D15：受理前校验，不再静默丢弃）；
            40004 项目已归档（只读，兜底校验）。
    """
    # 受理前置状态校验（D15 修复）：先于一切副作用，杜绝"返回受理但后台丢弃"
    if project.status in _STORYBOARD_FORBIDDEN_STATUSES:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"项目状态 {project.status} 不允许发起分镜生成"
            + ("（归档项目为只读）" if project.status == "archived" else "（请先恢复项目）"),
        )
    script = (
        await session.execute(
            select(Script)
            .where(
                Script.project_id == project.id,
                Script.id == project.current_script_id,
            )
            .order_by(Script.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if script is None:
        script = (
            await session.execute(
                select(Script)
                .where(Script.project_id == project.id)
                .order_by(Script.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，请先生成剧本")
    episode = (
        await session.execute(
            select(Episode).where(Episode.script_id == script.id, Episode.episode_no == episode_no)
        )
    ).scalar_one_or_none()
    if episode is None:
        # D32 伴生兜底：current_script_id 指向的版本工作副本可能缺该集
        # （历史 CASCADE 事故/版本未同步），回退到含该集的最新版本解析
        episode = (
            await session.execute(
                select(Episode)
                .join(Script, Episode.script_id == Script.id)
                .where(Script.project_id == project.id, Episode.episode_no == episode_no)
                .order_by(Script.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if episode is None:
        raise BizError(ErrorCode.NOT_FOUND, f"集号 {episode_no} 不存在，请先确认剧本分集")

    existed = await StoryboardRepository.get_by_episode(session, int(episode.id))
    if existed is not None:
        return {
            "idempotent": True,
            "storyboard": storyboard_out(existed, episode_no=episode_no),
            "message": "该集已存在分镜板，直接返回已有结果",
        }

    # 修复（E2E 报告问题 3）：受理前统计资产就绪度并在回执中返回。
    # 此前资产仅 1/39 锁定时也能直接受理分镜生成且零提示，产出的镜头
    # 缺少形象基准。此处不阻断生成（避免小成本试跑场景被卡死），
    # 由前端依据 asset_readiness 弹二次确认，明确告知未锁定数量。
    asset_statuses = (
        await session.execute(
            select(Asset.status).where(Asset.project_id == int(project.id))
        )
    ).scalars().all()
    total_assets = len(asset_statuses)
    locked_assets = sum(1 for s in asset_statuses if s == "locked")
    asset_readiness = {
        "total": total_assets,
        "locked": locked_assets,
        "unlocked": total_assets - locked_assets,
        "ready": total_assets > 0 and locked_assets == total_assets,
    }

    # V4.0：节奏模式 / 分镜模板先落 director_notes，供拆解节点读取参数
    if pacing_mode is not None or template_id is not None:
        from app.schemas.storyboard import DirectorNoteRequest

        await upsert_director_note(
            session,
            project_id=int(project.id),
            body=DirectorNoteRequest(
                pacing_mode=pacing_mode or "balanced",
                episode_no=episode_no,
                template_id=template_id,
            ),
            user_id=user_id,
        )

    _spawn_bg_task(
        _run_storyboard_generation(
            int(project.id),
            int(episode.id),
            user_id,
            target_shot_count=target_shot_count,
            target_duration_ms=target_duration_ms,
            max_shot_duration_ms=max_shot_duration_ms,
        )
    )
    await event_bus.publish(int(project.id), "storyboard_generate.started", {"episode_no": episode_no})
    # ---- 状态推进钩子：发起分镜生成即进入分镜阶段 ----
    # 修复：资产复用/迁移场景下项目可能停在 asset_preparing（未经过逐资产
    # 锁定的状态推进钩子），旧代码只处理 asset_review，导致分镜生成后项目
    # 状态不前进、后续批量渲染与审批链路卡住。这里按状态机链式推进。
    from app.services.project_service import transition

    stage_targets = []
    if project.status == "asset_preparing":
        stage_targets.append(("asset_review", "资产已就绪，进入资产审核"))
    if project.status in ("asset_preparing", "asset_review"):
        stage_targets.append(("storyboarding", "发起分镜生成，进入分镜阶段"))
    for target, reason in stage_targets:
        try:
            await transition(project, target, reason, session=session, user_id=user_id)
        except BizError as exc:
            logger.warning(
                "发起分镜生成后推进 %s 失败 project_id=%s: %s",
                target, project.id, exc.message,
            )
    return {
        "idempotent": False,
        "status": "accepted",
        "episode_no": episode_no,
        "asset_readiness": asset_readiness,
        "message": (
            "分镜生成任务已受理，进度请订阅项目事件流"
            if asset_readiness["ready"]
            else f"分镜生成任务已受理（仍有 {asset_readiness['unlocked']} 项资产未锁定，生成镜头可能缺少形象基准）"
        ),
    }


async def _resolve_pacing(
    session: AsyncSession, project_id: int, episode_id: int
) -> tuple[str, int, int, list[str] | None]:
    """解析该集的节奏模式与模板规则（V4.0 AC-F4-07-1 / AC-F4-10-1）。

    优先级：按集策略 > 全剧策略 > 默认 balanced。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。
        episode_id: 集 ID。

    Returns:
        ``(pacing_hint, duration_min_ms, duration_max_ms, template_rules)``；
        ``template_rules`` 无模板时为 None。
    """
    from app.services.template_packs import get_storyboard_template

    note = (
        await session.execute(
            select(DirectorNote).where(
                DirectorNote.project_id == project_id,
                DirectorNote.episode_id == episode_id,
            )
        )
    ).scalar_one_or_none()
    if note is None:
        note = (
            await session.execute(
                select(DirectorNote).where(
                    DirectorNote.project_id == project_id,
                    DirectorNote.episode_id.is_(None),
                )
            )
        ).scalar_one_or_none()

    mode = (note.pacing_mode if note else None) or "balanced"
    profile = PACING_PROFILES.get(mode, PACING_PROFILES["balanced"])
    duration_min_ms = int(profile["shot_duration_min"])
    duration_max_ms = int(profile["shot_duration_max"])
    hint = f"{profile['label']}：{profile['hint']}"
    if note is not None and note.dialogue_camera_rule:
        hint += f"；对话机位规则：{note.dialogue_camera_rule}"

    rules: list[str] | None = None
    if note is not None and note.template_id:
        template = get_storyboard_template(note.template_id)
        if template is not None:
            rules = list(template.get("rules") or [])
        else:
            custom = await session.get(
                StoryboardTemplate,
                int(note.template_id.split("_", 1)[1])
                if note.template_id.startswith("custom_")
                and note.template_id.split("_", 1)[1].isdigit()
                else 0,
            )
            if custom is not None and custom.rules:
                rules = list(custom.rules)
    return hint, duration_min_ms, duration_max_ms, rules


async def _run_storyboard_generation(
    project_id: int,
    episode_id: int,
    user_id: int,
    *,
    target_shot_count: int | None = None,
    target_duration_ms: int | None = None,
    max_shot_duration_ms: int | None = None,
) -> None:
    """后台任务主体：LLM 导演 → 组装 shots → QC → 状态流转 → 事件。

    Args:
        project_id: 项目 ID。
        episode_id: 集 ID。
        user_id: 发起用户 ID。
        target_shot_count: 目标镜头数（P1-7，可空）。
        target_duration_ms: 目标单集时长（毫秒，可空）。
        max_shot_duration_ms: 单镜时长上限（毫秒，可空）。

    Raises:
        无（异常兜底发布 ``storyboard_generate.failed``，不向调用方传播）。
    """
    episode_no: int | None = None
    try:
        async with async_session_factory() as session:
            project = await session.get(Project, project_id)
            episode = await session.get(Episode, episode_id)
            if project is None or episode is None:
                return
            episode_no = int(episode.episode_no)
            style = await _project_style_prefix(session, project_id)
            script_text = await _episode_script_text(session, episode)
            pacing_hint, duration_min_ms, duration_max_ms, template_rules = (
                await _resolve_pacing(session, project_id, episode_id)
            )
            # 导演提示词补入已锁定角色的外形基准（着装/发型），避免 description
            # 自行编造与资产设定冲突的形象（跨镜头角色漂移的源头）。
            character_rows = (
                await session.execute(
                    select(Asset).where(
                        Asset.project_id == project_id,
                        Asset.asset_type == "character",
                        Asset.status == "locked",
                    )
                )
            ).scalars().all()
            character_appearance = "\n".join(
                f"  - {asset.name}：{_appearance_summary(asset, 120)}"
                for asset in character_rows
                if _appearance_summary(asset, 120)
            )
            system, user = _build_director_prompts(
                script_text,
                style,
                pacing_hint=pacing_hint,
                template_rules=template_rules,
                target_shot_count=target_shot_count,
                target_duration_ms=target_duration_ms,
                max_shot_duration_ms=max_shot_duration_ms,
                character_appearance=character_appearance or None,
            )
            plan = await call_llm_contract(
                system=system, user=user, contract_type=DirectorPlan, project_id=project_id,
                temperature=0.7,
            )
            # 行号 → 剧本行映射（dialogue_ref 解析用，行号与提示词编号一致）
            lines = (
                await session.execute(
                    select(ScriptLine)
                    .join(Scene, Scene.id == ScriptLine.scene_id)
                    .where(Scene.episode_id == episode_id)
                    .order_by(ScriptLine.scene_id.asc(), ScriptLine.line_no.asc())
                )
            ).scalars().all()
            lines_by_index = {idx + 1: line for idx, line in enumerate(lines)}

            # ---- P1-8：补齐漏绑对白；P1-7：按目标镜头数/时长裁剪归一化 ----
            scene_rows = (
                await session.execute(
                    select(Scene).where(Scene.episode_id == episode_id)
                )
            ).scalars().all()
            scene_name_by_id = {
                int(scene.id): str(
                    getattr(scene, "location", None)
                    or getattr(scene, "title", None)
                    or getattr(scene, "name", None)
                    or ""
                )
                for scene in scene_rows
            }
            plan.shots, bound_rows = _bind_dialogue_refs(
                list(plan.shots), lines_by_index, scene_name_by_id
            )
            if bound_rows:
                logger.info(
                    "分镜对白补齐 project_id=%s episode_id=%s rows=%s",
                    project_id, episode_id, bound_rows[:20],
                )
            plan.shots = _apply_shot_targets(
                list(plan.shots),
                target_shot_count=target_shot_count,
                target_duration_ms=target_duration_ms,
                duration_min_ms=duration_min_ms,
                duration_max_ms=duration_max_ms,
                max_shot_duration_ms=max_shot_duration_ms,
            )
            # Q-019：镜头数后校验——产出少于目标-1 时拆分长台词镜补足，
            # 校验结果随完成事件写入项目 config._shot_count_adjustment。
            plan.shots, shot_count_adjustment = _validate_shot_count(
                list(plan.shots),
                target_shot_count=target_shot_count,
            )
            # 动作行绑定放在裁剪之后：被裁掉的镜头不应占用动作行资源。
            # scene_summary_by_id 供「本镜没有任何动作行」时兜底，保证
            # action_desc 不会退化为空、进而让 motion 层凭空编造动作。
            scene_summary_by_id = {
                int(scene.id): str(getattr(scene, "summary", None) or "").strip()
                for scene in scene_rows
            }
            plan.shots, bound_action_rows, scene_of_shot = _bind_action_refs(
                list(plan.shots), lines_by_index, scene_name_by_id
            )
            if bound_action_rows:
                logger.info(
                    "分镜动作行绑定 project_id=%s episode_id=%s rows=%s",
                    project_id, episode_id, bound_action_rows[:20],
                )

            # P0 修复：剧本行 → 角色资产绑定在资产锁定后自动回填。
            # 此前解析后 character_asset_id 恒为 NULL，配音无法区分角色音色，
            # 分镜也无法把对白归到具体人物。
            project_assets = (
                await session.execute(
                    select(Asset).where(
                        Asset.project_id == project_id,
                        Asset.status == "locked",
                    )
                )
            ).scalars().all()
            for line in lines:
                if line.speaker_name and not line.character_asset_id:
                    speaker_asset = _match_asset(project_assets, line.speaker_name, "character")
                    if speaker_asset is not None:
                        line.character_asset_id = int(speaker_asset.id)

            storyboard = Storyboard(
                project_id=project_id,
                episode_id=episode_id,
                version_no=1,
                status="draft",
                shot_count=0,
                estimated_duration_ms=0,
            )
            session.add(storyboard)
            await session.flush()
            # 2026-11 QA 修复：生成分镜后项目未回写 current_storyboard_id，
            # 其他页面/外部调用按该字段定位分镜时会拿到 null。
            project.current_storyboard_id = int(storyboard.id)

            # P2-5 修复：组装前做运镜多样性均衡（static ≤50%、至少 2 种
            # 运动镜头），避免 LLM/模板回退大量产出 static 导致镜头语言单一。
            plan.shots = _diversify_camera_moves(plan.shots)
            # P0 分镜优化：景别序列规划，消除相邻镜头同景别连续堆砌。
            plan.shots = _diversify_shot_sizes(plan.shots)
            # 场景资产未匹配的镜头下标（组装后转成 QC warning）
            scene_unbound_idx: set[int] = set()
            # 没有引用到任何剧本动作行的镜头下标（组装后转成 QC warning：
            # 这类镜头只能靠导演 description，动作细节最易落空）。
            action_gap_idx: set[int] = set()
            # 靠场次出场角色兜底绑定角色的镜头下标（QC warning：提示剧本
            # 动作行缺角色名，应回到剧本侧用真名重写）。
            character_fallback_idx: set[int] = set()
            # 2026-11 QA 修复（角色锚点丢失 → 成片形象漂移）：
            # 剧本动作行常用「她/他」指代而非角色名，只按镜头文本匹配角色会
            # 完全匹配不到人，I2V 便没有任何角色参考图可用。这里按场次预先
            # 汇总「本场出场角色资产」（该场全部行的文本命中 + 说话人），
            # 供无角色镜头的场次级兜底绑定。
            character_assets = [
                asset for asset in project_assets
                if str(getattr(asset, "asset_type", "")) == "character"
                and str(getattr(asset, "name", "") or "").strip()
            ]
            scene_character_assets: dict[int, list[Asset]] = {}
            for scene in scene_rows:
                scene_id = int(scene.id)
                scene_blob = " ".join(
                    str(line.content or "")
                    for line in lines
                    if int(getattr(line, "scene_id", 0) or 0) == scene_id
                )
                scene_character_assets[scene_id] = [
                    asset for asset in character_assets
                    if len(str(asset.name)) >= 2
                    and str(asset.name) in scene_blob
                ]
            # ---- 确定性组装：每 shot 落 shots（status=draft）----
            for idx, item in enumerate(plan.shots):
                shot_size = item.shot_size if item.shot_size in _SHOT_SIZES else "medium"
                camera_move = item.camera_move if item.camera_move in _CAMERA_MOVES else "static"
                emotion = item.emotion_tag if item.emotion_tag in _EMOTION_TAGS else None
                max_shot_ms = (
                    min(duration_max_ms, int(max_shot_duration_ms))
                    if max_shot_duration_ms
                    else duration_max_ms
                )
                duration_ms = _clamp_int(
                    int(item.duration_ms or round(float(item.duration_sec) * 1000)),
                    max(motion_service.SHOT_DURATION_MIN_MS, duration_min_ms),
                    max(
                        motion_service.SHOT_DURATION_MIN_MS,
                        min(motion_service.SHOT_DURATION_MAX_MS, max_shot_ms),
                    ),
                )
                dialogue: str | None = None
                action_desc: str | None = None
                # P1-8：一个镜头可承载多句对白（按行号顺序用换行拼接）
                refs = list(item.dialogue_refs or [])
                if not refs and item.dialogue_ref:
                    refs = [int(item.dialogue_ref)]
                ref_lines = [lines_by_index[r] for r in refs if r in lines_by_index]
                # P0 修复：旁白（narration）与对白一样进入台词轨，
                # 否则画外音无法配音、字幕和成片。
                spoken_lines = [
                    line for line in ref_lines if line.line_type in ("dialogue", "narration")
                ]
                dialogue_lines = [
                    str(line.content).strip()
                    for line in spoken_lines
                    if str(line.content or "").strip()
                ]
                if dialogue_lines:
                    dialogue = "\n".join(dialogue_lines)
                # 2026-11 修复（动作行丢失）：动作描述改从剧本 [动作] 行原文
                # 读取。此前 ref_lines 已被 _bind_dialogue_refs 过滤成
                # dialogue/narration，action 行永远取不到，action_desc 只剩
                # 导演的 ≤60 字 description。action_refs 由 _bind_action_refs
                # 确定性补齐，保留「具象动作 + 道具：… + 机位：…」全文。
                action_lines = [
                    str(lines_by_index[r].content).strip()
                    for r in (int(x) for x in (item.action_refs or []) if str(x).isdigit())
                    if r in lines_by_index
                    and str(lines_by_index[r].line_type) == "action"
                    and str(lines_by_index[r].content or "").strip()
                ]
                if action_lines:
                    action_desc = "；".join(action_lines)
                else:
                    # 2026-11 QA 修复（摘要冒充动作）：场次 summary 是因果摘要
                    # （以「承接：…」开头），不是可拍摄动作；塞进 action_desc 会
                    # 被运镜层当成动作复述，验收也会判为「动作空洞」。
                    # 兜底顺序改为：导演 description（纯视觉描述）优先，其次
                    # 才用不含因果引子的场次摘要，最后留空交由运镜层走中性动作。
                    description_text = str(item.description or "").strip()
                    scene_summary_text = scene_summary_by_id.get(
                        int(scene_of_shot.get(idx, 0) or 0), ""
                    ).strip()
                    if description_text:
                        action_desc = description_text
                    elif scene_summary_text and not scene_summary_text.startswith("承接"):
                        action_desc = scene_summary_text
                    else:
                        action_desc = None
                    action_gap_idx.add(idx)

                # 剧本动作行里的「道具：…」与「机位：…」标注确定性解析，
                # 让剧本原文直接驱动道具绑定与运镜，而不是交给导演二次猜测。
                parsed_props, parsed_camera = _parse_action_annotations(action_desc)
                if parsed_camera:
                    camera_move = parsed_camera

                # P0 修复：自动绑定角色/场景锚点，把说话人排在首位。
                # 此前 character_asset_ids/asset_anchor_ids 恒为 NULL，
                # 分镜无法锁定，渲染也无从获得角色参考图。
                speaker_names = [
                    str(line.speaker_name).strip()
                    for line in spoken_lines
                    if line.speaker_name
                ]
                ordered_names: list[str] = []
                seen_names: set[str] = set()
                for raw_name in speaker_names + list(item.character_names or []):
                    normalized = _normalize_asset_name(raw_name)
                    if normalized and normalized not in seen_names:
                        seen_names.add(normalized)
                        ordered_names.append(str(raw_name))
                matched_characters: list[Asset] = []
                for raw_name in ordered_names:
                    character_asset = _match_asset(project_assets, raw_name, "character")
                    if character_asset is not None and character_asset not in matched_characters:
                        matched_characters.append(character_asset)
                # 2026-11 QA 修复（对话对象缺席）：剧本动作行常写明"两人对峙/
                # 递给他"等在场角色，但导演 character_names 可能只填说话人。
                # 有对白的镜头里，动作行点名的角色补入出场角色（最多补 2 人），
                # 保证双人同框构图有第二个主体。
                if dialogue and action_desc:
                    for asset in project_assets:
                        if len(matched_characters) >= 3:
                            break
                        name = str(getattr(asset, "name", "") or "").strip()
                        if (
                            str(getattr(asset, "asset_type", "")) == "character"
                            and len(name) >= 2
                            and name in action_desc
                            and asset not in matched_characters
                        ):
                            matched_characters.append(asset)
                # 2026-11 QA 修复（角色锚点丢失）：动作行用「她/他」指代时
                # 上面的名字匹配会全部落空，镜头没有任何角色参考图，I2V 只能
                # 自由发挥 → 成片形象漂移（验收实测 7/12 镜）。
                # 兜底：本镜含人物线索但没匹配到角色时，按场次出场角色补绑。
                if not matched_characters:
                    person_hint_text = " ".join(
                        filter(None, [item.description, action_desc, dialogue])
                    )
                    has_person_hint = any(
                        token in person_hint_text
                        for token in ("她", "他", "两人", "三人", "对方", "有人",
                                      "身影", "女人", "男人", "女孩", "男孩")
                    ) or bool(action_lines)
                    if has_person_hint:
                        fallback_scene_id = int(scene_of_shot.get(idx, 0) or 0)
                        for asset in scene_character_assets.get(fallback_scene_id, []):
                            if len(matched_characters) >= 3:
                                break
                            if asset not in matched_characters:
                                matched_characters.append(asset)
                        if matched_characters:
                            character_fallback_idx.add(idx)
                matched_scene = _match_asset(project_assets, item.scene_name, "scene")
                if matched_scene is None:
                    for line in ref_lines:
                        location = scene_name_by_id.get(int(line.scene_id), "")
                        matched_scene = _match_asset(project_assets, location, "scene")
                        if matched_scene is not None:
                            break
                if matched_scene is None:
                    # 无台词/无动作行镜头：按场次归属取剧本 location 再匹配一次
                    fallback_location = scene_name_by_id.get(
                        int(scene_of_shot.get(idx, 0) or 0), ""
                    )
                    matched_scene = _match_asset(project_assets, fallback_location, "scene")
                # 2026-11 修复（静默失败）：场景资产匹配不到时记录 warning，
                # 由组装后的 QC 报告透出，避免镜头凭空落在白墙/其它房间。
                if matched_scene is None:
                    scene_unbound_idx.add(idx)
                character_ids = [int(asset.id) for asset in matched_characters]
                # P1 修复（道具未参与画面）：此前分镜只自动绑定角色/场景，
                # prop_asset_ids 恒为 NULL，取图时道具图永远无法命中，
                # 手写纸条、关东煮等特写镜头只能退回场景/人物图。
                # 这里按「画面描述 + 对白 + 动作行」中的道具名做确定性绑定。
                prop_text_blob = " ".join(
                    filter(None, [item.description, dialogue, action_desc])
                )
                # 道具绑定：LLM 显式 prop_names + 剧本「道具：…」解析结果优先，
                # 文本命中兜底（剧本原文是道具资产的最可信来源）。
                prop_name_candidates = [
                    *[str(name) for name in (item.prop_names or [])],
                    *parsed_props,
                ]
                matched_props = _match_prop_assets(
                    project_assets, prop_name_candidates, prop_text_blob
                )
                prop_ids = [int(asset.id) for asset in matched_props]
                # 2026-10-29 缺陷一修复：锚点版本经准入过滤（排除表情集等
                # 宫格类版本），不再直接取 current_version_id；matched_scene
                # 为 None 时由 _safe_anchor_version_ids 内部按缺失跳过。
                anchor_ids = await _safe_anchor_version_ids(
                    session, [*matched_characters, matched_scene, *matched_props]
                )

                # 情绪：导演若只给 calm/空值，采用剧本行的情绪标注
                if ref_lines:
                    reference_emotions = [
                        str(line.emotion_tag)
                        for line in ref_lines
                        if str(line.emotion_tag) in _EMOTION_TAGS
                    ]
                    if reference_emotions and (emotion is None or emotion == "calm"):
                        emotion = reference_emotions[0]

                # 2026-11 QA 修复（角色形象漂移）：把角色外形基准写成
                # 「人物造型锁定（全片不得换装/换发型）」强约束句，而不是
                # 平铺的名称+描述，让 I2V 明确知道自己必须沿用这套造型。
                character_locks = "；".join(
                    f"人物造型锁定（全片不得换装/换发型）：{asset.name}——{asset.description}"
                    for asset in matched_characters
                    if asset.description
                )
                prop_locks = "；".join(
                    f"道具：{asset.name}——{asset.description}"
                    for asset in matched_props
                    if asset.description
                )
                appearance = "；".join(filter(None, [character_locks, prop_locks]))
                # 2026-11 修复（QA 发现）：final_prompt 此前只有角色/道具描述，
                # 场景名与场景描述完全缺席（仅存在于 composition_spec），文本
                # 提示词无法约束场景。这里补入场景摘要，与 bind_anchors 口径一致。
                scene_prompt = (
                    f"场景：{matched_scene.name}，{_appearance_summary(matched_scene, 130)}"
                    if matched_scene is not None
                    else ""
                )
                # 截断上限从 500 放宽到 900：动作行原文（具象动作 + 道具 +
                # 机位）需要完整进入提示词，500 字会把后半段动作切掉。
                image_prompt = "，".join(
                    filter(None, [style, item.description.strip(), action_desc])
                )[:900]
                # P0 修复（动态性）：final_prompt 必须包含人物动作/镜头运动/
                # 时序变化三要素，否则视频模型只会生成"会呼吸的照片"。
                base_prompt = "；".join(
                    filter(None, [image_prompt, appearance, scene_prompt])
                )
                final_prompt = motion_service.build_motion_prompt(
                    SimpleNamespace(
                        action_desc=action_desc or item.description.strip(),
                        emotion_tag=emotion,
                        camera_move=camera_move,
                        final_prompt=base_prompt,
                    ),
                    base_prompt,
                )
                session.add(Shot(
                    storyboard_id=storyboard.id,
                    project_id=project_id,
                    episode_id=episode_id,
                    shot_no=idx + 1,
                    order_index=idx,
                    shot_size=shot_size,
                    camera_move=camera_move,
                    emotion_tag=emotion,
                    duration_ms=duration_ms,
                    dialogue=dialogue,
                    action_desc=action_desc or item.description.strip(),
                    image_prompt=image_prompt,
                    final_prompt=final_prompt,
                    character_asset_ids=character_ids or None,
                    # 场次 ID（剧本 Scene）用于「换场判定」：同场次但场景资产
                    # 不同 = 异常换景；不同场次 = 正常硬切（2026-11 新增）。
                    scene_id=scene_of_shot.get(idx) or None,
                    scene_asset_id=int(matched_scene.id) if matched_scene is not None else None,
                    prop_asset_ids=prop_ids or None,
                    asset_anchor_ids=sorted(set(anchor_ids)) or None,
                    script_anchor=_build_script_anchor(
                        item, ref_lines, idx, len(plan.shots), action_lines
                    ),
                    composition_spec=derive_composition_spec(
                        shot_no=idx + 1,
                        shot_size=shot_size,
                        camera_move=camera_move,
                        action_desc=action_desc or item.description.strip(),
                        dialogue=dialogue,
                        aspect_ratio=str(getattr(project, "aspect_ratio", None) or "9:16"),
                        characters=[
                            AssetRef(asset_id=int(asset.id), name=str(asset.name or ""))
                            for asset in matched_characters
                        ],
                        scene=(
                            AssetRef(
                                asset_id=int(matched_scene.id),
                                name=str(matched_scene.name or ""),
                                role="scene",
                            )
                            if matched_scene is not None
                            else None
                        ),
                        props=[
                            AssetRef(
                                asset_id=int(asset.id),
                                name=str(asset.name or ""),
                                role="prop",
                            )
                            for asset in matched_props
                        ],
                        speaker_names=ordered_names,
                        llm_hint=item.composition,
                    ).model_dump(),
                    status="draft",
                ))
            await session.flush()

            shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
            # ---- 简单 QC：对白镜头时长截断 + 景别跳跃检测 ----
            warnings_by_shot: dict[int, list[dict[str, Any]]] = {}
            dialogue_cap_ms = (
                min(_DIALOGUE_DURATION_MAX_MS, int(max_shot_duration_ms))
                if max_shot_duration_ms
                else _DIALOGUE_DURATION_MAX_MS
            )
            for shot in shots:
                if shot.dialogue and shot.duration_ms > dialogue_cap_ms:
                    warnings_by_shot.setdefault(int(shot.id), []).append({
                        "type": "dialogue_duration_clamped",
                        "from_ms": shot.duration_ms,
                        "to_ms": dialogue_cap_ms,
                        "reason": f"对白镜头时长超过 {dialogue_cap_ms / 1000:.0f} 秒，已截断",
                    })
                    shot.duration_ms = dialogue_cap_ms
            # 场景资产未匹配 → 透出 warning（避免“镜头凭空落在白墙”静默发生）
            for position in sorted(scene_unbound_idx):
                if position >= len(shots):
                    continue
                warnings_by_shot.setdefault(int(shots[position].id), []).append({
                    "type": "scene_unbound",
                    "reason": "未匹配到已锁定的场景资产，画面可能缺少场景基底；"
                    "请在资产设计页补齐场景，或在该镜头手动绑定场景锚点",
                })
            # 无剧本动作行 → 透出 warning（提示剧本侧补动作行，避免镜头动作空洞）
            for position in sorted(action_gap_idx):
                if position >= len(shots):
                    continue
                warnings_by_shot.setdefault(int(shots[position].id), []).append({
                    "type": "action_ref_missing",
                    "reason": "该镜头未引用到剧本动作行，只能依赖导演画面摘要；"
                    "请在剧本中为该处补充 action 行（具象动作+道具+机位）",
                })
            # 角色靠场次兜底绑定 → 透出 warning（提示剧本动作行缺角色真名）
            for position in sorted(character_fallback_idx):
                if position >= len(shots):
                    continue
                warnings_by_shot.setdefault(int(shots[position].id), []).append({
                    "type": "character_name_pronoun",
                    "reason": "镜头动作行未写角色真名（用「她/他」指代），已按场次出场角色"
                    "兜底绑定；建议在剧本动作行改写为角色名，确保角色锚点精确",
                })
            # 截断后重新累计总时长
            total_ms = sum(int(s.duration_ms) for s in shots)
            for i in range(1, len(shots)):
                prev, cur = shots[i - 1], shots[i]
                jump = abs(
                    _SHOT_SIZE_ORDER.get(cur.shot_size, 2) - _SHOT_SIZE_ORDER.get(prev.shot_size, 2)
                )
                if jump < 2:
                    continue
                # Q-021：告警降噪——跨场次转场（换景/换时间）与主体切换
                # （切到另一角色）本来就该换景别，不算跳跃；仅「同场次 +
                # 同主体」且跳级 ≥2 才是真正的连贯性风险。
                same_scene = (
                    prev.scene_id is not None
                    and cur.scene_id is not None
                    and int(prev.scene_id) == int(cur.scene_id)
                )
                prev_subjects = set(
                    int(a) for a in (prev.character_asset_ids or [])
                )
                cur_subjects = set(
                    int(a) for a in (cur.character_asset_ids or [])
                )
                same_subject = bool(prev_subjects & cur_subjects)
                if not (same_scene and same_subject):
                    continue
                warnings_by_shot.setdefault(int(cur.id), []).append({
                    "type": "shot_size_jump",
                    "from": prev.shot_size,
                    "to": cur.shot_size,
                    "reason": f"同场次同主体景别连续跳跃（{prev.shot_size} → {cur.shot_size}）",
                })
            # P0 分镜优化：同场次相邻镜头 180° 轴线风险检测——正反打时
            # 人物 facing 应相对（一人朝画左、一人朝画右），两侧 facing
            # 相同即视为越轴风险，写 warning 提示人工复核。
            prev_scene: int | None = None
            prev_facing: str | None = None
            for shot in shots:
                comp = shot.composition_spec if isinstance(shot.composition_spec, dict) else {}
                facings = [
                    str((sub or {}).get("facing") or "")
                    for sub in (comp.get("subjects") or [])
                    if isinstance(sub, dict)
                ]
                facing = next((f for f in facings if f in ("left", "right")), None)
                scene_key = int(shot.scene_asset_id) if shot.scene_asset_id else None
                if (
                    facing is not None
                    and prev_facing is not None
                    and scene_key is not None
                    and prev_scene == scene_key
                    and facing == prev_facing
                ):
                    warnings_by_shot.setdefault(int(shot.id), []).append({
                        "type": "axis_cross_risk",
                        "facing": facing,
                        "reason": "同场次相邻镜头人物朝向相同，疑似越轴（180° 轴线风险）",
                    })
                prev_facing = facing
                prev_scene = scene_key
            for shot in shots:
                shot_warnings = warnings_by_shot.get(int(shot.id))
                if not shot_warnings:
                    continue
                session.add(QcReport(
                    project_id=project_id,
                    target_type="shot",
                    target_id=int(shot.id),
                    target_version_id=None,
                    result="warning",
                    consistency_score=None,
                    quality_defects=None,
                    detail={"warnings": shot_warnings, "stage": "storyboard_generation"},
                    round_no=1,
                    model_key="rule-based-qc",
                ))

            storyboard.status = "pending_review"
            storyboard.shot_count = len(shots)
            storyboard.estimated_duration_ms = total_ms
            # 修复（E2E 报告问题 1）：生成成功后清除历史失败标记，
            # 避免前端继续展示上一轮的失败横幅。
            cleared_config = dict(project.config or {})
            cleared_config.pop("_last_storyboard_failure", None)
            # Q-019：镜头数校验结果落 config，前端/排查可按此追踪修正动作
            if shot_count_adjustment is not None:
                cleared_config["_shot_count_adjustment"] = {
                    **shot_count_adjustment,
                    "episode_no": int(episode.episode_no),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            project.config = cleared_config
            await session.commit()

            warning_count = sum(len(v) for v in warnings_by_shot.values())
            await event_bus.publish(project_id, "storyboard.generated", {
                "storyboard_id": int(storyboard.id),
                "episode_no": int(episode.episode_no),
                "shot_count": len(shots),
                "estimated_duration_ms": total_ms,
                "warning_count": warning_count,
            })
            logger.info(
                "分镜生成完成 project_id=%s storyboard_id=%s shots=%s warnings=%s",
                project_id, storyboard.id, len(shots), warning_count,
            )
    except Exception as exc:  # noqa: BLE001 后台任务兜底
        logger.error("分镜生成失败 project_id=%s err=%s", project_id, exc, exc_info=exc)
        # P4-b 修复：失败时回滚项目状态。发起生成时项目被推进到 storyboarding，
        # 若后台管线异常终止而状态不回滚，项目将永久停留在「分镜生成中」
        # （前端底部状态栏按项目状态渲染，刷新后依旧显示生成中，状态机卡死）。
        # 回滚目标 asset_review 为 storyboarding 的来源状态（资产已就绪），
        # 用户可直接重试「生成分镜」。
        try:
            async with async_session_factory() as rollback_session:
                rollback_project = await rollback_session.get(Project, project_id)
                if rollback_project is not None:
                    from app.services.project_service import _add_operation_log

                    old_status = rollback_project.status
                    rollback_reason = "分镜生成失败，自动回滚至资产审核"
                    status_changed = old_status == "storyboarding"
                    if status_changed:
                        rollback_project.status = "asset_review"
                        rollback_project.lock_version += 1
                    # Q-007 修复：失败原因必须无条件落库，不再仅在
                    # status=storyboarding 分支写入；否则后台任务异常时
                    # 前端刷新后与“从未生成”无法区分。
                    failure_config = dict(rollback_project.config or {})
                    failure_config["_last_storyboard_failure"] = {
                        "episode_no": episode_no,
                        "message": (
                            getattr(exc, "message", None)
                            or str(exc)[:500]
                            or "分镜生成失败"
                        )[:500],
                        "code": str(getattr(exc, "code", "") or ""),
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                    rollback_project.config = failure_config
                    if status_changed:
                        await _add_operation_log(
                            rollback_session,
                            project_id=rollback_project.id,
                            entity_id=rollback_project.id,
                            action="update",
                            diff={"status": {"from": old_status, "to": "asset_review"}, "reason": rollback_reason},
                            user_id=user_id,
                        )
                    await rollback_session.commit()
                    if status_changed:
                        await event_bus.publish(
                            project_id,
                            "status_changed",
                            {
                                "from": old_status,
                                "to": "asset_review",
                                "reason": rollback_reason,
                                "lock_version": rollback_project.lock_version,
                            },
                        )
                        logger.info(
                            "分镜生成失败已回滚项目状态 project_id=%s %s -> asset_review",
                            project_id, old_status,
                        )
        except Exception:  # noqa: BLE001 回滚失败只记日志，不掩盖原始生成异常
            logger.warning(
                "分镜生成失败后回滚项目状态异常 project_id=%s", project_id, exc_info=True
            )
        try:
            await event_bus.publish(
                project_id,
                "storyboard_generate.failed",
                {"episode_no": episode_no, "message": str(exc)[:300]},
            )
        except Exception:  # noqa: BLE001
            pass


# =====================================================================
# 分镜列表 / 详情
# =====================================================================


async def list_storyboards(session: AsyncSession, project_id: int) -> list[dict[str, Any]]:
    """查询项目分镜板列表（按集号升序，含集号/镜头数/状态/时长）。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        分镜板响应字典列表。
    """
    rows = await StoryboardRepository.list_by_project(session, project_id)
    return [storyboard_out(sb, episode_no=ep_no) for sb, ep_no in rows]


async def get_storyboard_or_error(session: AsyncSession, storyboard_id: int) -> Storyboard:
    """按 ID 获取分镜板，不存在抛 10002。

    Args:
        session: 异步数据库会话。
        storyboard_id: 分镜板 ID。

    Returns:
        分镜板 ORM 实例。

    Raises:
        BizError: 10002 分镜板不存在。
    """
    storyboard = await StoryboardRepository.get_by_id(session, storyboard_id)
    if storyboard is None:
        raise BizError(ErrorCode.NOT_FOUND, "分镜板不存在")
    return storyboard


async def _shot_media_url(session: AsyncSession, shot: Shot) -> str | None:
    """解析镜头当前首帧媒体 URL（first_frame_media_id 优先，当前版本兜底）。

    旧 stub 视频产物是 ``application/json`` 占位，内部 ``poster_key`` 指向
    首帧 SVG。这里把 JSON 占位解析为 poster SVG，保证分镜页能显示关键帧，
    而不是把 JSON 当作图片地址加载失败。

    Args:
        session: 异步数据库会话。
        shot: 镜头 ORM 实例。

    Returns:
        媒体 URL（视频/图片/SVG 首帧）；无首帧时为 None。
    """
    # Q-013：已渲染镜头的成片视频优先于首帧静图，分镜页才能看到镜头视频。
    from app.models.render import RenderOutput

    output_row = (
        await session.execute(
            select(RenderOutput.media_file_id)
            .where(
                RenderOutput.shot_id == int(shot.id),
                RenderOutput.output_type == "video",
                RenderOutput.media_file_id.is_not(None),
            )
            .order_by(RenderOutput.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if output_row is not None:
        video_media = await AssetRepository.get_media_file(session, int(output_row))
        if video_media is not None and str(video_media.mime_type or "").startswith("video/"):
            return video_media.url

    media_id = shot.first_frame_media_id
    if media_id is None:
        version = await StoryboardRepository.current_shot_version(session, int(shot.id))
        media_id = version.media_file_id if version is not None else None
    if media_id is None:
        return None
    media = await AssetRepository.get_media_file(session, media_id)
    if media is None:
        return None
    if str(media.mime_type or "") == "application/json":
        meta = media.meta if isinstance(media.meta, dict) else {}
        poster = meta.get("poster_key")
        if isinstance(poster, str) and poster:
            return f"/files/{poster}"
    return media.url


async def _latest_shot_qc(
    session: AsyncSession, shot_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """批量读取镜头最近一轮质检结论（含剧本锚点失配标记）。

    分镜页需要把「画面与剧本锚点对不上」的镜头标红（2026-11 新增），
    这里一次查询整板 QC 报告，避免逐镜查询造成 N+1。

    Args:
        session: 异步数据库会话。
        shot_ids: 分镜板内镜头 ID 列表。

    Returns:
        ``{shot_id: {result, round_no, mismatch, reasons, vision}}``；
        无 QC 报告的镜头不在返回值中。

    Raises:
        无：字段结构异常按空报告处理。
    """
    if not shot_ids:
        return {}
    rows = (
        await session.execute(
            select(QcReport)
            .where(
                QcReport.target_type == "shot",
                QcReport.target_id.in_(shot_ids),
            )
            .order_by(QcReport.target_id.asc(), QcReport.round_no.asc())
        )
    ).scalars().all()
    latest: dict[int, dict[str, Any]] = {}
    warnings_by_shot: dict[int, list[str]] = {}
    for report in rows:
        detail = report.detail if isinstance(report.detail, dict) else {}
        shot_id = int(report.target_id)
        # 汇总生成期规则告警（如 scene_unbound），与渲染期报告分属不同
        # round，不能互相覆盖；这里先累加，最后统一附加到 latest。
        for item in detail.get("warnings") or []:
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or item.get("type") or "").strip()
            if reason:
                warnings_by_shot.setdefault(shot_id, []).append(reason)
        anchor_check = (
            detail.get("anchor_check") if isinstance(detail.get("anchor_check"), dict) else {}
        )
        latest[shot_id] = {
            "result": str(report.result or ""),
            "round_no": int(report.round_no or 0),
            "mismatch": bool(detail.get("anchor_mismatch")),
            "reasons": [str(item) for item in (anchor_check.get("reasons") or [])],
            "vision": detail.get("anchor_vision") or None,
        }
    for shot_id, entry in latest.items():
        if warnings_by_shot.get(shot_id):
            entry["warnings"] = warnings_by_shot[shot_id]
    return latest


async def get_storyboard_detail(session: AsyncSession, storyboard_id: int) -> dict[str, Any]:
    """查询分镜板详情（含镜头列表与当前首帧 media_url）。

    Args:
        session: 异步数据库会话。
        storyboard_id: 分镜板 ID。

    Returns:
        分镜板详情字典（含 shots，每个镜头附最近一轮 ``anchor_qc``）。

    Raises:
        BizError: 10002 分镜板不存在。
    """
    storyboard = await get_storyboard_or_error(session, storyboard_id)
    shots = await StoryboardRepository.list_shots(session, storyboard_id)
    qc_by_shot = await _latest_shot_qc(session, [int(shot.id) for shot in shots])
    items: list[dict[str, Any]] = []
    for shot in shots:
        item = shot_out(shot, media_url=await _shot_media_url(session, shot))
        qc = qc_by_shot.get(int(shot.id))
        if qc:
            item["anchor_qc"] = qc
        items.append(item)
    data = storyboard_out(storyboard)
    data["shots"] = items
    return data


# =====================================================================
# 镜头编辑（patch / reorder / anchors）
# =====================================================================


async def get_shot_or_error(session: AsyncSession, shot_id: int) -> Shot:
    """按 ID 获取镜头，不存在抛 10002。

    Args:
        session: 异步数据库会话。
        shot_id: 镜头 ID。

    Returns:
        镜头 ORM 实例。

    Raises:
        BizError: 10002 镜头不存在。
    """
    shot = await StoryboardRepository.get_shot(session, shot_id)
    if shot is None:
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")
    return shot


async def patch_shot(
    session: AsyncSession, shot: Shot, changes: dict[str, Any]
) -> dict[str, Any]:
    """局部更新镜头字段（PATCH 语义；duration_ms 区间由 schema 校验）。

    Args:
        session: 异步数据库会话。
        shot: 镜头 ORM 实例。
        changes: 已过滤的更新字段（仅显式传入项）。

    Returns:
        镜头响应字典。

    Raises:
        BizError: 10002 镜头不存在；40001 镜头已锁定。
    """
    shot = await get_shot_or_error(session, int(shot.id))
    if shot.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "镜头已锁定，请先解除分镜锁定")
    if not changes:
        return shot_out(shot)
    for key, value in changes.items():
        setattr(shot, key, value)
    # 修复：改景别/动作/运镜后旧 composition_spec 会继续复用（人物仍是旧
    # 比例/姿态/锚点），表现为"改了镜头但画面没变"。这些字段变化时清空
    # spec，下次合成按新参数重新推导；同时标记上游变更，提示重出整幅画面。
    if (
        {"shot_size", "action_desc", "camera_move"} & set(changes)
        and shot.composition_spec is not None
    ):
        shot.composition_spec = None
        shot.upstream_changed = True
    await session.commit()
    await session.refresh(shot)
    return shot_out(shot)


async def reorder_shots(
    session: AsyncSession, storyboard: Storyboard, body: dict[str, Any]
) -> dict[str, Any]:
    """重排镜头：移动目标镜头到锚点前/后并重算全部 order_index。

    Args:
        session: 异步数据库会话。
        storyboard: 分镜板 ORM 实例。
        body: 已解析的请求体 {shot_id, insert_before_shot_id, insert_after_shot_id}。

    Returns:
        ``{"episode_duration_ms", "hook_position_changed", "shots"}``。

    Raises:
        BizError: 10001 未提供插入锚点或同时提供两个；10002 镜头不存在；
            40001 分镜已锁定。
    """
    if storyboard.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "分镜已锁定，无法重排镜头")
    before_id = body.get("insert_before_shot_id")
    after_id = body.get("insert_after_shot_id")
    if before_id is None and after_id is None:
        raise BizError(ErrorCode.PARAM_INVALID, "必须提供 insert_before_shot_id 或 insert_after_shot_id 之一")
    if before_id is not None and after_id is not None:
        raise BizError(ErrorCode.PARAM_INVALID, "insert_before_shot_id 与 insert_after_shot_id 只能二选一")

    shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
    shot_ids = [int(s.id) for s in shots]
    moving_id = int(body["shot_id"])
    anchor_id = int(before_id or after_id)
    if moving_id not in shot_ids or anchor_id not in shot_ids:
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在或不属于该分镜板")
    old_last_id = shot_ids[-1] if shot_ids else None

    ordered = [i for i in shot_ids if i != moving_id]
    anchor_pos = ordered.index(anchor_id)
    insert_pos = anchor_pos if before_id is not None else anchor_pos + 1
    ordered.insert(insert_pos, moving_id)

    id_to_shot = {int(s.id): s for s in shots}
    for new_index, shot_id in enumerate(ordered):
        id_to_shot[shot_id].order_index = new_index
    storyboard.estimated_duration_ms = sum(int(s.duration_ms) for s in shots)
    await session.commit()

    new_last_id = ordered[-1] if ordered else None
    return {
        "episode_duration_ms": int(storyboard.estimated_duration_ms),
        "hook_position_changed": old_last_id != new_last_id,
        "shots": [
            {"shot_id": sid, "order_index": idx} for idx, sid in enumerate(ordered)
        ],
    }


def _appearance_summary(asset: Any, limit: int = 160) -> str:
    """提取资产的外观摘要（meta.appearance 优先，description 兜底）。

    人物外观需要完整服装/发型描述，80 字截断会把「下穿深色直筒裤」
    「口袋鼓着一小包东西」切成半句，I2V 模型因此丢失衣着锚点、
    跨镜头角色漂移；默认放宽到 160 字覆盖完整人物设定。

    Args:
        asset: 资产 ORM 实例。
        limit: 截断长度。

    Returns:
        外观摘要字符串。
    """
    meta = asset.meta or {}
    text = str(meta.get("appearance") or asset.description or "")
    return text[:limit]


async def bind_anchors(
    session: AsyncSession,
    shot: Shot,
    character_asset_ids: list[int],
    scene_asset_id: int | None,
) -> dict[str, Any]:
    """绑定镜头锚点（资产须 locked）并组装 final_prompt。

    规则：
    - 角色资产须存在、类型为 character 且状态 locked（否则 50001）；
    - 场景资产须存在、类型为 scene 且状态 locked（否则 50001）；
    - 绑定后 ``asset_anchor_ids`` 写入各锚点资产的当前版本 ID，
      ``image_prompt`` 追加角色外观摘要，``final_prompt`` 注入完整锚点信息。

    Args:
        session: 异步数据库会话。
        shot: 镜头 ORM 实例。
        character_asset_ids: 角色资产 ID 列表。
        scene_asset_id: 场景资产 ID（可空）。

    Returns:
        ``{"shot_id", "character_asset_ids", "scene_asset_id",
        "asset_anchor_ids", "final_prompt"}``。

    Raises:
        BizError: 10001 类型不匹配；10002 资产不存在；40001 镜头已锁定；
            50001 资产未锁定。
    """
    shot = await get_shot_or_error(session, int(shot.id))
    if shot.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "镜头已锁定，请先解除分镜锁定")

    characters = await AssetRepository.list_by_ids(session, list(dict.fromkeys(character_asset_ids)))
    if len(characters) != len(set(character_asset_ids)):
        raise BizError(ErrorCode.NOT_FOUND, "部分角色资产不存在")
    for asset in characters:
        if asset.asset_type != "character":
            raise BizError(ErrorCode.PARAM_INVALID, f"资产 {asset.name} 不是角色类型")
        if asset.status != "locked":
            raise BizError(
                ErrorCode.ASSET_NOT_LOCKED,
                f"角色资产「{asset.name}」未锁定，请先完成审核并锁定",
                data={"asset_id": int(asset.id), "status": asset.status},
            )
    scene_asset = None
    if scene_asset_id is not None:
        scene_asset = await AssetRepository.get_by_id(session, scene_asset_id)
        if scene_asset is None:
            raise BizError(ErrorCode.NOT_FOUND, "场景资产不存在")
        if scene_asset.asset_type != "scene":
            raise BizError(ErrorCode.PARAM_INVALID, f"资产 {scene_asset.name} 不是场景类型")
        if scene_asset.status != "locked":
            raise BizError(
                ErrorCode.ASSET_NOT_LOCKED,
                f"场景资产「{scene_asset.name}」未锁定，请先完成审核并锁定",
                data={"asset_id": int(scene_asset.id), "status": scene_asset.status},
            )

    shot.character_asset_ids = [int(a.id) for a in characters]
    shot.scene_asset_id = scene_asset_id
    # 2026-10-29 缺陷一修复：锚点版本经参考帧准入过滤（排除表情集等宫格
    # 类版本），历史实现直接取 current_version_id 会把九宫格图绑进渲染链路。
    anchor_version_ids = await _safe_anchor_version_ids(
        session, [a for a in [*characters, scene_asset] if a is not None]
    )
    shot.asset_anchor_ids = anchor_version_ids

    # ---- final_prompt 组装：画风前缀 + 画面描述 + 锚点外观摘要 ----
    project = await session.get(Project, int(shot.project_id))
    style = await _project_style_prefix(session, int(shot.project_id)) if project else ""
    char_summary = "；".join(
        f"{a.name}（{_appearance_summary(a)}）" for a in characters
    )
    parts = [str(shot.image_prompt or shot.action_desc or "")]
    if char_summary:
        parts.append(f"出场角色：{char_summary}")
    if scene_asset is not None:
        # 场景保留 130 字：截到 60 字会在「收银台与关东」处断句，丢失
        # 暖柜/冷暖对比等关键布景信息。
        parts.append(f"场景：{scene_asset.name}，{_appearance_summary(scene_asset, 130)}")
    final_prompt = "，".join(p for p in parts if p)
    if style and style not in final_prompt:
        final_prompt = f"{style}，{final_prompt}"
    shot.final_prompt = final_prompt
    await session.commit()
    await session.refresh(shot)
    return {
        "shot_id": int(shot.id),
        "character_asset_ids": shot.character_asset_ids,
        "scene_asset_id": shot.scene_asset_id,
        "asset_anchor_ids": shot.asset_anchor_ids,
        "final_prompt": shot.final_prompt,
    }


# =====================================================================
# 分镜锁定与审批（lock / decide）
# =====================================================================


async def lock_storyboard(session: AsyncSession, storyboard: Storyboard, user_id: int) -> dict[str, Any]:
    """锁定分镜：校验全部镜头至少 1 个锚点 → 锁定 → 创建审批单。

    Args:
        session: 异步数据库会话。
        storyboard: 分镜板 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        ``{"storyboard", "approval_id", "message"}``。

    Raises:
        BizError: 40001 已锁定或无镜头；60005 存在缺锚点镜头（data 携带
            缺锚点镜头清单）。
    """
    storyboard = await get_storyboard_or_error(session, int(storyboard.id))
    if storyboard.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "分镜已锁定，请勿重复锁定")
    shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
    if not shots:
        raise BizError(ErrorCode.ASSET_ANCHOR_MISSING, "分镜板无镜头，无法锁定")
    missing = [
        {
            "shot_id": int(s.id),
            "shot_no": int(s.shot_no),
            "order_index": int(s.order_index),
        }
        for s in shots
        if not (s.character_asset_ids or s.scene_asset_id)
    ]
    # 修复（E2E 报告 P-02）：此前仅校验「镜头是否绑定锚点」，未校验锚点资产
    # 本身存在且已锁定——引用未锁定 / 已删除资产时锁定仍可通过，且随数据
    # 漂移出现「同状态有时能锁有时被拦」的不一致表现。这里对全部镜头的
    # 角色与场景引用做一次存在性 + locked 状态校验，并把所有问题一次性
    # 汇总返回（缺锚点镜头 + 无效锚点资产），保证校验结果确定可复现。
    ref_ids: set[int] = set()
    for s in shots:
        for aid in (s.character_asset_ids or []):
            try:
                ref_ids.add(int(aid))
            except (TypeError, ValueError):
                continue
        if s.scene_asset_id:
            try:
                ref_ids.add(int(s.scene_asset_id))
            except (TypeError, ValueError):
                continue
    invalid_anchors: list[dict[str, Any]] = []
    if ref_ids:
        rows = (
            await session.execute(select(Asset.id, Asset.name, Asset.status).where(Asset.id.in_(ref_ids)))
        ).all()
        asset_by_id = {int(r.id): (r.name, r.status) for r in rows}
        for aid in sorted(ref_ids):
            info = asset_by_id.get(aid)
            if info is None:
                invalid_anchors.append({"asset_id": aid, "reason": "资产不存在（可能已被删除）"})
            elif info[1] != "locked":
                invalid_anchors.append(
                    {"asset_id": aid, "asset_name": info[0], "reason": f"资产未锁定（当前 {info[1]}）"}
                )
    if missing or invalid_anchors:
        problems: list[str] = []
        if missing:
            problems.append(f"{len(missing)} 个镜头未绑定锚点")
        if invalid_anchors:
            problems.append(f"{len(invalid_anchors)} 个锚点资产无效或未锁定")
        raise BizError(
            ErrorCode.ASSET_ANCHOR_MISSING,
            f"{'、'.join(problems)}，无法锁定",
            data={"missing_shots": missing, "invalid_anchors": invalid_anchors},
        )

    now = _utcnow()
    storyboard.status = "locked"
    storyboard.locked_at = now
    for shot in shots:
        shot.status = "locked"
        shot.locked_at = now
    approval = ApprovalRequest(
        project_id=int(storyboard.project_id),
        approval_type="storyboard",
        target_type=None,  # DB CHECK 限定 script/asset/shot/timeline，storyboard 以 approval_type 表达
        target_id=int(storyboard.id),
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(days=_APPROVAL_EXPIRE_DAYS),
    )
    session.add(approval)
    await session.flush()
    await session.commit()

    # ---- 状态推进钩子：分镜锁定后推进到 storyboard_review ----
    # Q-008：项目可能停留在 asset_review（资产审核未单独通过），旧逻辑
    # 只处理 storyboarding，导致锁定后状态不前进。这里用完整链推进，
    # 兼容 asset_review → storyboarding → storyboard_review。
    project = await session.get(Project, int(storyboard.project_id))
    if project is not None:
        from app.services import project_service

        await project_service.advance_chain(
            project,
            ["asset_review", "storyboarding", "storyboard_review"],
            "分镜已锁定，等待审批",
            session=session,
            user_id=user_id,
        )

    await event_bus.publish(int(storyboard.project_id), "approval.created", {
        "approval_id": int(approval.id),
        "approval_type": "storyboard",
        "target_id": int(storyboard.id),
        "expires_at": approval.expires_at.isoformat(),
    })
    return {
        "storyboard": storyboard_out(storyboard),
        "approval_id": int(approval.id),
        "message": "分镜已锁定，审批单已创建（7 天内有效）",
    }


async def decide_storyboard(
    session: AsyncSession,
    storyboard: Storyboard,
    action: str,
    feedback: str | None,
    user_id: int,
) -> dict[str, Any]:
    """分镜审批决策（storyboard 域专用 decide，不与 orchestration 冲突）。

    规则：
    - 仅存在 pending 审批单时可决策（惰性过期检查）；
    - approve：审批单置 approved → 分镜保持 locked → 项目流转 rendering
      （经 project_service.transition 白名单，状态不允许时抛 40001）；
    - reject（feedback ≥10 字）：审批单置 rejected → 分镜解锁回
      pending_review，镜头同步解锁，反馈留痕审批单。

    Args:
        session: 异步数据库会话。
        storyboard: 分镜板 ORM 实例。
        action: 决策动作 approve/reject。
        feedback: 反馈原文（reject 必填 ≥10 字）。
        user_id: 决策人用户 ID。

    Returns:
        ``{"approval_id", "action", "storyboard_id", "project_status"}``。

    Raises:
        BizError: 10001 reject 反馈不足 10 字；10002 无 pending 审批单；
            40001 审批单已过期或项目状态不允许流转。
    """
    from app.services import project_service

    storyboard = await get_storyboard_or_error(session, int(storyboard.id))
    approval = await StoryboardRepository.get_pending_approval(session, int(storyboard.id))
    if approval is None:
        raise BizError(ErrorCode.NOT_FOUND, "该分镜板不存在待决策的审批单")
    if approval.expires_at < _utcnow():
        approval.status = "expired"
        await session.commit()
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "审批单已过期")

    approval.decision = action
    approval.decided_by_user_id = user_id
    approval.decided_at = _utcnow()

    project_status = None
    if action == "approve":
        approval.status = "approved"
        project = await session.get(Project, int(storyboard.project_id))
        if project is not None:
            # 修复（测试报告 #29/#35）：项目若因渲染/生成失败处于 failed，
            # 审批通过本身是人工恢复信号——先自动恢复再链式推进到 rendering，
            # 避免 40001 死锁（allowed=[]）与审批后状态不推进。
            project = await project_service.recover_failed_project(
                session, project, "分镜审批通过，自动恢复失败项目", user_id=user_id,
            )
            if project.status != "rendering":
                # Q-008：兼容 asset_review/storyboarding/storyboard_review
                # 全链路，避免仅从 storyboard_review 起推导致 40001 死锁。
                await project_service.advance_chain(
                    project,
                    ["asset_review", "storyboarding", "storyboard_review", "rendering"],
                    "分镜审批通过，进入渲染阶段",
                    session=session,
                    user_id=user_id,
                )
            project_status = project.status
    else:
        if not feedback or len(feedback.strip()) < 10:
            raise BizError(ErrorCode.PARAM_INVALID, "驳回反馈不得少于 10 个字符")
        approval.status = "rejected"
        approval.feedback = feedback
        storyboard.status = "pending_review"
        storyboard.locked_at = None
        shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
        for shot in shots:
            shot.status = "pending_review"
            shot.locked_at = None
        await session.commit()
        project = await session.get(Project, int(storyboard.project_id))
        project_status = project.status if project is not None else None

    await event_bus.publish(int(storyboard.project_id), "approval.decided", {
        "approval_id": int(approval.id),
        "approval_type": "storyboard",
        "action": action,
        "storyboard_id": int(storyboard.id),
        "user_id": user_id,
    })
    # 埋点：HITL-3 分镜关口决策
    await track_gate(
        GATE_STORYBOARD,
        action == "approve",
        user_id=user_id,
        project_id=int(storyboard.project_id),
    )
    return {
        "approval_id": int(approval.id),
        "action": action,
        "storyboard_id": int(storyboard.id),
        "project_status": project_status,
    }


# =====================================================================
# 草稿预览 / 画布布局
# =====================================================================


async def draft_preview(session: AsyncSession, storyboard: Storyboard) -> dict[str, Any]:
    """生成分镜草稿预览（关键帧 URL + 字幕 + 时长合计）。

    subtitle 取对白（缺失时用动作描述摘要）；audio_url 预留 None
    （真实音频由渲染/后期任务填充）。

    Args:
        session: 异步数据库会话。
        storyboard: 分镜板 ORM 实例。

    Returns:
        ``{"storyboard_id", "shots", "total_duration_ms"}``。

    Raises:
        BizError: 10002 分镜板不存在。
    """
    storyboard = await get_storyboard_or_error(session, int(storyboard.id))
    shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
    items: list[dict[str, Any]] = []
    total = 0
    for shot in shots:
        keyframe_url = await _shot_media_url(session, shot)
        subtitle = shot.dialogue or (shot.action_desc or "")[:40] or (shot.image_prompt or "")[:40]
        items.append({
            "shot_id": int(shot.id),
            "order_index": int(shot.order_index),
            "keyframe_url": keyframe_url,
            "subtitle": subtitle,
            "audio_url": None,
            "duration_ms": int(shot.duration_ms),
            "emotion_tag": shot.emotion_tag,
        })
        total += int(shot.duration_ms)
    return {
        "storyboard_id": int(storyboard.id),
        "shots": items,
        "total_duration_ms": total,
    }


async def save_layout(
    session: AsyncSession,
    storyboard: Storyboard,
    user_id: int,
    nodes: list[dict[str, Any]],
    viewport: dict[str, Any] | None,
) -> dict[str, Any]:
    """保存分镜画布布局（按用户维度 upsert）。

    Args:
        session: 异步数据库会话。
        storyboard: 分镜板 ORM 实例。
        user_id: 用户 ID。
        nodes: 布局节点列表（{shot_id, x, y, w, h}）。
        viewport: 视口状态（可空）。

    Returns:
        ``{"storyboard_id", "user_id", "node_count", "saved"}``。

    Raises:
        BizError: 10002 分镜板不存在。
    """
    storyboard = await get_storyboard_or_error(session, int(storyboard.id))
    layout = {"nodes": nodes}
    row = await StoryboardRepository.get_layout(session, int(storyboard.id), user_id)
    if row is not None:
        row.layout = layout
        row.viewport = viewport
    else:
        session.add(StoryboardLayout(
            storyboard_id=int(storyboard.id),
            user_id=user_id,
            layout=layout,
            viewport=viewport,
        ))
    await session.commit()
    return {
        "storyboard_id": int(storyboard.id),
        "user_id": user_id,
        "node_count": len(nodes),
        "saved": True,
    }


# =====================================================================
# V4.0 分镜域增量：节奏模式 / 批量改 / 复制 / 操作历史 / 静帧重出 / 模板
# =====================================================================

#: 节奏模式 → 拆解参数（AC-F4-07-1【假设待验证：具体参数值】）
PACING_PROFILES: dict[str, dict[str, Any]] = {
    "fast": {
        "shot_duration_min": 3000,
        "shot_duration_max": 4000,
        "closeup_density": 0.40,
        "label": "快节奏爽剧",
        "hint": "镜头均值 3000-4000ms，特写占比 ≥40%",
    },
    "balanced": {
        "shot_duration_min": 3000,
        "shot_duration_max": 4500,
        "closeup_density": 0.30,
        "label": "均衡",
        "hint": "镜头均值 3000-4500ms，特写占比约 30%",
    },
    "slow": {
        "shot_duration_min": 3500,
        "shot_duration_max": 5000,
        "closeup_density": 0.22,
        "label": "慢节奏情感剧",
        "hint": "镜头均值 3500-5000ms，空镜占比上调",
    },
}

#: 题材 → 推荐节奏模式（AC-F4-07-2【假设待验证：推荐映射表】）
GENRE_PACING_HINTS: dict[str, str] = {
    "counterattack": "fast",
    "rebirth": "fast",
    "urban_romance": "balanced",
    "suspense": "balanced",
    "workplace": "balanced",
    "campus": "balanced",
    "family_ethics": "slow",
    "fantasy": "slow",
}

#: 单镜头静帧重出上限（AC-F4-03-3）
MAX_REDRAFT_COUNT: int = 3


async def record_op(
    session: AsyncSession,
    *,
    storyboard_id: int,
    user_id: int,
    op_type: str,
    target_shot_ids: list[int],
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> int:
    """记录一次画布操作到环形历史栈（AC-F4-09-1，≤100 步）。

    每步结构 ``{seq, op_type, target_shot_ids, before, after, at}``；
    超过 100 步时淘汰最旧步。同时刷新 ``snapshot``（回退恢复依据）。

    Args:
        session: 请求级数据库会话（调用方提交）。
        storyboard_id: 分镜板 ID。
        user_id: 操作人用户 ID。
        op_type: 操作类型 add/delete/move/batch_edit/copy/redraw。
        target_shot_ids: 受影响镜头 ID 列表。
        before: 变更前快照（可空）。
        after: 变更后快照（可空）。

    Returns:
        本次操作的步骤序号 seq。
    """
    record = (
        await session.execute(
            select(StoryboardOp).where(
                StoryboardOp.storyboard_id == storyboard_id,
                StoryboardOp.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        record = StoryboardOp(
            storyboard_id=storyboard_id, user_id=user_id, op_history=[], last_seq=0
        )
        session.add(record)
        await session.flush()

    history = list(record.op_history or [])
    seq = int(record.last_seq) + 1
    history.append(
        {
            "seq": seq,
            "op_type": op_type,
            "target_shot_ids": list(target_shot_ids),
            "before": before,
            "after": after,
            "at": _utcnow().isoformat(),
        }
    )
    if len(history) > MAX_OP_HISTORY:
        history = history[-MAX_OP_HISTORY:]
    record.op_history = history
    record.last_seq = seq
    record.snapshot = await _shots_snapshot(session, storyboard_id)
    await session.flush()
    return seq


async def _shots_snapshot(
    session: AsyncSession, storyboard_id: int
) -> dict[str, Any]:
    """构造分镜板全部镜头的完整快照（回退恢复用）。

    Args:
        session: 请求级数据库会话。
        storyboard_id: 分镜板 ID。

    Returns:
        ``{"shots": [镜头字段快照...]}``。
    """
    shots = await StoryboardRepository.list_shots(session, storyboard_id)
    return {
        "shots": [
            {
                "shot_no": int(s.shot_no),
                "order_index": int(s.order_index),
                "shot_size": s.shot_size,
                "camera_move": s.camera_move,
                "emotion_tag": s.emotion_tag,
                "duration_ms": int(s.duration_ms),
                "dialogue": s.dialogue,
                "action_desc": s.action_desc,
                "image_prompt": s.image_prompt,
                "final_prompt": s.final_prompt,
                "negative_prompt": s.negative_prompt,
                "asset_anchor_ids": s.asset_anchor_ids,
                "character_asset_ids": s.character_asset_ids,
                "scene_asset_id": s.scene_asset_id,
                "prop_asset_ids": s.prop_asset_ids,
                "status": s.status,
            }
            for s in shots
        ]
    }


async def get_ops_history(
    session: AsyncSession, storyboard_id: int, user_id: int
) -> dict[str, Any]:
    """查询画布操作历史（仅当次会话有效，AC-F4-09-1）。

    Args:
        session: 请求级数据库会话。
        storyboard_id: 分镜板 ID。
        user_id: 操作人用户 ID。

    Returns:
        ``{storyboard_id, last_seq, items: [操作步骤...]}``。
    """
    record = (
        await session.execute(
            select(StoryboardOp).where(
                StoryboardOp.storyboard_id == storyboard_id,
                StoryboardOp.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return {"storyboard_id": storyboard_id, "last_seq": 0, "items": []}
    return {
        "storyboard_id": storyboard_id,
        "last_seq": int(record.last_seq),
        "items": list(record.op_history or []),
    }


async def rollback_to_seq(
    session: AsyncSession,
    storyboard: Storyboard,
    *,
    seq: int,
    confirm: bool,
    user_id: int,
) -> dict[str, Any]:
    """把画布恢复到指定步骤结束时的状态（AC-F4-09-2）。

    实现方式：以该步之后的 ``snapshot`` 为准（若历史中没有则按当前镜头
    集合重放「删除后重建」）——本实现采用「按目标步记录之后的快照恢复」：
    历史栈保存的是每步之后的完整快照，故恢复即把镜头表对齐到该快照，
    被删除的镜头以原参数重建，后续历史步截断。

    Args:
        session: 请求级数据库会话。
        storyboard: 分镜板 ORM 实例。
        seq: 目标步骤序号。
        confirm: 是否二次确认（未确认只返回影响摘要）。
        user_id: 操作人用户 ID。

    Returns:
        ``{confirmed, storyboard_id, target_seq, restored_shots,
        truncated_steps}``。

    Raises:
        BizError: 10001 未二次确认或步骤不存在；40001 分镜已锁定。
    """
    if storyboard.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "分镜已锁定，无法回退操作")
    record = (
        await session.execute(
            select(StoryboardOp).where(
                StoryboardOp.storyboard_id == int(storyboard.id),
                StoryboardOp.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    history = list(record.op_history or []) if record else []
    target = next((step for step in history if int(step.get("seq") or 0) == seq), None)
    if target is None:
        raise BizError(
            ErrorCode.PARAM_INVALID, f"操作历史中不存在第 {seq} 步", error_detail="op_seq_not_found"
        )
    if not confirm:
        return {
            "confirmed": False,
            "storyboard_id": int(storyboard.id),
            "target_seq": seq,
            "op_type": target.get("op_type"),
            "target_shot_ids": target.get("target_shot_ids"),
            "truncated_steps": max(0, int(record.last_seq) - seq) if record else 0,
            "message": "请二次确认后执行回退",
        }

    after = (target.get("after") or {}) if isinstance(target, dict) else {}
    snap = after.get("snapshot") if isinstance(after, dict) else None
    restored = 0
    if isinstance(snap, dict) and snap.get("shots"):
        shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
        by_no = {int(s.shot_no): s for s in shots}
        for item in snap["shots"]:
            shot_no = int(item.get("shot_no") or 0)
            shot = by_no.get(shot_no)
            if shot is None:
                shot = Shot(
                    storyboard_id=int(storyboard.id),
                    project_id=int(storyboard.project_id),
                    episode_id=int(storyboard.episode_id),
                    shot_no=shot_no,
                    order_index=int(item.get("order_index") or 0),
                    shot_size=item.get("shot_size") or "medium",
                    camera_move=item.get("camera_move") or "static",
                    duration_ms=int(item.get("duration_ms") or 3000),
                    status=item.get("status") or "draft",
                )
                session.add(shot)
            shot.order_index = int(item.get("order_index") or shot.order_index)
            shot.emotion_tag = item.get("emotion_tag")
            shot.dialogue = item.get("dialogue")
            shot.action_desc = item.get("action_desc")
            shot.image_prompt = item.get("image_prompt")
            shot.final_prompt = item.get("final_prompt")
            shot.asset_anchor_ids = item.get("asset_anchor_ids")
            shot.character_asset_ids = item.get("character_asset_ids")
            shot.scene_asset_id = item.get("scene_asset_id")
            restored += 1
        refreshed = await StoryboardRepository.list_shots(session, int(storyboard.id))
        storyboard.shot_count = len(refreshed)
        storyboard.estimated_duration_ms = sum(int(s.duration_ms) for s in refreshed)

    if record is not None:
        record.op_history = [step for step in history if int(step.get("seq") or 0) <= seq]
        record.last_seq = seq
        record.snapshot = await _shots_snapshot(session, int(storyboard.id))
    await session.commit()
    return {
        "confirmed": True,
        "storyboard_id": int(storyboard.id),
        "target_seq": seq,
        "restored_shots": restored,
        "truncated_steps": max(0, len(history) - len(record.op_history)) if record else 0,
    }


async def batch_patch_shots(
    session: AsyncSession,
    storyboard: Storyboard,
    body: Any,
    user_id: int,
) -> dict[str, Any]:
    """批量修改镜头参数（AC-F4-08-2~3）。

    ``confirm=false`` 只返回「N 个镜头将修改」与逐项变更摘要；
    ``confirm=true`` 执行修改并自动重跑分镜质检相关项（对白超时长标红）。

    Args:
        session: 请求级数据库会话。
        storyboard: 分镜板 ORM 实例。
        body: ShotBatchPatchRequest 实例。
        user_id: 操作人用户 ID。

    Returns:
        ``{confirmed, affected, changes, qc_flags, shots}``。

    Raises:
        BizError: 10001 无任何待修改字段；10002 镜头不存在/不属于该板；
            40001 分镜已锁定。
    """
    if storyboard.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "分镜已锁定，无法批量修改")
    changes = {
        key: value
        for key, value in {
            "camera_move": body.camera_move,
            "duration_ms": body.duration_ms,
            "emotion_tag": body.emotion_tag,
            "shot_size": body.shot_size,
        }.items()
        if value is not None
    }
    if not changes:
        raise BizError(ErrorCode.PARAM_INVALID, "至少提供一个待修改字段")

    shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
    by_id = {int(s.id): s for s in shots}
    missing = [sid for sid in body.shot_ids if sid not in by_id]
    if missing:
        raise BizError(
            ErrorCode.NOT_FOUND, "部分镜头不存在或不属于该分镜板",
            data={"missing_shot_ids": missing},
        )
    targets = [by_id[sid] for sid in body.shot_ids]

    summary = [
        {"field": field, "from": getattr(targets[0], field), "to": value}
        for field, value in changes.items()
    ]
    if not body.confirm:
        return {
            "confirmed": False,
            "affected": len(targets),
            "changes": summary,
            "message": f"{len(targets)} 个镜头将修改",
        }

    before = {
        "shots": [
            {"shot_id": int(s.id), **{field: getattr(s, field) for field in changes}}
            for s in targets
        ]
    }
    for shot in targets:
        for field, value in changes.items():
            setattr(shot, field, value)
    await session.flush()

    # 质检相关项自动重跑：对白镜头时长 > 6s 标红（AC-F4-08-3，与 AC-F4-02-2 同源）
    qc_flags: list[dict[str, Any]] = []
    for shot in targets:
        if shot.dialogue and int(shot.duration_ms) > 6000:
            qc_flags.append({
                "shot_id": int(shot.id),
                "shot_no": int(shot.shot_no),
                "issue": "dialogue_duration_exceeded",
                "message": "对白镜头时长超过 6 秒，请在画布单独调整",
            })

    seq = await record_op(
        session,
        storyboard_id=int(storyboard.id),
        user_id=user_id,
        op_type="batch_edit",
        target_shot_ids=[int(s.id) for s in targets],
        before=before,
        after={"fields": list(changes)},
    )
    await session.commit()
    return {
        "confirmed": True,
        "affected": len(targets),
        "changes": summary,
        "qc_flags": qc_flags,
        "op_seq": seq,
        "shots": [shot_out(s) for s in targets],
    }


async def copy_shot(
    session: AsyncSession,
    storyboard: Storyboard,
    shot_id: int,
    body: Any,
    user_id: int,
) -> dict[str, Any]:
    """复制镜头并插入指定位置（AC-F4-08-1）。

    复制体继承 ``final_prompt/camera_move/duration_ms/emotion_tag/
    asset_anchor_ids``；``order_index`` 与 ``shot_no`` 自动重算，ID 全新。

    Args:
        session: 请求级数据库会话。
        storyboard: 分镜板 ORM 实例。
        shot_id: 源镜头 ID。
        body: ShotCopyRequest 实例。
        user_id: 操作人用户 ID。

    Returns:
        ``{new_shot, order_index, op_seq}``。

    Raises:
        BizError: 10002 源镜头/锚点镜头不存在；40001 分镜已锁定。
    """
    if storyboard.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "分镜已锁定，无法复制镜头")
    source = await StoryboardRepository.get_shot(session, shot_id)
    if source is None or int(source.storyboard_id) != int(storyboard.id):
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在或不属于该分镜板")

    shots = await StoryboardRepository.list_shots(session, int(storyboard.id))
    ordered = [int(s.id) for s in shots]
    if body.mode == "after":
        if body.anchor_shot_id is None or body.anchor_shot_id not in ordered:
            raise BizError(ErrorCode.NOT_FOUND, "锚点镜头不存在或不属于该分镜板")
        insert_at = ordered.index(body.anchor_shot_id) + 1
    else:
        insert_at = len(ordered)
    ordered.insert(insert_at, None)  # type: ignore[arg-type]

    max_no = await StoryboardRepository.max_shot_no(session, int(storyboard.id))
    clone = Shot(
        storyboard_id=int(storyboard.id),
        project_id=int(storyboard.project_id),
        episode_id=int(storyboard.episode_id),
        scene_id=source.scene_id,
        shot_no=max_no + 1,
        order_index=insert_at,
        shot_size=source.shot_size,
        camera_move=source.camera_move,
        emotion_tag=source.emotion_tag,
        duration_ms=int(source.duration_ms),
        dialogue=source.dialogue,
        action_desc=source.action_desc,
        image_prompt=source.image_prompt,
        final_prompt=source.final_prompt,
        negative_prompt=source.negative_prompt,
        asset_anchor_ids=source.asset_anchor_ids,
        character_asset_ids=source.character_asset_ids,
        scene_asset_id=source.scene_asset_id,
        prop_asset_ids=source.prop_asset_ids,
        status="draft",
    )
    session.add(clone)
    await session.flush()

    by_id = {int(s.id): s for s in shots}
    final_order = [int(x) for x in ordered if x is not None]
    final_order.insert(insert_at, int(clone.id))
    for index, sid in enumerate(final_order):
        if sid == int(clone.id):
            clone.order_index = index
        elif sid in by_id:
            by_id[sid].order_index = index
    storyboard.shot_count = len(final_order)
    storyboard.estimated_duration_ms = sum(int(by_id[sid].duration_ms) for sid in final_order if sid in by_id) + int(clone.duration_ms)

    seq = await record_op(
        session,
        storyboard_id=int(storyboard.id),
        user_id=user_id,
        op_type="copy",
        target_shot_ids=[int(clone.id)],
        after={"source_shot_id": int(source.id)},
    )
    await session.commit()
    await session.refresh(clone)
    return {"new_shot": shot_out(clone), "order_index": int(clone.order_index), "op_seq": seq}


async def redraw_shot_frame(
    session: AsyncSession,
    shot: Shot,
    feedback: str | None,
    user_id: int,
    mode: str = "compose",
) -> dict[str, Any]:
    """静帧重出：默认本地合成整幅要素帧，可选文生图。

    Args:
        session: 请求级数据库会话。
        shot: 镜头 ORM 实例。
        feedback: 重出反馈（≤50 字）。
        user_id: 操作人用户 ID。
        mode: ``compose`` 走本地合成（0 成本、不消耗重出次数）；
            ``generate`` 走文生图（每镜 ≤3 次）。

    Returns:
        ``{shot_id, mode, redraft_count, remaining, media_url, degraded,
        elements}``。

    Raises:
        BizError: 60004 重出次数超限（仅 generate）；80001 模型不可用。
    """
    from app.infra.storage import storage as default_storage
    from app.services import shot_frame_service

    # 锁定态兜底：分镜锁定表示画面已定稿，重出会改变成片内容。
    # 前端已禁用按钮，这里再校验一次，避免直接调接口绕过交互约束。
    # 注意：锁定后镜头 status 会被渲染流程改写为 rendered，因此必须按
    # "分镜板状态"判断，不能只看 shot.status。
    storyboard_row = await session.get(Storyboard, int(shot.storyboard_id))
    if (
        str(getattr(shot, "status", "") or "") == "locked"
        or (storyboard_row is not None and str(storyboard_row.status) == "locked")
    ):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "分镜已锁定，请先在镜头抽屉驳回审批解锁后再重出画面",
            data={
                "shot_id": int(shot.id),
                "status": "locked",
                "storyboard_id": int(shot.storyboard_id),
            },
        )

    if mode == "compose":
        result = await shot_frame_service.compose_shot_frame(
            session,
            shot,
            storage=default_storage,
            feedback=feedback,
        )
        await record_op(
            session,
            storyboard_id=int(shot.storyboard_id),
            user_id=user_id,
            op_type="redraw",
            target_shot_ids=[int(shot.id)],
            after={
                "mode": "compose",
                "feedback": feedback,
                "media_url": result["media_url"],
                "degraded": result["degraded"],
            },
        )
        await session.commit()
        return {
            "shot_id": int(shot.id),
            "mode": "compose",
            "redraft_count": int(shot.redraft_count or 0),
            "remaining": max(0, MAX_REDRAFT_COUNT - int(shot.redraft_count or 0)),
            "media_url": result["media_url"],
            "degraded": result["degraded"],
            "elements": result["elements"],
        }

    # ---- generate：沿用文生图与原重出次数限制 ----
    from app.infra.model_gateway.base import GenRequest
    from app.infra.model_gateway.gateway import model_gateway
    from app.models.asset import MediaFile

    if int(shot.redraft_count or 0) >= MAX_REDRAFT_COUNT:
        raise BizError(
            ErrorCode.RETRY_LIMIT_EXCEEDED,
            f"该镜头静帧已重出 {MAX_REDRAFT_COUNT} 次，建议修改 prompt 后重新拆解",
            data={"redraft_count": int(shot.redraft_count or 0), "limit": MAX_REDRAFT_COUNT},
        )

    prompt = shot.final_prompt or shot.image_prompt or shot.action_desc or "画面"
    if feedback:
        prompt = f"{prompt}。调整要求：{feedback}"
    result = await model_gateway.generate(
        "image",
        "text_to_image",
        GenRequest(
            capability="image",
            prompt=prompt,
            params={"size": "720x1280", "shot_no": int(shot.shot_no)},
        ),
        project_id=int(shot.project_id),
    )
    if not result.ok:
        raise BizError(
            ErrorCode.MODEL_UNAVAILABLE, (result.error or "静帧重出失败")[:200]
        )
    media = MediaFile(
        project_id=int(shot.project_id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=result.object_key,
        url=result.media_url,
        media_type="image",
        mime_type=result.meta.get("mime_type"),
        width=result.meta.get("width"),
        height=result.meta.get("height"),
        file_size_bytes=int(result.meta.get("file_size_bytes") or 0),
        meta={"simulated": result.meta.get("simulated"), "redraft": True},
    )
    session.add(media)
    await session.flush()
    shot.first_frame_media_id = int(media.id)
    shot.redraft_count = int(shot.redraft_count or 0) + 1
    await record_op(
        session,
        storyboard_id=int(shot.storyboard_id),
        user_id=user_id,
        op_type="redraw",
        target_shot_ids=[int(shot.id)],
        after={"mode": "generate", "feedback": feedback, "media_url": result.media_url},
    )
    await session.commit()
    return {
        "shot_id": int(shot.id),
        "mode": "generate",
        "redraft_count": int(shot.redraft_count),
        "remaining": max(0, MAX_REDRAFT_COUNT - int(shot.redraft_count)),
        "media_url": result.media_url,
        "degraded": [],
        "elements": None,
    }


async def compose_storyboard_frames(
    session: AsyncSession,
    storyboard: Storyboard,
    shot_ids: list[int] | None,
    user_id: int,
) -> dict[str, Any]:
    """为整板（或指定镜头）批量合成整幅要素首帧。

    单镜失败不阻断整批：逐镜返回状态，前端据此高亮失败镜。

    Args:
        session: 请求级数据库会话。
        storyboard: 分镜板实例。
        shot_ids: 指定镜头 ID；为空表示整板。
        user_id: 操作人用户 ID。

    Returns:
        ``{total, succeeded, failed, results}``，results 每项含
        ``{shot_id, shot_no, status, media_url, degraded, error}``。

    Raises:
        无：单镜异常被捕获并记入 results。
    """
    from app.infra.storage import storage as default_storage
    from app.services import shot_frame_service

    rows = await StoryboardRepository.list_shots(session, int(storyboard.id))
    wanted = {int(item) for item in (shot_ids or [])}
    targets = [shot for shot in rows if not wanted or int(shot.id) in wanted]

    results: list[dict[str, Any]] = []
    succeeded = 0
    for shot in targets:
        try:
            outcome = await shot_frame_service.compose_shot_frame(
                session, shot, storage=default_storage
            )
            succeeded += 1
            results.append(
                {
                    "shot_id": int(shot.id),
                    "shot_no": int(shot.shot_no),
                    "status": "ok",
                    "media_url": outcome["media_url"],
                    "degraded": outcome["degraded"],
                    # 补全产物元数据：旧版只返回 URL，前端无法显示画布尺寸、
                    # 实际合成器（pillow/ffmpeg）与置入要素，排查降级只能查库。
                    "width": outcome.get("width"),
                    "height": outcome.get("height"),
                    "renderer": outcome.get("renderer"),
                    "placed": outcome.get("placed"),
                    "spec_source": outcome.get("spec_source"),
                    "elements": outcome.get("elements"),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单镜失败不应中断整批
            results.append(
                {
                    "shot_id": int(shot.id),
                    "shot_no": int(shot.shot_no),
                    "status": "failed",
                    "media_url": None,
                    "degraded": [],
                    "error": str(exc)[:200],
                }
            )
    await record_op(
        session,
        storyboard_id=int(storyboard.id),
        user_id=user_id,
        op_type="batch_edit",
        target_shot_ids=[int(item["shot_id"]) for item in results],
        after={"action": "compose_frames", "succeeded": succeeded},
    )
    await session.commit()
    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }


async def upsert_director_note(
    session: AsyncSession,
    *,
    project_id: int,
    body: Any,
    user_id: int,
) -> dict[str, Any]:
    """写入/更新导演策略（节奏模式，AC-F4-07-1~3）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        body: DirectorNoteRequest 实例。
        user_id: 操作人用户 ID。

    Returns:
        ``{note, recalculated_shots: 0, protected_manual_episode: bool}``。

    Raises:
        BizError: 10002 集号不存在；10001 模板不存在。
    """
    profile = PACING_PROFILES[body.pacing_mode]
    episode_id: int | None = None
    if body.episode_no is not None:
        episode = (
            await session.execute(
                select(Episode).where(
                    Episode.project_id == project_id,
                    Episode.episode_no == body.episode_no,
                )
            )
        ).scalar_one_or_none()
        if episode is None:
            raise BizError(ErrorCode.NOT_FOUND, f"集号 {body.episode_no} 不存在")
        episode_id = int(episode.id)

    if body.template_id:
        from app.services.template_packs import get_storyboard_template

        if get_storyboard_template(body.template_id) is None:
            raise BizError(
                ErrorCode.PARAM_INVALID,
                f"分镜模板 {body.template_id} 不存在",
                error_detail="storyboard_template_not_found",
            )

    note = (
        await session.execute(
            select(DirectorNote).where(
                DirectorNote.project_id == project_id,
                DirectorNote.episode_id.is_(episode_id)
                if episode_id is None
                else DirectorNote.episode_id == episode_id,
            )
        )
    ).scalar_one_or_none()
    if note is None:
        note = DirectorNote(project_id=project_id, episode_id=episode_id)
        session.add(note)
    note.pacing_mode = body.pacing_mode
    note.closeup_density = (
        body.closeup_density
        if body.closeup_density is not None
        else profile["closeup_density"]
    )
    note.dialogue_camera_rule = body.dialogue_camera_rule or (
        "对话以过肩中近景为主，正反打交替；严格遵守 180° 轴线规则——"
        "同场次相邻镜头人物朝向（facing）相对（一人朝画左、一人朝画右），"
        "不得无故越轴，如需越轴必须插入中性过渡镜；人物视线方向须与"
        "画外对象方位匹配，动作衔接遵守动作匹配剪辑原则"
    )
    note.strategy_notes = {
        **(note.strategy_notes or {}),
        "pacing_hint": profile["hint"],
        "shot_duration_range": [profile["shot_duration_min"], profile["shot_duration_max"]],
    }
    note.template_id = body.template_id
    note.created_by = user_id
    await session.flush()
    await session.commit()
    await session.refresh(note)

    # 重拆保护（AC-F4-07-3）：该集已有手动调整过的分镜板时提示保留
    protected = False
    if episode_id is not None:
        board = await StoryboardRepository.get_by_episode(session, episode_id)
        protected = board is not None and board.status not in ("draft",)
    return {
        "note": director_note_out(note),
        "pacing_profile": profile,
        "protected_manual_episode": protected,
        "message": (
            "该集已有分镜内容，重新拆解前请先创建手动快照（不会静默覆盖）"
            if protected else "节奏模式已保存"
        ),
    }


def director_note_out(note: DirectorNote) -> dict[str, Any]:
    """把导演策略 ORM 对象序列化为响应字典。

    Args:
        note: 导演策略 ORM 实例。

    Returns:
        策略响应字典。
    """
    return {
        "id": int(note.id),
        "project_id": int(note.project_id),
        "episode_id": int(note.episode_id) if note.episode_id else None,
        "pacing_mode": note.pacing_mode,
        "rhythm_curve": note.rhythm_curve,
        "closeup_density": (
            float(note.closeup_density) if note.closeup_density is not None else None
        ),
        "dialogue_camera_rule": note.dialogue_camera_rule,
        "strategy_notes": note.strategy_notes,
        "template_id": note.template_id,
    }


async def get_director_notes(
    session: AsyncSession, project_id: int
) -> dict[str, Any]:
    """查询项目全剧与按集的导演策略（只读可见，可解释性 AC-F4-01-2）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。

    Returns:
        ``{project_note, episode_notes, recommended_pacing}``。
    """
    rows = (
        await session.execute(
            select(DirectorNote).where(DirectorNote.project_id == project_id)
        )
    ).scalars().all()
    project_note = None
    episode_notes: list[dict[str, Any]] = []
    for note in rows:
        if note.episode_id is None:
            project_note = director_note_out(note)
        else:
            episode_notes.append(director_note_out(note))
    project = await session.get(Project, project_id)
    recommended = GENRE_PACING_HINTS.get(
        str(project.genre if project else "") or "", "balanced"
    )
    return {
        "project_note": project_note,
        "episode_notes": episode_notes,
        "recommended_pacing": recommended,
        "available_modes": [
            {"value": key, **value} for key, value in PACING_PROFILES.items()
        ],
    }


async def list_storyboard_templates(
    session: AsyncSession, user_id: int
) -> dict[str, Any]:
    """列出内置 + 用户自定义分镜模板（AC-F4-10-1~2）。

    Args:
        session: 请求级数据库会话。
        user_id: 当前用户 ID。

    Returns:
        ``{builtin: [...], custom: [...], custom_limit}``。
    """
    from app.services.template_packs import (
        MAX_CUSTOM_TEMPLATES,
        list_storyboard_templates as builtin_templates,
    )

    rows = (
        await session.execute(
            select(StoryboardTemplate)
            .where(StoryboardTemplate.owner_user_id == user_id)
            .order_by(StoryboardTemplate.id.asc())
        )
    ).scalars().all()
    return {
        "builtin": builtin_templates(),
        "custom": [
            {
                "template_id": f"custom_{int(row.id)}",
                "id": int(row.id),
                "name": row.name,
                "kind": row.kind,
                "rules": row.rules or [],
                "description": row.description,
                "is_builtin": False,
            }
            for row in rows
        ],
        "custom_limit": MAX_CUSTOM_TEMPLATES,
    }


async def save_storyboard_template(
    session: AsyncSession, *, user_id: int, body: Any
) -> dict[str, Any]:
    """保存用户自定义分镜模板（AC-F4-10-2，上限 20 个）。

    Args:
        session: 请求级数据库会话。
        user_id: 当前用户 ID。
        body: StoryboardTemplateRequest 实例。

    Returns:
        ``{template, total}``。

    Raises:
        BizError: 10001 规则条目非法；10002 已达上限。
    """
    from app.services.template_packs import MAX_CUSTOM_TEMPLATES, validate_template_rules

    try:
        rules = validate_template_rules(body.rules)
    except ValueError as exc:
        raise BizError(ErrorCode.PARAM_INVALID, str(exc), error_detail="template_rules_invalid")

    total = int(
        (
            await session.execute(
                select(func.count(StoryboardTemplate.id)).where(
                    StoryboardTemplate.owner_user_id == user_id
                )
            )
        ).scalar_one()
        or 0
    )
    if total >= MAX_CUSTOM_TEMPLATES:
        raise BizError(
            ErrorCode.ASSET_LIMIT_EXCEEDED,
            f"自定义分镜模板最多 {MAX_CUSTOM_TEMPLATES} 个",
            error_detail="template_limit_exceeded",
        )

    row = StoryboardTemplate(
        owner_user_id=user_id,
        name=body.name,
        kind=body.kind,
        rules=rules,
        description=body.description,
        is_builtin=False,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "template": {
            "template_id": f"custom_{int(row.id)}",
            "id": int(row.id),
            "name": row.name,
            "kind": row.kind,
            "rules": row.rules or [],
            "description": row.description,
            "is_builtin": False,
        },
        "total": total + 1,
    }