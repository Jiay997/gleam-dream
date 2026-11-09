"""资产域服务层模块（Task 7b）。

实现资产全生命周期业务（202 异步任务模型 + 后台 asyncio task）：
- **资产规划**：:func:`plan_assets` 发起后台 LLM 提取（角色/场景/道具
  清单），创建 assets（status=planning，meta 存 appearance/weight），
  同类型同名幂等复用，完成后发布 ``assets.planned``；
- **形象生成**：:func:`generate_version` / :func:`regenerate_version`
  发起后台 stub 文生图（prompt = 画风前缀 + 外观 + 版本类型描述），
  产出 asset_versions（is_current）+ media_files，并执行确定性伪向量
  一致性 QC（face_embeddings + qc_reports），状态流转 generating →
  qc_review → pending_review（failed → qc_failed），发布 ``asset.generated``；
- **人工审核**：approve / reject（feedback ≥10 字）/ lock（校验版本
  齐全且 QC 通过，否则 60003）；
- **辅助**：音色绑定、真人授权留痕、锚点影响面统计、音色库分页。

伪向量语义：非 portrait 版本 embedding 围绕 portrait 基准向量按
``sha256(asset_id + version_type)`` 派生的固定偏移构造，余弦相似度
确定性落在 0.70~0.95 区间（见 :func:`_build_embedding`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import sha256_hash
from app.infra.event_bus import event_bus
from app.infra.model_gateway.base import GenRequest
from app.infra.model_gateway.gateway import model_gateway
from app.models.asset import Asset, AssetVersion, FaceAuthorization, FaceEmbedding, MediaFile
from app.models.collab import StylePreset, SystemConfig
from app.models.project import Project
from app.models.quality import QcReport
from app.models.script import Scene, Script, ScriptLine
from app.repositories.asset_repo import AssetRepository
from app.schemas.asset import (
    AssetGenerateRequest,
    AssetLabelsRequest,
    AssetRejectRequest,
    BatchRegenerateRequest,
    SceneVariantRequest,
    asset_detail_out,
    asset_out,
    asset_version_out,
    face_authorization_out,
    qc_report_out,
    voice_out,
)
from app.services import analytics_service, billing_service
from app.services.consistency_service import make_deterministic_embedding, qc_face_check
from app.services.template_packs import MAX_ASSET_LABELS, MAX_LABEL_LENGTH

logger = logging.getLogger("gleamdream.services.asset")

T = TypeVar("T", bound=BaseModel)

# ---- 单张图像生成的积分单价（成本透明口径，AC-F3-03-2）----
#: 与 model_registry 中 stub-image 的 per_image 单价对齐（积分/张）
IMAGE_UNIT_PRICE: int = 10

#: 场景变体标签白名单（时段 4 + 天气 3，AC-F3-08-1）
SCENE_VARIANT_TAGS: dict[str, list[str]] = {
    "tod": ["dawn", "day", "dusk", "night"],
    "weather": ["sunny", "rain", "snow"],
}

#: 单类变体最多可选数量（AC-F3-08-1【假设待验证：最多各选 2 项】）
MAX_VARIANTS_PER_KIND: int = 2

#: 资产生成互斥锁（按 ``(asset_id, version_type)`` 串行化版本号分配）。
#: 背景：``asset_versions`` 唯一键为 (asset_id, version_type, version_no)，
#: 并发发起同类型生成（如一次提交多个场景变体）会读到同一 ``max+1`` 并撞键，
#: 故在「分配版本号 → 落库」区间内加进程内锁（单实例约束，见 03 文档 ADR-09）。
_GENERATION_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _generation_lock(asset_id: int, version_type: str) -> asyncio.Lock:
    """获取指定 (资产, 版本类型) 的生成互斥锁。

    Args:
        asset_id: 资产 ID。
        version_type: 版本类型。

    Returns:
        该组合对应的 asyncio.Lock 实例（进程内复用）。
    """
    key = (int(asset_id), str(version_type))
    if key not in _GENERATION_LOCKS:
        _GENERATION_LOCKS[key] = asyncio.Lock()
    return _GENERATION_LOCKS[key]


#: 资产提交互斥锁（按 asset_id 串行化「状态校验 → 置 generating」区间）。
#: Q-014：并发同时发 portrait/三视图等多个 generate 时，各请求都会读到
#: 旧状态（非 generating）而全部受理，随后台任务互相覆盖 ``asset.status``，
#: 表现为部分版本静默丢失。提交锁保证同资产的受理判断原子化。
_SUBMIT_LOCKS: dict[int, asyncio.Lock] = {}


def _submit_lock(asset_id: int) -> asyncio.Lock:
    """获取指定资产的提交互斥锁（进程内单例）。

    Args:
        asset_id: 资产 ID。

    Returns:
        该资产对应的 asyncio.Lock 实例。
    """
    key = int(asset_id)
    if key not in _SUBMIT_LOCKS:
        _SUBMIT_LOCKS[key] = asyncio.Lock()
    return _SUBMIT_LOCKS[key]

# ---- 版本类型与资产类型的合法组合（超出组合 → 10001） ----
_ALLOWED_VERSION_TYPES: dict[str, frozenset[str]] = {
    "character": frozenset({
        "portrait", "turnaround_front", "turnaround_side",
        "turnaround_back", "expression_set",
    }),
    "scene": frozenset({"scene_ref"}),
    "prop": frozenset({"prop_ref"}),
    "voice": frozenset(),
}

# 版本类型的中文画面描述（拼入生成 prompt）
# P2-13 修复：显式约束统一背景、三视图姿势一致、画面内禁止任何文字/水印/logo
# 2026-10-29 缺陷四强化：三视图统一"纯白背景 + 从头到脚完整全身 + 脚部完整
# 包含在画面内 + 不触碰画面边缘"；表情集用途隔离为"单张单表情面部特写"
#（仅作人设/配音情绪参考，参考帧准入机制已禁止其进入视频链路）。
_VERSION_TYPE_LABELS: dict[str, str] = {
    "portrait": "角色正面半身主形象，面部清晰，光线均匀，统一纯白背景，单人入画，人物居中不触碰画面边缘，",
    "turnaround_front": (
        "同一角色全身正面转视图，标准 A-pose 自然站立，从头到脚的完整全身像，"
        "脚部完整包含在画面内，人物不触碰、不被画面边缘裁切，服装发型与主形象严格一致，"
        "纯白背景，单人入画，"
    ),
    "turnaround_side": (
        "同一角色全身侧面转视图，标准 A-pose 自然站立，从头到脚的完整全身像，"
        "脚部完整包含在画面内，人物不触碰、不被画面边缘裁切，服装发型与主形象严格一致，"
        "纯白背景，单人入画，"
    ),
    "turnaround_back": (
        "同一角色全身背面转视图，标准 A-pose 自然站立，从头到脚的完整全身像，"
        "脚部完整包含在画面内，人物不触碰、不被画面边缘裁切，服装发型与主形象严格一致，"
        "纯白背景，单人入画，"
    ),
    "expression_set": (
        "同一角色单张单表情面部特写，面部完整居中占画面主要部分，表情清晰自然，"
        "纯色浅背景，单人入画，不与其他表情或元素拼版，"
    ),
    "scene_ref": "场景概念设计图，全景构图，展示空间氛围与光影，无人物，",
    "prop_ref": (
        "道具特写参考图，纯净背景，突出材质细节，单品居中，"
        # 2026-11 QA：道具图里的文字是成片乱码的源头（转账截图/合同条款），
        # 明确要求纸张、屏幕、包装类道具不绘制可读内容。
        "纸张、屏幕、票据、包装类道具不绘制任何可读文字、数字、表格线或界面元素，"
        "仅表现材质、折痕、边缘与光影，"
    ),
}

#: 资产图负向提示词（P2-13：防止图内出现文字/水印/错误形象）
_ASSET_NEGATIVE_PROMPT: str = (
    "文字, 字幕, 字母, 数字, logo, 水印, 签名, UI 元素, 边框, 拼贴, "
    "多人, 重复人物, 多余肢体, 畸形手指, 性别错误, 动物化人形, 低清晰度, 模糊"
)

#: 所有资产图共用的画面一致性约束（拼入正向 prompt 末尾）
_ASSET_CONSISTENCY_CLAUSE: str = (
    "画面中不得出现任何文字、字母、logo、水印、字幕或 UI 元素；统一背景；"
    "角色外观（性别、年龄、发型发色、服装）须与项目已确认形象一致"
)

# 允许发起生成的资产状态（generating 防并发、locked 已锁定）
_GENERATABLE_STATUSES = frozenset({
    "planning", "pending_review", "approved", "rejected", "qc_failed",
})

# 角色资产锁定所需版本类型
_CHARACTER_LOCK_REQUIRED = ("portrait", "turnaround_front", "turnaround_side", "turnaround_back")

# QC 判定为通过的结果集合（warning 仍可人工通过）
_QC_OK_RESULTS = frozenset({"passed", "warning"})

# portrait 抖动系数（regenerate portrait 时模拟真实重绘波动）
_PORTRAIT_JITTER: float = 0.15

# 伪相似度目标区间（sha256(asset_id:version_type) 派生落点）
_SIM_TARGET_MIN: float = 0.70
_SIM_TARGET_SPAN: float = 0.25

# 资产 plan 的剧本文本上限（字符）
_PLAN_SCRIPT_TEXT_LIMIT: int = 6000

# ---- P1-4：资产规划项目级幂等闸门 ----
# 同一项目同时只允许一个规划任务在跑；集合记录进行中的项目 ID。
# 说明：检查与写入之间没有 await，单线程事件循环内天然原子，无需额外锁；
#       进程内闸门适用于当前单进程部署，未来多 worker 需换分布式锁。
_ASSET_PLAN_INFLIGHT: set[int] = set()

# ---- 后台任务强引用集（防 asyncio.Task 被垃圾回收 / 异常静默）----
_BG_TASKS: set[asyncio.Task[None]] = set()

# LLM 自修重试上限
_MAX_SELF_FIX_RETRIES: int = 2

# LLM 单次调用最大输出 token
# 修复（E2E 报告问题 11）：长剧本（10 集/30 场）在 4000 上限下频繁触发
# finish_reason=length 截断，导致契约校验失败并触发整轮重试（耗时翻倍）。
# 提高初始上限到 8000，与自适应翻倍后的档位一致，减少一次无效往返。
_LLM_MAX_TOKENS: int = 8000


def _spawn_bg(coro: Any, *, label: str = "asset-bg") -> asyncio.Task[Any]:
    """创建资产域后台任务并持有强引用，异常落日志避免静默失败。

    Args:
        coro: 后台协程。
        label: 任务标签（仅日志用途）。

    Returns:
        已调度的 asyncio.Task。
    """
    task = asyncio.create_task(coro, name=label)
    _BG_TASKS.add(task)  # type: ignore[arg-type]

    def _on_done(t: asyncio.Task[Any]) -> None:
        """任务完成回调：移除强引用并记录未捕获异常。

        Args:
            t: 完成的 Task。

        Returns:
            None。
        """
        _BG_TASKS.discard(t)  # type: ignore[arg-type]
        if not t.cancelled() and t.exception() is not None:
            logger.exception("资产后台任务异常 label=%s", label, exc_info=t.exception())

    task.add_done_callback(_on_done)
    return task


def _version_image_path_for_qc(object_key: str | None) -> str:
    """把资产媒体的存储对象键解析为磁盘路径（QC 完整性检测用）。

    Args:
        object_key: 存储对象键（可空）。

    Returns:
        磁盘绝对路径字符串；键为空或解析失败时返回空字符串
        （由 check_edge_contact 按图像缺失保守处理）。
    """
    if not object_key:
        return ""
    try:
        from app.infra.storage import storage

        return str(storage.object_path(str(object_key)))
    except Exception:  # noqa: BLE001 - 存储实现差异按缺失处理
        return ""


def _utcnow() -> datetime:
    """获取当前 UTC 时间（timezone-aware）。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


def _normalize(vec: list[float]) -> list[float]:
    """将向量 L2 归一化。

    Args:
        vec: 原始向量。

    Returns:
        归一化后的单位向量（零向量原样返回）。
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]


def _orthogonalize(vec: list[float], ref: list[float]) -> list[float]:
    """将向量相对参考向量正交化（去除平行分量后归一化）。

    Args:
        vec: 待正交化向量。
        ref: 参考单位向量。

    Returns:
        与 ref 正交的单位向量。

    Raises:
        无（退化情形按段序号扰动重派生兜底，不会抛异常）。
    """
    dot = sum(a * b for a, b in zip(vec, ref))
    residual = [a - dot * b for a, b in zip(vec, ref)]
    norm = math.sqrt(sum(v * v for v in residual))
    if norm > 1e-6:
        return [v / norm for v in residual]
    # 极端退化（vec ∥ ref）：用扰动文本重新派生
    fallback = make_deterministic_embedding("ortho-fallback:" + sha256_hash("".join(map(str, vec)))[:16])
    dot2 = sum(a * b for a, b in zip(fallback, ref))
    residual2 = [a - dot2 * b for a, b in zip(fallback, ref)]
    return _normalize(residual2)


def _mix(base: list[float], offset: list[float], k: float) -> list[float]:
    """构造 base + k*offset 后归一化的混合向量。

    Args:
        base: 基准单位向量。
        offset: 与 base 正交的单位向量。
        k: 偏移系数（≥0）。

    Returns:
        归一化混合向量。
    """
    return _normalize([b + k * o for b, o in zip(base, offset)])


def _build_embedding(
    asset_id: int,
    version_type: str,
    version_no: int,
    portrait_emb: list[float] | None,
) -> list[float]:
    """构造资产版本的确定性伪人脸向量。

    规则：
    - portrait：资产基向量 + 正交抖动（系数 0.15，模拟重绘波动）；
    - 非 portrait：围绕 portrait 基准按 ``sha256(asset_id:version_type)``
      派生的目标相似度（0.70~0.95）构造正交偏移向量；
    - 无 portrait 基准时围绕资产基向量构造（QC 记 passed，无 score）。

    Args:
        asset_id: 资产 ID。
        version_type: 版本类型。
        version_no: 版本号（portrait 抖动种子）。
        portrait_emb: portrait 基准向量（可空）。

    Returns:
        128 维单位向量。
    """
    base = make_deterministic_embedding(f"gleamdream:asset:{asset_id}")
    if version_type == "portrait":
        jitter = _orthogonalize(
            make_deterministic_embedding(f"gleamdream:asset:{asset_id}:portrait:v{version_no}"),
            base,
        )
        return _mix(base, jitter, _PORTRAIT_JITTER)
    digest = sha256_hash(f"{asset_id}:{version_type}")
    u = int.from_bytes(bytes.fromhex(digest[:16]), "big") / 2 ** 64
    s_target = _SIM_TARGET_MIN + _SIM_TARGET_SPAN * u
    anchor = portrait_emb if portrait_emb is not None else base
    k = math.sqrt(max(1.0 / (s_target * s_target) - 1.0, 0.0))
    offset = _orthogonalize(
        make_deterministic_embedding(f"gleamdream:asset:{asset_id}:{version_type}:offset"), anchor
    )
    return _mix(anchor, offset, k)


async def _load_qc_thresholds() -> dict[str, float]:
    """从 system_configs 加载 QC 阈值（缺失项用默认值兜底）。

    Returns:
        阈值字典（face_pass_threshold / face_warn_low / qc_pass_threshold）。

    Raises:
        无（数据库异常按默认阈值兜底，不阻断生成主流程）。
    """
    defaults = {
        "face_pass_threshold": 0.80,
        "face_warn_low": 0.68,
        "qc_pass_threshold": 0.80,
    }
    try:
        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(SystemConfig).where(
                        SystemConfig.config_key.in_(defaults.keys())
                    )
                )
            ).scalars().all()
        for row in rows:
            try:
                defaults[row.config_key] = float(row.config_value)
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001 阈值读取失败不阻断生成
        logger.warning("读取 system_configs 阈值失败，使用默认值", exc_info=True)
    return defaults


async def _style_prefix(session: AsyncSession, style_preset_key: str | None) -> str:
    """查询画风预设的正向 prompt 前缀。

    Args:
        session: 异步数据库会话。
        style_preset_key: 画风预设键（可空）。

    Returns:
        prompt 前缀字符串（未命中返回空串）。
    """
    if not style_preset_key:
        return ""
    row = (
        await session.execute(
            select(StylePreset).where(StylePreset.preset_key == style_preset_key)
        )
    ).scalar_one_or_none()
    return str(row.prompt_prefix) if row is not None and row.prompt_prefix else ""


# =====================================================================
# LLM 通用调用（JSON 契约 + 自修循环）
# =====================================================================


def _extract_json(text: str) -> Any:
    """从 LLM 输出文本中提取 JSON 对象（容错围栏与前后缀）。

    Args:
        text: 模型原始输出。

    Returns:
        解析后的 Python 对象。

    Raises:
        ValueError: 无法定位/解析 JSON 时抛出（自修循环据此重试）。
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


async def call_llm_contract(
    *,
    system: str,
    user: str,
    contract_type: type[T],
    project_id: int,
    temperature: float = 0.6,
) -> T:
    """执行一次「LLM 调用 → JSON 提取 → 契约校验 → 自修循环」。

    自修策略：解析/校验失败时把原始输出片段与错误拼回提示词重试，
    最多 :data:`_MAX_SELF_FIX_RETRIES` 次；仍失败抛 BizError(80001)。

    Args:
        system: 五段式系统提示词。
        user: 用户提示词。
        contract_type: pydantic 契约模型。
        project_id: 项目 ID（网关日志关联）。
        temperature: 采样温度。

    Returns:
        校验通过的契约对象。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规。
    """
    last_error = ""
    last_output = ""
    for attempt in range(1 + _MAX_SELF_FIX_RETRIES):
        user_now = user
        if attempt > 0 and last_error:
            user_now = (
                f"{user}\n\n【自修要求】你上一次的输出不合规，请修正后重新输出。"
                f"错误：{last_error}\n上次输出片段：{last_output[:600]}\n"
                "请严格只输出符合契约的 JSON。"
            )
        request = GenRequest(
            capability="llm",
            prompt=user_now,
            system_prompt=system,
            params={"temperature": temperature, "max_tokens": _LLM_MAX_TOKENS, "response_json": True},
        )
        result = await model_gateway.generate("llm", "llm_text", request, project_id=project_id)
        if not result.ok or not result.text:
            last_error = f"模型调用失败：{result.error or '空响应'}"
            last_output = ""
            logger.warning("LLM 调用失败 attempt=%s err=%s", attempt, last_error)
            continue
        try:
            payload = _extract_json(result.text)
            return contract_type.model_validate(payload)
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:500]
            last_output = result.text
            logger.warning("契约校验失败 attempt=%s contract=%s err=%s", attempt, contract_type.__name__, last_error[:200])
    raise BizError(
        ErrorCode.MODEL_UNAVAILABLE,
        f"模型输出持续不合规（{contract_type.__name__}），已重试 {_MAX_SELF_FIX_RETRIES} 次",
        data={"last_error": last_error[:500]},
    )


# =====================================================================
# 资产规划（plan）
# =====================================================================


class PlannedCharacter(BaseModel):
    """LLM 提取的角色资产契约。

    Attributes:
        name: 角色名。
        age_range: 年龄段（如 25-30）。
        appearance: 外形与着装描述。
        weight: 戏份权重（1-5）。
    """

    name: str
    age_range: str | None = None
    appearance: str | None = None
    weight: int | None = None


class PlannedScene(BaseModel):
    """LLM 提取的场景资产契约。

    Attributes:
        name: 场景名。
        time_of_day: 时段（day/night/dawn/dusk）。
        int_ext: 内外景（int/ext）。
        description: 场景描述。
    """

    name: str
    time_of_day: str | None = None
    int_ext: str | None = None
    description: str | None = None


class PlannedProp(BaseModel):
    """LLM 提取的道具资产契约。

    Attributes:
        name: 道具名。
        description: 道具描述。
    """

    name: str
    description: str | None = None


class AssetPlanContract(BaseModel):
    """资产清单 LLM 输出契约。

    Attributes:
        characters: 角色清单。
        scenes: 场景清单。
        props: 道具清单。
    """

    characters: list[PlannedCharacter] = []
    scenes: list[PlannedScene] = []
    props: list[PlannedProp] = []


def _build_plan_prompts(script_text: str) -> tuple[str, str]:
    """构造资产规划的五段式提示词（系统 + 用户）。

    Args:
        script_text: 剧本全文文本。

    Returns:
        (系统提示词, 用户提示词) 二元组。
    """
    system = (
        "【角色与目标】你是短剧项目的美术制片（art_director）。任务：通读剧本，"
        "提取后续 AI 形象生成所需的全量资产清单——出场角色、场景、主要道具。\n"
        "【项目上下文】竖屏短剧（9:16），角色形象需服务连续多集生成，外形描述必须"
        "具体可视觉化（年龄段、发型发色、服装风格、体态气质），同一角色在全部镜头中保持一致。\n"
        "【工作区约定】1. 只输出一个 JSON 对象，不要任何多余文字；2. 名称在同类内唯一且"
        "使用剧中原名；3. appearance 描述 ≤200 字，必须写明性别与年龄段，聚焦可生成视觉特征"
        "（发型发色、服装、体态），同一角色跨集描述必须一致；角色默认是人类形象，"
        "除非剧本明确是动物/拟人角色，否则严禁描述为猫、狗等动物；4. 场景 description "
        "注明空间布局与光线氛围；5. 道具只收录对叙事有意义的物品；6. weight 为戏份权重 1-5"
        "（5 最重，主角 5、配角 2-3、龙套 1）。\n"
        "【预算与边界】禁止输出涉政、色情、血腥内容；禁止使用真实公众人物姓名或肖像特征；"
        "禁止编造剧本中完全不存在的角色。\n"
        "【输出契约】严格按以下 JSON 结构输出：\n"
        '{"characters":[{"name":"人物名","age_range":"如 25-30","appearance":"外形着装描述",'
        '"weight":3}],"scenes":[{"name":"场景名","time_of_day":"day|night|dawn|dusk",'
        '"int_ext":"int|ext","description":"场景描述"}],"props":[{"name":"道具名","description":"道具描述"}]}'
    )
    user = f"请从以下剧本中提取资产清单：\n\n{script_text}"
    return system, user


async def _latest_script_or_error(session: AsyncSession, project: Project) -> Script:
    """获取项目当前剧本（current_script_id 优先，否则最新版本）。

    Args:
        session: 异步数据库会话。
        project: 项目 ORM 实例。

    Returns:
        剧本 ORM 实例。

    Raises:
        BizError: 10002 项目尚无剧本。
    """
    stmt = select(Script).where(Script.project_id == project.id)
    if project.current_script_id:
        stmt = stmt.where(Script.id == project.current_script_id)
    stmt = stmt.order_by(Script.version_no.desc()).limit(1)
    script = (await session.execute(stmt)).scalar_one_or_none()
    if script is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目尚无剧本，请先生成剧本")
    return script


async def _script_text_from_script(session: AsyncSession, script: Script) -> str:
    """把剧本结构化内容拼接为 LLM 可读文本（场次 + 行）。

    Args:
        session: 异步数据库会话（查询场次与行）。
        script: 剧本 ORM 实例。

    Returns:
        截断后的剧本文本（上限 _PLAN_SCRIPT_TEXT_LIMIT 字符）。
    """
    scenes = (
        select(Scene)
        .where(Scene.project_id == script.project_id)
        .order_by(Scene.episode_id.asc(), Scene.scene_no.asc())
    )
    lines = (
        select(ScriptLine)
        .where(ScriptLine.project_id == script.project_id)
        .order_by(ScriptLine.scene_id.asc(), ScriptLine.line_no.asc())
    )
    scene_rows = (await session.execute(scenes)).scalars().all()
    line_rows = (await session.execute(lines)).scalars().all()
    lines_by_scene: dict[int, list[ScriptLine]] = {}
    for line in line_rows:
        lines_by_scene.setdefault(line.scene_id, []).append(line)
    parts: list[str] = [f"剧名：{script.title or '未命名'}；梗概：{script.logline or '无'}"]
    for scene in scene_rows:
        head = f"\n【场次 {scene.scene_no}】{scene.title or scene.location or ''} {scene.int_ext or ''} {scene.time_of_day or ''}"
        parts.append(head)
        for line in lines_by_scene.get(scene.id, []):
            if line.line_type == "dialogue":
                parts.append(f"{line.speaker_name or '未知'}：{line.content}")
            else:
                parts.append(line.content)
    text = "\n".join(parts)
    return text[:_PLAN_SCRIPT_TEXT_LIMIT]


async def plan_assets(session: AsyncSession, project: Project, user_id: int) -> dict[str, Any]:
    """发起资产规划（202 异步任务模型）。

    校验项目存在剧本后，启动后台任务：LLM 提取角色/场景/道具清单 →
    创建 assets（status=planning，meta 存 appearance/weight）→ 发布
    ``assets.planned``。

    P1-4 修复：增加项目级进程内幂等闸门。现象是重复点击会并发拉起多个
    后台任务，各自基于「尚未提交」的清单重复建资产（实测产生 12 个重复
    资产）；同一项目已有规划在跑时直接返回 ``in_progress``，不再重复入队。

    Args:
        session: 请求级数据库会话（仅做前置校验，后台任务自建会话）。
        project: 项目 ORM 实例（ACL 已由路由层保证）。
        user_id: 发起用户 ID。

    Returns:
        202 data：{project_id, status, message}；重复提交时 status=in_progress。

    Raises:
        BizError: 10002 项目尚无剧本。
    """
    await _latest_script_or_error(session, project)
    project_id = int(project.id)
    if project_id in _ASSET_PLAN_INFLIGHT:
        return {
            "project_id": project_id,
            "status": "in_progress",
            "message": "资产规划任务正在执行，请勿重复提交",
        }
    _ASSET_PLAN_INFLIGHT.add(project_id)
    _spawn_bg(_run_asset_plan(project_id, user_id), label="asset-plan")
    await event_bus.publish(
        project_id,
        "asset_plan.started",
        {"message": "资产清单提取任务已受理"},
    )
    return {
        "project_id": project_id,
        "status": "accepted",
        "message": "资产清单提取任务已受理，进度请订阅项目事件流",
    }


async def purge_planning_assets(session: AsyncSession, project_id: int) -> dict[str, Any]:
    """清理项目内滞留 ``planning`` 状态的资产（P1-4 修复）。

    背景：规划任务异常中断或重复提交会留下大量 planning 资产，它们不参与
    生成/锁定，却会永久阻塞「全部实体资产已锁定 → asset_review」的状态
    推进，且此前无删除入口。planning 资产尚未生成任何版本、不会被镜头
    锚点引用，可安全物理删除。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。

    Returns:
        ``{project_id, deleted, asset_ids}``。

    Raises:
        无（无滞留资产时 deleted=0）。
    """
    from sqlalchemy import delete

    ids = list(
        (
            await session.execute(
                select(Asset.id).where(
                    Asset.project_id == project_id,
                    Asset.status == "planning",
                )
            )
        ).scalars().all()
    )
    if ids:
        await session.execute(delete(Asset).where(Asset.id.in_(ids)))
        await session.commit()
    return {"project_id": project_id, "deleted": len(ids), "asset_ids": ids}


async def _run_asset_plan(project_id: int, user_id: int) -> None:
    """后台任务主体：LLM 提取资产清单并落库（幂等复用同名资产）。

    P1-4 修复：批内再按 ``(类型, 名称)`` 去重，防止同一份 LLM 输出内出现
    重名条目；任务结束（成功或失败）都释放项目级 in-flight 标记。

    Args:
        project_id: 项目 ID。
        user_id: 发起用户 ID。

    Raises:
        无（异常兜底记日志并发 ``asset_plan.failed`` 事件，不向调用方传播）。
    """
    try:
        async with async_session_factory() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return
            # ---- P1-4：已存在待确认（planning）资产时直接幂等复用 ----
            # 重复点击不应重复调用 LLM 重新抽取：真实模型每次输出名称可能
            # 略有差异（如「怀表」vs「旧怀表」），精确同名去重仍会新增资产。
            # 因此只要项目里还有尚未进入生成流程的 planning 资产，就视为
            # 上一次规划的成果，直接回放结果（created=0）。
            from sqlalchemy import func as sa_func

            planning_rows = (
                await session.execute(
                    select(Asset.asset_type, sa_func.count())
                    .where(Asset.project_id == project_id, Asset.status == "planning")
                    .group_by(Asset.asset_type)
                )
            ).all()
            planning_total = sum(int(count) for _, count in planning_rows)
            if planning_total > 0:
                counts = {str(asset_type): int(count) for asset_type, count in planning_rows}
                await event_bus.publish(project_id, "assets.planned", {
                    "characters": counts.get("character", 0),
                    "scenes": counts.get("scene", 0),
                    "props": counts.get("prop", 0),
                    "created": 0,
                    "reused": planning_total,
                    "idempotent": True,
                })
                logger.info(
                    "资产规划幂等复用 project_id=%s reused=%s", project_id, planning_total,
                )
                # 事件发布后立即释放闸门（finally 兜底再释放一次）
                _ASSET_PLAN_INFLIGHT.discard(project_id)
                return
            script = await _latest_script_or_error(session, project)
            script_text = await _script_text_from_script(session, script)
            system, user = _build_plan_prompts(script_text)
            plan = await call_llm_contract(
                system=system, user=user, contract_type=AssetPlanContract, project_id=project_id,
            )
            created = 0
            reused = 0
            seen: set[tuple[str, str]] = set()

            def _duplicate_in_batch(asset_type: str, name: str) -> bool:
                """判断同一份 LLM 规划清单内是否出现重复的（类型, 名称）。

                Args:
                    asset_type: 资产类型 character/scene/prop。
                    name: 资产名称（已截断）。

                Returns:
                    True 表示批内已出现，应跳过创建。
                """
                key = (asset_type, name)
                if key in seen:
                    return True
                seen.add(key)
                return False

            for ch in plan.characters:
                name = ch.name[:128]
                if _duplicate_in_batch("character", name):
                    reused += 1
                    continue
                existed = await AssetRepository.find_same_name(session, project_id, "character", name)
                if existed is not None:
                    reused += 1
                    continue
                session.add(Asset(
                    project_id=project_id, owner_user_id=user_id, asset_type="character",
                    name=name, description=ch.appearance, status="planning",
                    source="ai_generated", tags=None,
                    meta={"appearance": ch.appearance, "age_range": ch.age_range, "weight": ch.weight},
                ))
                created += 1
            for sc in plan.scenes:
                name = sc.name[:128]
                if _duplicate_in_batch("scene", name):
                    reused += 1
                    continue
                existed = await AssetRepository.find_same_name(session, project_id, "scene", name)
                if existed is not None:
                    reused += 1
                    continue
                session.add(Asset(
                    project_id=project_id, owner_user_id=user_id, asset_type="scene",
                    name=name, description=sc.description, status="planning",
                    source="ai_generated",
                    meta={"time_of_day": sc.time_of_day, "int_ext": sc.int_ext, "description": sc.description},
                ))
                created += 1
            for prop in plan.props:
                name = prop.name[:128]
                if _duplicate_in_batch("prop", name):
                    reused += 1
                    continue
                existed = await AssetRepository.find_same_name(session, project_id, "prop", name)
                if existed is not None:
                    reused += 1
                    continue
                session.add(Asset(
                    project_id=project_id, owner_user_id=user_id, asset_type="prop",
                    name=name, description=prop.description, status="planning",
                    source="ai_generated", meta={"description": prop.description},
                ))
                created += 1
            await session.commit()
            await event_bus.publish(project_id, "assets.planned", {
                "characters": len(plan.characters),
                "scenes": len(plan.scenes),
                "props": len(plan.props),
                "created": created,
                "reused": reused,
            })
            logger.info("资产规划完成 project_id=%s created=%s reused=%s", project_id, created, reused)
            # 事件发布后立即释放闸门，使紧随其后的重复点击走幂等复用分支
            _ASSET_PLAN_INFLIGHT.discard(project_id)
    except Exception as exc:  # noqa: BLE001 后台任务兜底
        logger.error("资产规划失败 project_id=%s err=%s", project_id, exc, exc_info=exc)
        try:
            await event_bus.publish(
                project_id, "asset_plan.failed",
                {"message": str(exc)[:300]},
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        # P1-4：无论成功/失败都释放 in-flight 标记，避免项目永久禁止重新规划
        _ASSET_PLAN_INFLIGHT.discard(project_id)


# =====================================================================
# 资产列表 / 详情
# =====================================================================


async def list_assets(
    session: AsyncSession,
    project_id: int,
    *,
    asset_type: str | None,
    status: str | None,
    page: int,
    page_size: int,
    search: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """分页查询项目资产列表（支持类型/状态/关键词/标签筛选）。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。
        asset_type: 类型筛选（可空）。
        status: 状态筛选（可空）。
        page: 页码。
        page_size: 每页条数。
        search: 名称/标签关键词（AC-F3-11-1，可空）。
        labels: 标签多选 AND 过滤（AC-F3-11-1，可空）。

    Returns:
        ``{"items", "page", "page_size", "total"}``。

    Raises:
        无（筛选值非法时按空结果处理）。
    """
    items, total = await AssetRepository.list_by_project(
        session,
        project_id,
        asset_type=asset_type,
        status=status,
        page=page,
        page_size=page_size,
        search=search,
        labels=labels,
    )
    return {
        "items": [asset_out(a) for a in items],
        "page": page,
        "page_size": page_size,
        "total": total,
        "search_applied": bool(search or labels),
    }


async def get_asset_or_error(session: AsyncSession, asset_id: int) -> Asset:
    """按 ID 获取存活资产，不存在抛 10002。

    Args:
        session: 异步数据库会话。
        asset_id: 资产 ID。

    Returns:
        资产 ORM 实例。

    Raises:
        BizError: 10002 资产不存在。
    """
    asset = await AssetRepository.get_by_id(session, asset_id)
    if asset is None:
        raise BizError(ErrorCode.NOT_FOUND, "资产不存在")
    return asset


async def get_asset_detail(session: AsyncSession, asset_id: int) -> dict[str, Any]:
    """查询资产详情（详情 + 版本 + QC 报告 + 音色 + 引用镜头数）。

    Args:
        session: 异步数据库会话。
        asset_id: 资产 ID。

    Returns:
        资产详情字典。

    Raises:
        BizError: 10002 资产不存在。
    """
    asset = await get_asset_or_error(session, asset_id)
    versions = await AssetRepository.list_versions(session, asset_id)
    version_items: list[dict[str, Any]] = []
    for version in versions:
        media = await AssetRepository.get_media_file(session, version.media_file_id)
        version_items.append(asset_version_out(version, media_url=media.url if media else None))
    qc_rows = await AssetRepository.list_qc_reports(session, "asset", asset_id)
    voice = None
    if asset.voice_id:
        voice_row = await AssetRepository.get_voice(session, asset.voice_id)
        if voice_row is not None:
            voice = voice_out(voice_row)
    shot_refs = await AssetRepository.count_anchor_shots(session, asset_id)
    return asset_detail_out(
        asset, version_items, [qc_report_out(q) for q in qc_rows], voice, shot_refs,
    )


# =====================================================================
# 形象生成（generate / regenerate）
# =====================================================================


def _validate_version_type(asset: Asset, version_type: str) -> None:
    """校验版本类型与资产类型匹配。

    Args:
        asset: 资产 ORM 实例。
        version_type: 版本类型。

    Raises:
        BizError: 10001 版本类型与资产类型不匹配（voice 资产不可生成）。
    """
    allowed = _ALLOWED_VERSION_TYPES.get(asset.asset_type, frozenset())
    if version_type not in allowed:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"资产类型 {asset.asset_type} 不支持版本类型 {version_type}",
            data={"allowed": sorted(allowed)},
        )


async def generate_version(
    session: AsyncSession, asset: Asset, body: AssetGenerateRequest
) -> dict[str, Any]:
    """发起资产生成（202 异步任务模型）。

    校验状态（generating 防并发、locked 不可再生成）后置 generating，
    启动后台任务：stub 文生图 → asset_versions + media_files → 伪向量
    QC → 状态流转 → ``asset.generated`` 事件。

    Args:
        session: 请求级数据库会话（状态置 generating 在此提交）。
        asset: 资产 ORM 实例。
        body: 生成请求体（version_type + 可选 feedback）。

    Returns:
        202 data：{asset_id, version_type, status, message}。

    Raises:
        BizError: 10001 版本类型不匹配；10002 资产不存在；
            40001 正在生成中或已锁定。
    """
    _validate_version_type(asset, body.version_type)
    # Q-014：同资产并发提交串行化——锁内重读最新状态再判定，
    # 后到的请求显式拿到 40001「正在生成中」，不再静默丢失。
    async with _submit_lock(int(asset.id)):
        await session.refresh(asset)
        if asset.status == "generating":
            raise BizError(ErrorCode.PROJECT_STATE_INVALID, "该资产正在生成中，请稍后再试")
        if asset.status == "locked":
            raise BizError(ErrorCode.PROJECT_STATE_INVALID, "资产已锁定，不可重新生成")
        if asset.status not in _GENERATABLE_STATUSES:
            raise BizError(
                ErrorCode.PROJECT_STATE_INVALID,
                f"资产状态 {asset.status} 不允许发起生成",
            )
        asset.status = "generating"
        await session.commit()
    _spawn_bg(
        _run_generate(int(asset.id), body.version_type, body.feedback),
        label="asset-generate",
    )
    return {
        "asset_id": int(asset.id),
        "version_type": body.version_type,
        "status": "accepted",
        "message": "生成任务已受理，进度请订阅项目事件流",
    }


async def regenerate_version(
    session: AsyncSession, asset: Asset, body: AssetGenerateRequest
) -> dict[str, Any]:
    """重新生成资产（同 generate，产生新版本且旧版本保留）。

    Args:
        session: 请求级数据库会话。
        asset: 资产 ORM 实例。
        body: 生成请求体（version_type + 可选 feedback）。

    Returns:
        202 data（同 :func:`generate_version`）。

    Raises:
        BizError: 同 :func:`generate_version`。
    """
    return await generate_version(session, asset, body)


async def _run_generate(
    asset_id: int,
    version_type: str,
    feedback: str | None,
    variant_tag: str | None = None,
) -> None:
    """后台任务主体：文生图 → 落库 → 伪向量 QC → 状态流转 → 事件。

    Args:
        asset_id: 资产 ID。
        version_type: 版本类型。
        feedback: 用户调整要求（可空）。
        variant_tag: 场景时段/天气变体标签（V4.0，AC-F3-08-1）。

    Raises:
        无（异常兜底置 qc_failed 并发 ``asset.generation_failed`` 事件）。
    """
    try:
        async with _generation_lock(asset_id, version_type), async_session_factory() as session:
            asset = await AssetRepository.get_by_id(session, asset_id)
            if asset is None:
                return
            project = await session.get(Project, asset.project_id) if asset.project_id else None
            style = await _style_prefix(session, project.style_preset_key if project else None)
            appearance = str((asset.meta or {}).get("appearance") or asset.description or "")
            subject = appearance or asset.name
            prompt = "，".join(filter(None, [
                style,
                f"{asset.name}，{subject}",
                _VERSION_TYPE_LABELS.get(version_type, ""),
                _variant_prompt_hint(variant_tag),
                _ASSET_CONSISTENCY_CLAUSE,
            ]))
            if feedback:
                prompt = f"{prompt}。调整要求：{feedback}"
            version_no = await AssetRepository.next_version_no(session, asset_id, version_type)
            seed = int(sha256_hash(f"{asset_id}:{version_type}:{version_no}")[:12], 16) % (2 ** 31)

            result = await model_gateway.generate(
                "image",
                "text_to_image",
                GenRequest(
                    capability="image",
                    prompt=prompt,
                    params={
                        "size": "1024x1024",
                        "asset_name": asset.name,
                        # P2-13：负向提示词随请求下发，真实图像模型据此抑制文字/畸形
                        "negative_prompt": _ASSET_NEGATIVE_PROMPT,
                    },
                ),
                project_id=asset.project_id,
            )
            if not result.ok:
                asset.status = "qc_failed"
                asset.meta = {**(asset.meta or {}), "last_error": (result.error or "")[:300]}
                await session.commit()
                await event_bus.publish(asset.project_id or 0, "asset.generation_failed", {
                    "asset_id": asset_id, "version_type": version_type,
                    "message": (result.error or "生成失败")[:300],
                })
                return

            media = MediaFile(
                owner_user_id=asset.owner_user_id,
                project_id=asset.project_id,
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=result.object_key,
                url=result.media_url,
                media_type="image",
                mime_type=result.meta.get("mime_type"),
                width=result.meta.get("width"),
                height=result.meta.get("height"),
                meta={"simulated": result.meta.get("simulated"), "seed": seed, "task_type": "text_to_image"},
            )
            session.add(media)
            await session.flush()

            await AssetRepository.clear_current(session, asset_id, version_type)
            accumulated = await _accumulated_asset_cost(session, asset_id) + IMAGE_UNIT_PRICE
            version = AssetVersion(
                asset_id=asset_id,
                version_type=version_type,
                version_no=version_no,
                media_file_id=media.id,
                prompt=prompt,
                negative_prompt=_ASSET_NEGATIVE_PROMPT,
                seed=seed,
                model_key=result.meta.get("model_key"),
                is_current=True,
                variant_tag=variant_tag,
                revision_notes=(feedback[:200] if feedback else None),
                cumulative_cost=accumulated,
            )
            session.add(version)
            await session.flush()

            # P1-12 修复：真实图像生成后把成本结算到积分账户（模拟产物不结算）
            billing_result = await _settle_asset_image_cost(
                session,
                owner_user_id=asset.owner_user_id or (project.owner_user_id if project else None),
                project_id=asset.project_id,
                version_id=int(version.id),
                simulated=bool(result.meta.get("simulated")),
            )
            media.meta = {
                **(media.meta or {}),
                "billing": billing_result.get("status"),
                "billing_detail": billing_result,
            }

            # ---- 伪向量一致性 QC ----
            thresholds = await _load_qc_thresholds()
            portrait_emb: list[float] | None = None
            first_portrait = await AssetRepository.first_version(session, asset_id, "portrait")
            if first_portrait is not None:
                pe = await AssetRepository.get_embedding_by_version(session, first_portrait.id)
                if pe is not None:
                    portrait_emb = list(pe.embedding)
            embedding = _build_embedding(asset_id, version_type, version_no, portrait_emb)
            session.add(FaceEmbedding(
                asset_id=asset_id,
                asset_version_id=version.id,
                media_file_id=media.id,
                face_index=0,
                embedding=embedding,
                model_key="deterministic-embedding-v1",
            ))
            if portrait_emb is None:
                score: float | None = None
                qc_result = "passed"
            else:
                score, qc_result = qc_face_check(portrait_emb, embedding, thresholds)
            # ---- P2-11：多主体告警（锚点污染风险）----
            # 现象：场景/角色参考图混入无关人物，单一主体锚点被污染。
            # 处理：提供商若能返回人脸数量（meta.face_count），face_count>1
            # 时把结论降级为 warning（仍可人工通过），并在详情中留痕；
            # 无该字段（stub/部分提供商）时不做猜测，保持原判定。
            try:
                face_count = int((result.meta or {}).get("face_count") or 1)
            except (TypeError, ValueError):
                face_count = 1
            multi_subject = face_count > 1
            if multi_subject and qc_result == "passed":
                qc_result = "warning"
            # ---- 2026-10-29 缺陷四：角色资产完整性检测（触边/缺脚部） ----
            # 对角色类版本做前景包围盒启发式检测：轮廓触及画面边缘或全身像
            # 底部缺少脚部留白时降级为 warning（仍可人工通过），留痕待重生成。
            edge_check: dict[str, Any] = {}
            if asset.asset_type == "character":
                try:
                    from app.services.reference_guard import check_edge_contact

                    edge_check = check_edge_contact(
                        _version_image_path_for_qc(result.object_key)
                    )
                except Exception:  # noqa: BLE001 - 检测失败不阻塞资产生成
                    edge_check = {"checked": False}
                if edge_check.get("checked"):
                    if edge_check.get("edge_contact") or edge_check.get("missing_feet"):
                        if qc_result == "passed":
                            qc_result = "warning"
                    qc_detail_extra = {
                        "edge_contact": bool(edge_check.get("edge_contact")),
                        "missing_feet": bool(edge_check.get("missing_feet")),
                        "bbox_margin": edge_check.get("bbox_margin"),
                    }
                else:
                    qc_detail_extra = {"edge_check": "skipped"}
            else:
                qc_detail_extra = {}
            qc_detail: dict[str, Any] = {
                "version_type": version_type,
                "version_no": version_no,
                "thresholds": thresholds,
                "baseline": "portrait_v1",
                "model": "deterministic-embedding-v1",
                # P2 修复（QC 可信度）：当前一致性分数来自确定性伪向量，
                # 并非真实人脸 embedding；显式标注避免被误读为真实比对结果。
                "simulated": True,
                "note": "沙箱伪向量一致性评分，仅用于流程演示，不代表真实人脸比对结论",
                "face_count": face_count,
                "multi_subject": multi_subject,
            }
            if multi_subject:
                qc_detail["warning"] = "检测到多个主体，参考图可能污染锚点，请人工确认"
            # 合并资产完整性检测结果（触边/缺脚部）进 QC 详情留痕
            qc_detail.update(qc_detail_extra)
            if edge_check.get("checked") and (
                edge_check.get("edge_contact") or edge_check.get("missing_feet")
            ):
                qc_detail["warning"] = (
                    "资产完整性检测告警："
                    + ("轮廓触边 " if edge_check.get("edge_contact") else "")
                    + ("缺脚部留白" if edge_check.get("missing_feet") else "")
                    + "，建议重新生成"
                )
            qc = QcReport(
                project_id=asset.project_id,
                target_type="asset",
                target_id=asset_id,
                target_version_id=version.id,
                result=qc_result,
                consistency_score=round(score, 3) if score is not None else None,
                quality_defects=None,
                detail=qc_detail,
                round_no=1,
                model_key="deterministic-embedding-v1",
            )
            session.add(qc)
            await session.flush()
            version.qc_report_id = qc.id

            asset.current_version_id = version.id
            asset.status = "pending_review" if qc_result in _QC_OK_RESULTS else "qc_failed"
            # 成功产出后清除上一轮生成失败的错误残留，避免详情页继续展示旧错误。
            meta_after_success = dict(asset.meta or {})
            meta_after_success.pop("last_error", None)
            asset.meta = meta_after_success
            await session.commit()

            await event_bus.publish(asset.project_id or 0, "asset.generated", {
                "asset_id": asset_id,
                "version_type": version_type,
                "version_no": version_no,
                "qc_result": qc_result,
                "score": score,
                "status": asset.status,
                "media_url": result.media_url,
            })
            logger.info(
                "资产生成完成 asset_id=%s version_type=%s qc=%s score=%s",
                asset_id, version_type, qc_result, score,
            )
    except Exception as exc:  # noqa: BLE001 后台任务兜底
        logger.error("资产生成失败 asset_id=%s err=%s", asset_id, exc, exc_info=exc)
        try:
            async with async_session_factory() as session:
                asset = await AssetRepository.get_by_id(session, asset_id)
                if asset is not None and asset.status == "generating":
                    asset.status = "qc_failed"
                    asset.meta = {**(asset.meta or {}), "last_error": str(exc)[:300]}
                    await session.commit()
            await event_bus.publish(0, "asset.generation_failed", {"asset_id": asset_id, "message": str(exc)[:300]})
        except Exception:  # noqa: BLE001
            pass


# =====================================================================
# 人工审核（approve / reject / lock）
# =====================================================================


async def approve_asset(session: AsyncSession, asset: Asset, user_id: int) -> dict[str, Any]:
    """审核通过资产（pending_review → approved）。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        资产响应字典。

    Raises:
        BizError: 10002 资产不存在；40001 状态不允许（仅 pending_review 可通过）。
    """
    asset = await get_asset_or_error(session, int(asset.id))
    if asset.status != "pending_review":
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"资产状态 {asset.status} 不允许审核通过（仅 pending_review）",
        )
    asset.status = "approved"
    asset.approved_at = _utcnow()
    await session.commit()
    await session.refresh(asset)
    # 埋点：HITL-2 资产关口通过
    await analytics_service.track_gate(
        analytics_service.GATE_ASSET,
        True,
        user_id=user_id,
        project_id=int(asset.project_id),
    )
    return asset_out(asset)


async def reject_asset(
    session: AsyncSession, asset: Asset, body: AssetRejectRequest, user_id: int
) -> dict[str, Any]:
    """驳回资产（pending_review → rejected，feedback 存 meta.reject_feedback）。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。
        body: 驳回请求体（feedback ≥10 字由 schema 保证）。
        user_id: 操作者用户 ID。

    Returns:
        资产响应字典。

    Raises:
        BizError: 10002 资产不存在；40001 状态不允许（仅 pending_review 可驳回）。
    """
    asset = await get_asset_or_error(session, int(asset.id))
    if asset.status != "pending_review":
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"资产状态 {asset.status} 不允许驳回（仅 pending_review）",
        )
    asset.status = "rejected"
    asset.meta = {**(asset.meta or {}), "reject_feedback": body.feedback}
    await session.commit()
    await session.refresh(asset)
    # 埋点：HITL-2 资产关口驳回
    await analytics_service.track_gate(
        analytics_service.GATE_ASSET,
        False,
        user_id=user_id,
        project_id=int(asset.project_id),
    )
    return asset_out(asset)


async def lock_asset(session: AsyncSession, asset: Asset, user_id: int) -> dict[str, Any]:
    """锁定资产作为生成锚点（校验版本齐全且 QC 通过，否则 60003）。

    校验规则：
    - character：须存在 portrait + 三视图（front/side/back）版本，
      且各版本 QC 非 failed；
    - scene / prop：须存在 scene_ref / prop_ref 版本且 QC 非 failed。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。
        user_id: 操作者用户 ID。

    Returns:
        资产响应字典（status=locked）。

    Raises:
        BizError: 10002 资产不存在；40001 已锁定；60003 版本缺失或 QC 未通过。
    """
    asset = await get_asset_or_error(session, int(asset.id))
    if asset.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "资产已锁定，无需重复锁定")
    if asset.asset_type == "voice":
        raise BizError(ErrorCode.PARAM_INVALID, "音色资产不支持锚点锁定")
    required = _CHARACTER_LOCK_REQUIRED if asset.asset_type == "character" else (
        ("scene_ref",) if asset.asset_type == "scene" else ("prop_ref",)
    )
    # 修复（E2E 报告 P-03）：一次性汇总全部缺失版本与质检未过版本，
    # 此前逐版本短路返回，用户每补一版才被告知下一个缺失项，需多轮往返。
    missing_versions: list[str] = []
    qc_failed_versions: list[str] = []
    for version_type in required:
        version = await AssetRepository.latest_version(session, int(asset.id), version_type)
        if version is None:
            missing_versions.append(version_type)
            continue
        qc = await AssetRepository.get_qc_report(session, version.qc_report_id)
        if qc is not None and qc.result == "failed":
            qc_failed_versions.append(version_type)
    if missing_versions or qc_failed_versions:
        parts: list[str] = []
        if missing_versions:
            parts.append(f"缺少 {'、'.join(missing_versions)} 版本")
        if qc_failed_versions:
            parts.append(f"{'、'.join(qc_failed_versions)} 版本质检未通过")
        raise BizError(
            ErrorCode.QC_NOT_PASSED,
            f"{'；'.join(parts)}，无法锁定",
            data={
                "missing": missing_versions,
                "qc_failed": qc_failed_versions,
                "required": list(required),
            },
        )
    asset.status = "locked"
    asset.locked_at = _utcnow()
    await session.commit()
    await session.refresh(asset)

    # ---- 状态推进钩子：项目全部实体资产锁定后 asset_preparing → asset_review ----
    # （Task 17 联调修复：此前该转换无触发点，项目状态会永久停在
    #   asset_preparing，导致分镜审批 approve 时 40001）
    if asset.project_id is not None:
        unlocked = (
            await session.execute(
                select(Asset.id).where(
                    Asset.project_id == int(asset.project_id),
                    Asset.asset_type.in_(("character", "scene", "prop")),
                    Asset.status != "locked",
                )
            )
        ).scalars().all()
        if not unlocked:
            project = await session.get(Project, int(asset.project_id))
            if project is not None and project.status == "asset_preparing":
                try:
                    from app.services.project_service import transition

                    await transition(
                        project, "asset_review", "全部资产已锁定，进入资产审核",
                        session=session, user_id=user_id,
                    )
                except BizError as exc:
                    logger.warning(
                        "资产锁定后推进 asset_review 失败 project_id=%s: %s",
                        asset.project_id, exc.message,
                    )
    return asset_out(asset)


# =====================================================================
# 音色绑定 / 真人授权 / 影响面
# =====================================================================


async def bind_voice(
    session: AsyncSession, asset: Asset, voice_id: int
) -> dict[str, Any]:
    """为角色资产绑定 TTS 音色（须存在且 available）。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。
        voice_id: 音色 ID。

    Returns:
        资产响应字典（含 voice 摘要）。

    Raises:
        BizError: 10002 音色不存在；10001 音色不可用或资产非角色类型。
    """
    if asset.asset_type != "character":
        raise BizError(ErrorCode.PARAM_INVALID, "仅角色资产可绑定音色")
    voice = await AssetRepository.get_voice(session, voice_id)
    if voice is None:
        raise BizError(ErrorCode.NOT_FOUND, "音色不存在")
    if voice.status != "available":
        raise BizError(ErrorCode.PARAM_INVALID, f"音色当前不可用（status={voice.status}）")
    asset.voice_id = voice_id
    await session.commit()
    await session.refresh(asset)
    return asset_out(asset)


async def create_face_authorization(
    session: AsyncSession,
    asset: Asset,
    authorization_type: str,
    agreement_version: str,
    client_ip: str | None,
    user_agent: str | None,
    user_id: int,
) -> dict[str, Any]:
    """写入真人照片授权留痕（时间戳/IP/协议版本存证）。

    授权记录同时回写资产 meta（face_authorization 摘要）。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。
        authorization_type: 授权类型 self/authorized_other。
        agreement_version: 协议版本号。
        client_ip: 客户端 IP（非法 IP 存 None）。
        user_agent: 客户端 UA。
        user_id: 授权提交人 ID。

    Returns:
        授权记录响应字典。

    Raises:
        BizError: 10001 授权类型非法；10002 资产不存在。
    """
    if authorization_type not in ("self", "authorized_other"):
        raise BizError(ErrorCode.PARAM_INVALID, "authorization_type 仅支持 self/authorized_other")
    ip = client_ip
    if ip:
        import ipaddress

        try:
            ipaddress.ip_address(ip)
        except ValueError:
            ip = None
    auth = FaceAuthorization(
        project_id=asset.project_id,
        user_id=user_id,
        asset_id=asset.id,
        authorization_type=authorization_type,
        protocol_version=agreement_version,
        ip=ip,
        user_agent=(user_agent or "")[:512] or None,
        authorized_at=_utcnow(),
    )
    session.add(auth)
    asset.meta = {
        **(asset.meta or {}),
        "face_authorization": {
            "type": authorization_type,
            "agreement_version": agreement_version,
            "authorized_at": auth.authorized_at.isoformat(),
        },
    }
    await session.commit()
    await session.refresh(auth)
    return face_authorization_out(auth)


async def get_anchor_impact(session: AsyncSession, asset_id: int) -> dict[str, Any]:
    """统计资产作为锚点出现在多少镜头（character_asset_ids 包含匹配）。

    Args:
        session: 异步数据库会话。
        asset_id: 资产 ID。

    Returns:
        ``{"asset_id", "affected_shots"}``。

    Raises:
        BizError: 10002 资产不存在。
    """
    await get_asset_or_error(session, asset_id)
    affected = await AssetRepository.count_anchor_shots(session, asset_id)
    return {"asset_id": asset_id, "affected_shots": affected}


async def list_tts_voices(
    session: AsyncSession,
    *,
    status: str | None,
    page: int,
    page_size: int,
    gender: str | None = None,
    exclude_test: bool = True,
) -> dict[str, Any]:
    """分页查询音色库。

    Args:
        session: 异步数据库会话。
        status: 状态筛选（None 查全部）。
        page: 页码。
        page_size: 每页条数。
        gender: 性别筛选（male/female/neutral，None 不筛选）。
        exclude_test: 是否排除自动化测试音色（默认排除，修复 E2E 问题 9）。

    Returns:
        ``{"items", "page", "page_size", "total"}``。

    Raises:
        无：仅查询数据库。
    """
    from app.services.voice_service import SUPPORTED_VOICE_KEYS

    items, total = await AssetRepository.list_voices(
        session,
        status=status,
        gender=gender,
        exclude_test=exclude_test,
        # P0 修复（音色漂移）：只展示已映射到真实引擎音色的业务音色键，
        # 其余 100+ 条未映射音色选中后都会落到性别默认音色。
        voice_keys=list(SUPPORTED_VOICE_KEYS),
        page=page,
        page_size=page_size,
    )
    return {
        "items": [voice_out(v) for v in items],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# =====================================================================
# V4.0 资产域增量：成本透明 / 标签搜索 / 批量重抽 / 场景变体 / 授权撤回
# =====================================================================


def _variant_prompt_hint(variant_tag: str | None) -> str | None:
    """把变体标签翻译为拼入 prompt 的画面提示。

    Args:
        variant_tag: 变体标签（dawn/day/dusk/night/sunny/rain/snow）。

    Returns:
        画面提示文本；未识别返回 None。
    """
    mapping = {
        "dawn": "清晨光线，低角度柔光，空气通透",
        "day": "明亮日光，高照度，阴影清晰",
        "dusk": "黄昏暖调，逆光轮廓，橙紫渐变天空",
        "night": "夜间低照度，人工光源点亮局部",
        "sunny": "晴天，天空干净无云",
        "rain": "雨天，地面积水反光，空气湿度感",
        "snow": "雪天，积雪覆盖，冷调高光",
    }
    return mapping.get((variant_tag or "").strip().lower())


async def _settle_asset_image_cost(
    session: AsyncSession,
    *,
    owner_user_id: int | None,
    project_id: int | None,
    version_id: int,
    simulated: bool,
) -> dict[str, Any]:
    """按张结算资产图像成本（P1-12 修复：真实图像成本必须记账）。

    历史实现只把成本累加到 ``asset_versions.cumulative_cost``（展示口径），
    积分账户余额从未扣减，项目消耗统计与真实成本脱节。这里在图像生成成功
    后走「冻结 → 结算」链路，并带幂等键避免重试重复扣费。

    Args:
        session: 数据库会话（调用方负责提交）。
        owner_user_id: 资产归属用户 ID（可空）。
        project_id: 项目 ID（可空）。
        version_id: 新生成的资产版本 ID（作为幂等键组成部分）。
        simulated: 是否沙箱模拟产物；模拟产物不产生真实成本，不结算。

    Returns:
        ``{status, ...}``：skipped（模拟/无账户）/settled（已结算）/
        failed（余额不足等，附 code/message）。

    Raises:
        无：计费失败不阻断已生成的资产入库。
    """
    if simulated:
        return {"status": "skipped", "reason": "simulated"}
    if not owner_user_id:
        return {"status": "skipped", "reason": "no_owner"}
    account = await billing_service.BillingRepository(session).get_account_by_user(
        int(owner_user_id)
    )
    if account is None:
        return {"status": "skipped", "reason": "no_account"}
    idempotency_key = f"asset-image:{int(version_id)}"
    try:
        freeze = await billing_service.precheck_and_freeze(
            session,
            account_id=int(account.id),
            project_id=project_id,
            task_type="asset_image",
            estimated_credits=IMAGE_UNIT_PRICE,
            idempotency_key=idempotency_key,
        )
        detail = await billing_service.settle(
            session, freeze_id=int(freeze.id), actual_credits=IMAGE_UNIT_PRICE
        )
    except BizError as exc:
        logger.warning(
            "资产图像结算失败 version_id=%s code=%s message=%s",
            version_id, exc.code, str(exc.message)[:200],
        )
        return {"status": "failed", "code": exc.code, "message": str(exc.message)[:200]}
    except Exception as exc:  # noqa: BLE001 计费异常不影响资产生成
        logger.warning("资产图像结算异常 version_id=%s：%s", version_id, str(exc)[:200])
        return {"status": "failed", "message": str(exc)[:200]}
    return {"status": "settled", **detail}


async def _accumulated_asset_cost(session: AsyncSession, asset_id: int) -> int:
    """统计资产已累计消耗积分（取最新版本的累计值）。

    Args:
        session: 异步数据库会话。
        asset_id: 资产 ID。

    Returns:
        累计消耗积分（无版本时为 0）。
    """
    versions = await AssetRepository.list_versions(session, asset_id)
    if not versions:
        return 0
    return max((int(v.cumulative_cost or 0) for v in versions), default=0)


async def estimate_regenerate_cost(
    session: AsyncSession, asset: Asset
) -> dict[str, Any]:
    """重抽成本透明预估（V4.0 P0 细节功能 SF-F3-12 / AC-F3-03-2）。

    返回「本次预估 X 积分 + 该资产已累计消耗 Y 积分 / 已重抽 N 次」；
    当 ``Y > 成本基线 × 2`` 时追加「建议换描述策略」提示（不阻断）。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。

    Returns:
        ``{asset_id, per_version_credits, accumulated_credits, regenerate_count,
        cost_baseline, suggest_change_strategy, hint}``。
    """
    versions = await AssetRepository.list_versions(session, int(asset.id))
    accumulated = max((int(v.cumulative_cost or 0) for v in versions), default=0)
    regenerate_count = max(len(versions) - 1, 0)
    baseline = IMAGE_UNIT_PRICE * 3
    suggest = accumulated > baseline * 2
    return {
        "asset_id": int(asset.id),
        "per_version_credits": IMAGE_UNIT_PRICE,
        "accumulated_credits": accumulated,
        "regenerate_count": regenerate_count,
        "cost_baseline": baseline,
        "suggest_change_strategy": suggest,
        "hint": (
            f"本次预估 {IMAGE_UNIT_PRICE} 积分；该资产已累计消耗 {accumulated} 积分"
            f"（已重抽 {regenerate_count} 次）"
            + ("；建议换描述策略而非继续盲抽" if suggest else "")
        ),
    }


async def update_asset_labels(
    session: AsyncSession, asset: Asset, labels: list[str], user_id: int
) -> dict[str, Any]:
    """更新资产可检索标签（AC-F3-11-1~2）。

    标签约束：≤10 个/资产、单标签 ≤12 字、去重且去首尾空白。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。
        labels: 标签数组。
        user_id: 操作人用户 ID（审计留痕）。

    Returns:
        更新后的资产响应字典。

    Raises:
        BizError: 10001 标签数量/长度超限。
    """
    cleaned: list[str] = []
    for raw in labels:
        text = str(raw).strip()
        if not text:
            continue
        if len(text) > MAX_LABEL_LENGTH:
            raise BizError(
                ErrorCode.PARAM_INVALID,
                f"单个标签不超过 {MAX_LABEL_LENGTH} 字",
                error_detail="asset_label_too_long",
            )
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_ASSET_LABELS:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"单个资产最多 {MAX_ASSET_LABELS} 个标签",
            error_detail="asset_label_limit_exceeded",
        )
    asset.labels = cleaned
    await session.commit()
    await session.refresh(asset)
    return asset_out(asset)


async def batch_regenerate(
    session: AsyncSession,
    project: Project,
    body: BatchRegenerateRequest,
    user_id: int,
) -> dict[str, Any]:
    """批量重抽：多角色应用同一条反馈，逐角色独立执行（AC-F3-10-1~2）。

    ``confirm=False`` 时只返回成本预估不执行（「N 个角色 × 单价 = 预估积分」）。
    执行时逐资产独立发起后台生成：单角色失败不阻塞其余；未完成真人授权的
    角色（授权已撤回）与版本类型不匹配的资产被单独拦截并说明原因
    （AC-F3-10-2 / AC-F3-12-2），不阻断整批。

    Args:
        session: 请求级数据库会话。
        project: 项目 ORM 实例（ACL 已校验）。
        body: 批量重抽请求。
        user_id: 操作人用户 ID。

    Returns:
        ``{confirmed, total, estimated_credits, per_asset_credits, accepted,
        blocked}``。

    Raises:
        BizError: 10002 资产不存在；10001 资产不属于该项目。
    """
    assets = await AssetRepository.list_by_ids(session, list(body.asset_ids))
    by_id = {int(a.id): a for a in assets}
    missing = [aid for aid in body.asset_ids if aid not in by_id]
    if missing:
        raise BizError(
            ErrorCode.NOT_FOUND, "部分资产不存在", data={"missing_asset_ids": missing}
        )
    for asset in assets:
        if asset.project_id is not None and int(asset.project_id) != int(project.id):
            raise BizError(
                ErrorCode.PARAM_INVALID,
                f"资产 {int(asset.id)} 不属于当前项目",
                error_detail="asset_project_mismatch",
            )

    # 版本类型不匹配的资产单独拦截，不阻断整批（AC-F3-10-2）
    feasible: list[Asset] = []
    pre_blocked: list[dict[str, Any]] = []
    for asset in assets:
        allowed = _ALLOWED_VERSION_TYPES.get(asset.asset_type, frozenset())
        if body.version_type not in allowed:
            pre_blocked.append(
                {
                    "asset_id": int(asset.id),
                    "asset_name": asset.name,
                    "reason": (
                        f"资产类型 {asset.asset_type} 不支持版本类型 {body.version_type}"
                    ),
                    "error_detail": "version_type_mismatch",
                }
            )
            continue
        feasible.append(asset)

    total = len(feasible)
    estimated = total * IMAGE_UNIT_PRICE
    if not body.confirm:
        return {
            "confirmed": False,
            "total": total,
            "per_asset_credits": IMAGE_UNIT_PRICE,
            "estimated_credits": estimated,
            "formula": f"{total} 个资产 × {IMAGE_UNIT_PRICE} 积分 = {estimated} 积分",
            "accepted": [],
            "blocked": pre_blocked,
        }

    accepted: list[int] = []
    blocked: list[dict[str, Any]] = list(pre_blocked)
    for asset in feasible:
        asset_id = int(asset.id)
        # 真人授权撤回拦截（AC-F3-10-2）：仅该角色终止，其余继续
        if (asset.meta or {}).get("face_source") or (asset.meta or {}).get("face_authorization"):
            auth = await AssetRepository.active_authorization(session, asset_id)
            if auth is None:
                blocked.append({
                    "asset_id": asset_id,
                    "asset_name": asset.name,
                    "reason": "真人授权已撤回，禁止发起新的生成",
                    "error_detail": "authorization_revoked",
                })
                continue
        if asset.status == "generating":
            blocked.append({
                "asset_id": asset_id,
                "asset_name": asset.name,
                "reason": "该资产正在生成中",
                "error_detail": "asset_generating",
            })
            continue
        if asset.status == "locked":
            blocked.append({
                "asset_id": asset_id,
                "asset_name": asset.name,
                "reason": "资产已锁定，不可重新生成",
                "error_detail": "asset_locked",
            })
            continue
        if asset.status not in _GENERATABLE_STATUSES:
            blocked.append({
                "asset_id": asset_id,
                "asset_name": asset.name,
                "reason": f"资产状态 {asset.status} 不允许发起生成",
                "error_detail": "asset_state_invalid",
            })
            continue
        asset.status = "generating"
        accepted.append(asset_id)
    await session.commit()

    for asset_id in accepted:
        _spawn_bg(
            _run_generate(asset_id, body.version_type, body.feedback),
            label="asset-batch-regenerate",
        )
    logger.info(
        "批量重抽受理 project_id=%s total=%s accepted=%s blocked=%s",
        project.id, total, len(accepted), len(blocked),
    )
    return {
        "confirmed": True,
        "total": total,
        "per_asset_credits": IMAGE_UNIT_PRICE,
        "estimated_credits": estimated,
        "accepted": accepted,
        "blocked": blocked,
        "message": "批量重抽已受理，逐角色独立执行（单角色失败不阻塞其余）",
    }


async def generate_scene_variants(
    session: AsyncSession,
    asset: Asset,
    body: SceneVariantRequest,
    user_id: int,
) -> dict[str, Any]:
    """为场景资产生成时段/天气变体（AC-F3-08-1~2）。

    每个变体生成独立 ``asset_version`` 并以「场景 × 变体」标注
    ``variant_tag``；同类变体最多选 2 项。

    Args:
        session: 请求级数据库会话。
        asset: 资产 ORM 实例。
        body: 变体请求。
        user_id: 操作人用户 ID。

    Returns:
        ``{asset_id, accepted_variants, estimated_credits, message}``。

    Raises:
        BizError: 10001 资产非场景类型/变体标签非法/同类超限；
            40001 资产状态不允许。
    """
    if asset.asset_type != "scene":
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "仅场景资产支持生成时段/天气变体",
            error_detail="variant_asset_type_invalid",
        )
    if asset.status == "locked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "资产已锁定，不可生成变体")
    if asset.status == "generating":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "该资产正在生成中，请稍后再试")

    tod_selected = [v for v in body.variants if v in SCENE_VARIANT_TAGS["tod"]]
    weather_selected = [v for v in body.variants if v in SCENE_VARIANT_TAGS["weather"]]
    unknown = [v for v in body.variants if v not in {*SCENE_VARIANT_TAGS["tod"], *SCENE_VARIANT_TAGS["weather"]}]
    if unknown:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"不支持的变体标签：{'/'.join(unknown)}",
            data={"allowed": {**_maybe_dict(SCENE_VARIANT_TAGS)}},
            error_detail="variant_tag_invalid",
        )
    if len(tod_selected) > MAX_VARIANTS_PER_KIND or len(weather_selected) > MAX_VARIANTS_PER_KIND:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"时段与天气变体各最多选 {MAX_VARIANTS_PER_KIND} 项",
            error_detail="variant_limit_exceeded",
        )

    asset.status = "generating"
    await session.commit()
    for tag in tod_selected + weather_selected:
        _spawn_bg(
            _run_generate(int(asset.id), "scene_ref", None, tag),
            label="asset-scene-variant",
        )
    return {
        "asset_id": int(asset.id),
        "accepted_variants": tod_selected + weather_selected,
        "estimated_credits": len(tod_selected + weather_selected) * IMAGE_UNIT_PRICE,
        "message": "场景变体生成已受理，进度请订阅项目事件流",
    }


def _maybe_dict(value: dict[str, list[str]]) -> dict[str, list[str]]:
    """原样返回变体白名单（便于响应体携带可选值）。

    Args:
        value: 变体标签白名单。

    Returns:
        同一字典。
    """
    return value


async def list_scene_variants(
    session: AsyncSession, asset: Asset
) -> dict[str, Any]:
    """列出场景资产的变体覆盖情况（AC-F3-08-2「变体缺失」判定依据）。

    Args:
        session: 异步数据库会话。
        asset: 资产 ORM 实例。

    Returns:
        ``{asset_id, variants: [{variant_tag, versions, is_current}],
        missing: [未生成的白名单标签]}``。
    """
    versions = await AssetRepository.list_versions(session, int(asset.id))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for version in versions:
        if not version.variant_tag:
            continue
        media = await AssetRepository.get_media_file(session, version.media_file_id)
        grouped.setdefault(version.variant_tag, []).append(
            asset_version_out(version, media_url=media.url if media else None)
        )
    all_tags = SCENE_VARIANT_TAGS["tod"] + SCENE_VARIANT_TAGS["weather"]
    return {
        "asset_id": int(asset.id),
        "variants": [
            {
                "variant_tag": tag,
                "versions": items,
                "is_current": any(v.get("is_current") for v in items),
            }
            for tag, items in grouped.items()
        ],
        "missing": [tag for tag in all_tags if tag not in grouped],
        "available_tags": dict(SCENE_VARIANT_TAGS),
    }


async def list_face_authorizations(
    session: AsyncSession,
    *,
    user_id: int,
    project_id: int | None,
    status: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """分页查询真人授权记录（AC-F3-12-1）。

    列表项包含角色名、授权时间、IP、协议版本与状态 active/revoked。

    Args:
        session: 异步数据库会话。
        user_id: 授权提交人用户 ID。
        project_id: 项目筛选。
        status: 状态筛选。
        page: 页码。
        page_size: 每页条数。

    Returns:
        ``{items, page, page_size, total}``。
    """
    rows, total = await AssetRepository.list_authorizations(
        session,
        user_id=user_id,
        project_id=project_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    items: list[dict[str, Any]] = []
    for auth in rows:
        data = face_authorization_out(auth)
        asset = (
            await AssetRepository.get_by_id(session, int(auth.asset_id))
            if auth.asset_id
            else None
        )
        data["asset_name"] = asset.name if asset else None
        data["asset_type"] = asset.asset_type if asset else None
        items.append(data)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


async def revoke_face_authorization(
    session: AsyncSession,
    auth: FaceAuthorization,
    *,
    confirm_name: str,
    reason: str | None,
    user_id: int,
) -> dict[str, Any]:
    """撤回真人授权（AC-F3-12-2~3）。

    语义：授权记录置 ``revoked``、该角色禁止发起新的镜头生成
    （返回 50003 + ``error_detail=authorization_revoked``）、已生成内容保留、
    撤回写入 ``audit_logs``；进行中的生成任务照常完成并结算。

    Args:
        session: 请求级数据库会话。
        auth: 授权记录 ORM 实例。
        confirm_name: 二次确认的角色名（须与资产名一致）。
        reason: 撤回原因（可空）。
        user_id: 操作人用户 ID。

    Returns:
        ``{authorization, revoked_shots_blocked: True}``。

    Raises:
        BizError: 10001 角色名不匹配；40001 授权已撤回。
    """
    from app.models.iam import AuditLog

    if auth.status == "revoked":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "该授权已撤回")
    asset = (
        await AssetRepository.get_by_id(session, int(auth.asset_id))
        if auth.asset_id
        else None
    )
    if asset is None:
        raise BizError(ErrorCode.NOT_FOUND, "关联资产不存在")
    if confirm_name.strip() != (asset.name or "").strip():
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "二次确认的角色名与资产名不一致",
            error_detail="confirm_name_mismatch",
        )

    auth.status = "revoked"
    auth.revoked_at = _utcnow()
    auth.revoke_reason = (reason or "")[:200] or None
    asset.meta = {
        **(asset.meta or {}),
        "face_authorization_revoked_at": auth.revoked_at.isoformat(),
    }
    session.add(
        AuditLog(
            actor_type="user",
            actor_user_id=user_id,
            project_id=int(auth.project_id) if auth.project_id else None,
            action="face_authorization_revoked",
            target_type="asset",
            target_id=int(asset.id),
            detail={
                "authorization_id": int(auth.id),
                "asset_name": asset.name,
                "reason": auth.revoke_reason,
            },
        )
    )
    await session.commit()
    await session.refresh(auth)
    logger.info(
        "真人授权已撤回 authorization_id=%s asset_id=%s", auth.id, asset.id
    )
    return {"authorization": face_authorization_out(auth), "shots_blocked": True}


async def ensure_face_authorization_ok(session: AsyncSession, asset: Asset) -> None:
    """真人定制角色的授权前置校验（AC-F3-05-2 / AC-F3-12-2）。

    仅对「真人照片定制」角色生效（``meta.face_source`` 存在）；若存在授权
    记录但已全部撤回，则拒绝并返回 ``50003 + authorization_revoked``。

    Args:
        session: 异步数据库会话。
        asset: 角色资产 ORM 实例。

    Raises:
        BizError: 50003 未完成真人授权或授权已撤回。
    """
    meta = asset.meta or {}
    if not (meta.get("face_source") or meta.get("face_authorization")):
        return
    if await AssetRepository.active_authorization(session, int(asset.id)) is not None:
        return
    raise BizError(
        ErrorCode.HUMAN_AUTH_MISSING,
        "真人授权已撤回，不可用于新的镜头生成",
        error_detail="authorization_revoked",
    )