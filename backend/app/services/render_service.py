"""渲染任务服务模块（Task 8 · 生成执行域）。

职责（全部写路径单事务提交后再发 SSE 事件）：
- :func:`estimate`：按镜头预估渲染成本（视频单价 = 路由命中模型单价 ×
  ``ceil(duration_ms / 1000)``）；
- :func:`batch_submit`：批量提交渲染任务（Idempotency-Key 批次幂等、
  逐镜头冻结、入队执行器）；
- :func:`cancel_task` / :func:`retry_task` / :func:`regenerate_shot`：
  取消（全额退款）/ 重试（复用或重新冻结）/ 单镜重新生成；
- :func:`get_task_detail` / :func:`list_tasks`：任务详情与分页列表；
- :func:`publish_task_event` / :func:`publish_batch_completed_if_done`：
  SSE 事件发布（``render.task.updated`` / ``render.batch.completed``）。

计费约定：
- 每个渲染任务对应一条独立冻结（幂等键 ``{批次键}:{shot_id}``），
  取消按任务全额退款、成功按任务实际消耗结算，互不影响同批其它任务；
- 实际成本 = ``Σ ai_call_logs.cost_credits``（含 QC 修正循环的每轮消耗）。

状态约定（对齐 render_tasks / shots 状态机）：
- 提交：shot ``locked → render_queued``，任务 ``queued``；
- 执行器接管 ``render_queued → rendering → rendered / render_failed /
  needs_manual``；
- 取消：任务 ``cancelled``，shot 回 ``locked``；重试：任务回 ``queued``。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id, sha256_hash
from app.infra.event_bus import event_bus
from app.infra.model_gateway import pick_model
from app.models import MediaFile, RenderTask, Shot, ShotVersion
from app.models.project import Project
from app.models.render import RenderBatch
from app.models.v6 import ShotLineBinding, VoiceClip
from app.repositories.billing_repo import BillingRepository
from app.services import anchor_service, motion_service
from app.repositories.render_repo import (
    RenderRepository,
    batch_freeze_key,
    batch_task_key,
    int_param,
    sanitize_idempotency_key,
    str_param,
)
from app.services import billing_service, message_mapping

#: 批次确认单分批大小（AC-F5-01-2：批 50）
PER_BATCH_SIZE: int = 50
#: 渲染管线版本（参与 input_hash；画面生成逻辑变化时递增，避免复用旧产物）
#: v10（2026-11-03）：提示词层新增「运镜视野不扩大」「画面文字留白」「可读
#:   道具内容虚化」；首帧层新增「抠像自适应阈值」「人物水平安全边 4%」。
#:   管线版本递增后，历史镜头重新渲染会真正重生成，而不是命中旧缓存。
#: v9：首帧优先级改为「本镜合成首帧 → 同场尾帧 → 资产图」+ 提交前自动合成首帧
#:     + 换场禁止尾帧续接（v8 为三要素动态 prompt + 真实 I2V + 尾帧续接 + 运动 QC）
RENDER_PIPELINE_VERSION: str = "i2v-motion-v10-20261103"

logger = logging.getLogger("gleamdream.services.render")

# 幂等键前缀（批次）与默认单价（model_registry 缺失兜底）
_DEFAULT_VIDEO_UNIT_PRICE: int = 30
# 占位模型（stub）最小冻结单价：保留余额护栏，实际结算 0（P2 成本展示修复）
_STUB_VIDEO_UNIT_PRICE: int = 1
# 单镜头 QC 修正轮次上限（含首轮；静态镜头最多重试 3 次 → 共 4 轮）
QC_MAX_ROUNDS: int = 4
# regenerate 任务的批次键前缀
_REGEN_BATCH_PREFIX = "regen"


def _utcnow() -> datetime:
    """获取当前 UTC 时间。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


def compute_estimated_credits(duration_ms: int, unit_price: int) -> int:
    """按时长与单价计算预估成本：``ceil(duration_ms / 1000) * unit_price``。

    Args:
        duration_ms: 镜头时长（毫秒，>0）。
        unit_price: 视频模型单价（积分/秒）。

    Returns:
        预估成本（积分，向上取整到秒）。
    """
    return max(1, math.ceil(duration_ms / 1000)) * max(0, unit_price)


def build_input_hash(
    prompt: str | None,
    duration_ms: int,
    anchor_ids: list[int] | None,
    voice_clip_ids: list[int] | None = None,
    anchor_pack_hash: str | None = None,
    aspect_ratio: str | None = None,
    frame_media_id: int | None = None,
    camera_move: str | None = None,
    continuity_media_id: int | None = None,
) -> str:
    """构造渲染任务入参哈希。

    摘要覆盖 ``prompt | duration | 锚点IDs | 配音片段IDs | 锚点包哈希 |
    画幅 | 首帧媒体ID | 运镜 | 上一镜尾帧媒体ID | 渲染管线版本``。

    首帧参与摘要的原因：整幅要素画面由本地合成产出，换帧后若哈希不变，
    任务会被幂等复用旧产物，表现为"改了首帧但成片没变"。

    Args:
        prompt: 最终组装 prompt（可空按空串参与摘要）。
        duration_ms: 镜头时长（毫秒）。
        anchor_ids: 锚点资产版本 ID 列表（排序后参与摘要）。
        voice_clip_ids: F5.13 音画对齐输入——镜头绑定的最新配音片段 ID。
        anchor_pack_hash: F3.13 多维锚点包引用摘要（缺包时为空）。
        aspect_ratio: 项目画幅（9:16/16:9/1:1，可空）。
        frame_media_id: 镜头首帧媒体 ID（可空）。
        camera_move: 运镜枚举（可空）；参与摘要，改运镜后必须重新生成视频。
        continuity_media_id: 上一镜当前版本媒体 ID（尾帧续接来源，可空）。

    Returns:
        64 位十六进制哈希。
    """
    anchors = ",".join(str(a) for a in sorted(anchor_ids or []))
    voices = ",".join(str(v) for v in sorted(voice_clip_ids or []))
    pack = anchor_pack_hash or ""
    ratio = aspect_ratio or ""
    frame = "" if frame_media_id is None else str(int(frame_media_id))
    move = str(camera_move or "")
    continuity = "" if continuity_media_id is None else str(int(continuity_media_id))
    return sha256_hash(
        f"{prompt or ''}|{duration_ms}|{anchors}|{voices}|{pack}|{ratio}|{frame}"
        f"|{move}|{continuity}|{RENDER_PIPELINE_VERSION}"
    )


def resolve_task_type(shot: Shot) -> str:
    """根据镜头是否绑定首帧图推导任务类型。

    Args:
        shot: 镜头实例。

    Returns:
        ``image_to_video``（有首帧）或 ``text_to_video``。
    """
    return "image_to_video" if shot.first_frame_media_id else "text_to_video"


def resolve_priority(shot: Shot) -> int:
    """推导任务优先级（1~9，值越小越优先；DB CHECK 约束 1~9）。

    规则：每集（分镜板）前 3 镜优先——按镜号 1/2/3 给 priority 1/2/3，
    其余镜头 priority=5（契约中「100-序号加成」受 DB CHECK 约束 1~9
    限制，折算为小序号高优先的等价表达）。

    Args:
        shot: 镜头实例。

    Returns:
        1~9 的优先级整数。
    """
    if 1 <= int(shot.shot_no) <= 3:
        return int(shot.shot_no)
    return 5


async def _video_unit_price(session: AsyncSession, task_type: str) -> int:
    """查询视频模型单价（确定性路由命中；路由失败兜底默认价）。

    P2 修复（成本展示失真）：占位模型 ``stub-*`` 在注册表里仍按真实模型
    定价（30 积分/秒，3 秒镜头预估/冻结 90），但实际渲染完成后按降级
    产物结算 0，页面出现「预估 900 / 实际 0」的矛盾展示。这里对 stub
    路由只按最小 1 积分/秒冻结（保留冻结机制与余额护栏），结算仍为 0。

    Args:
        session: 数据库会话。
        task_type: 任务类型（text_to_video / image_to_video）。

    Returns:
        单价（积分/秒）；stub 路由返回 :data:`_STUB_VIDEO_UNIT_PRICE`。
    """
    try:
        registry = await pick_model(session, "video", task_type)
        if str(registry.provider or "") == "stub" or str(registry.model_key or "").startswith("stub"):
            return _STUB_VIDEO_UNIT_PRICE
        return int(registry.unit_price_credits or _DEFAULT_VIDEO_UNIT_PRICE)
    except BizError:
        return _DEFAULT_VIDEO_UNIT_PRICE


async def publish_task_event(
    project_id: int,
    task_id: int,
    shot_id: int | None,
    status: str,
    *,
    attempt: int | None = None,
    progress: int | None = None,
) -> None:
    """发布渲染任务状态变更 SSE 事件（``render.task.updated``）。

    须在任务状态落库提交后调用（契约：先写 DB 后发事件）。

    Args:
        project_id: 项目 ID。
        task_id: 渲染任务 ID。
        shot_id: 镜头 ID（可空）。
        status: 最新任务状态。
        attempt: 当前尝试轮次（可选）。
        progress: 进度百分比 0~100（可选）。

    Raises:
        无（事件总线发布失败由调用栈向上传播，发布方保证在 DB 提交后）。
    """
    data: dict[str, Any] = {"task_id": task_id, "shot_id": shot_id, "status": status}
    if attempt is not None:
        data["attempt"] = attempt
    if progress is not None:
        data["progress"] = progress
    await event_bus.publish(project_id, "render.task.updated", data)


async def publish_batch_completed_if_done(session: AsyncSession, project_id: int, batch_key: str) -> bool:
    """检查批次是否全部终结，是则发布 ``render.batch.completed`` 事件。

    统计口径（按 ``input_params.batch_key``）：succeeded=成功数；
    failed=failed+manual_required+cancelled 数；cost=Σ actual_cost_credits。
    批内仍有 pending/queued/running/retrying/qc_review 任务时不发布。

    Args:
        session: 数据库会话（只读统计，不提交）。
        project_id: 项目 ID。
        batch_key: 批次键。

    Returns:
        True 表示已发布完成事件；False 表示批次未终结。
    """
    repo = RenderRepository(session)
    tasks = await repo.list_tasks_by_batch_key(project_id, batch_key)
    if not tasks:
        return False
    active = {"pending", "queued", "running", "retrying", "qc_review"}
    if any(t.status in active for t in tasks):
        return False
    succeeded = sum(1 for t in tasks if t.status == "succeeded")
    failed = sum(1 for t in tasks if t.status in ("failed", "manual_required", "cancelled"))
    cost = sum(int(t.actual_cost_credits or 0) for t in tasks)
    await event_bus.publish(
        project_id,
        "render.batch.completed",
        {"succeeded": succeeded, "failed": failed, "total": len(tasks), "cost": cost},
    )
    return True


async def estimate(session: AsyncSession, project: Project, shot_ids: list[int] | None) -> dict[str, Any]:
    """按镜头估算渲染成本（视频单价 = ceil(时长秒) × 模型单价）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        shot_ids: 镜头 ID 列表；空/None 表示全部锁定镜头。

    Returns:
        ``{per_shot: [{shot_id, estimated_credits}], total_credits,
        shot_count, available_credits}``。

    Raises:
        BizError: 60001 项目内无可用（锁定）镜头。
    """
    repo = RenderRepository(session)
    shots = (
        await repo.list_shots_by_ids(shot_ids)
        if shot_ids
        else await repo.list_renderable_shots(int(project.id))
    )
    # 允许已渲染/渲染失败镜头在首帧更新后重新提交（批量重出）
    eligible = [s for s in shots if s.status in ("locked", "rendered", "render_failed")]
    if not eligible:
        raise BizError(ErrorCode.STORYBOARD_NOT_LOCKED, "项目内没有可渲染的锁定镜头")

    per_shot: list[dict[str, Any]] = []
    total = 0
    for shot in eligible:
        task_type = resolve_task_type(shot)
        unit_price = await _video_unit_price(session, task_type)
        credits = compute_estimated_credits(int(shot.duration_ms), unit_price)
        per_shot.append({"shot_id": int(shot.id), "estimated_credits": credits})
        total += credits

    account = await BillingRepository(session).get_account_by_user(int(project.owner_user_id))
    available = int(account.balance_credits - account.frozen_credits) if account else 0
    budget = await _project_budget_usage(session, project)
    return {
        "per_shot": per_shot,
        "total_credits": total,
        "shot_count": len(eligible),
        "available_credits": available,
        # P2-12：成本护栏需按项目算力预算展示（用户余额不是提交上限）
        "budget_credits": budget["budget_credits"],
        "spent_credits": budget["spent_credits"],
        "frozen_credits": budget["frozen_credits"],
        "remaining_budget": budget["remaining_credits"],
    }


async def _project_budget_usage(
    session: AsyncSession,
    project: Project,
    extra_estimated: int = 0,
) -> dict[str, int]:
    """汇总项目算力预算占用：已结算 + 在途冻结 + 本次预估。

    口径：``committed = project.spent_credits + Σ(在途任务冻结额)``，
    其中在途状态取 :data:`_ACTIVE_STATES`（与渲染统计页保持一致）。
    项目预算 ``budget_credits=0`` 表示不限额。

    Args:
        session: 请求级数据库会话。
        project: 项目 ORM 实例（读取 budget_credits / spent_credits）。
        extra_estimated: 本次拟提交任务的预估积分（默认 0）。

    Returns:
        ``{budget_credits, spent_credits, frozen_credits, committed_credits,
        projected_credits, remaining_credits}``；``remaining_credits=-1``
        表示不限额。

    Raises:
        无（纯查询）。
    """
    from sqlalchemy import func as sa_func

    frozen = int(
        (
            await session.execute(
                select(sa_func.coalesce(sa_func.sum(RenderTask.frozen_credits), 0)).where(
                    RenderTask.project_id == int(project.id),
                    RenderTask.status.in_(tuple(_ACTIVE_STATES)),
                )
            )
        ).scalar_one()
        or 0
    )
    budget = int(project.budget_credits or 0)
    spent = int(project.spent_credits or 0)
    committed = spent + frozen
    projected = committed + max(int(extra_estimated), 0)
    return {
        "budget_credits": budget,
        "spent_credits": spent,
        "frozen_credits": frozen,
        "committed_credits": committed,
        "projected_credits": projected,
        "remaining_credits": -1 if budget <= 0 else max(budget - committed, 0),
    }


async def _load_voice_alignment(
    session: AsyncSession, shots: list[Shot]
) -> dict[int, dict[str, Any]]:
    """批量读取镜头绑定的最新配音片段（F5.13 音画对齐输入）。

    对每个镜头汇总其绑定的台词与已配音片段，计算台词总跨度（最后一个
    片段的 offset + 时长）。无绑定/无配音的镜头返回空上下文，不参与
    ``duration_mismatch`` 判定。

    Args:
        session: 数据库会话。
        shots: 待渲染镜头列表。

    Returns:
        镜头 ID → ``{voice_clip_ids, line_ids, line_duration_ms}``。
    """
    shot_ids = [int(shot.id) for shot in shots]
    if not shot_ids:
        return {}
    bindings = (
        (
            await session.execute(
                select(ShotLineBinding).where(ShotLineBinding.shot_id.in_(shot_ids))
            )
        )
        .scalars()
        .all()
    )
    bindings_by_shot: dict[int, list[ShotLineBinding]] = {}
    line_ids: set[int] = set()
    for binding in bindings:
        bindings_by_shot.setdefault(int(binding.shot_id), []).append(binding)
        line_ids.add(int(binding.line_id))
    latest_clip: dict[int, VoiceClip] = {}
    if line_ids:
        clips = (
            (
                await session.execute(
                    select(VoiceClip)
                    .where(
                        VoiceClip.line_id.in_(sorted(line_ids)),
                        VoiceClip.status == "voiced",
                    )
                    .order_by(VoiceClip.id.desc())
                )
            )
            .scalars()
            .all()
        )
        for clip in clips:
            latest_clip.setdefault(int(clip.line_id), clip)

    result: dict[int, dict[str, Any]] = {}
    for shot_id, items in bindings_by_shot.items():
        items.sort(key=lambda row: int(row.order_index))
        clip_ids: list[int] = []
        bound_line_ids: list[int] = []
        span_ms = 0
        for binding in items:
            bound_line_ids.append(int(binding.line_id))
            clip = latest_clip.get(int(binding.line_id))
            if clip is None:
                continue
            clip_ids.append(int(clip.id))
            span_ms = max(span_ms, int(binding.offset_ms or 0) + int(clip.duration_ms or 0))
        result[shot_id] = {
            "voice_clip_ids": clip_ids,
            "line_ids": bound_line_ids,
            "line_duration_ms": span_ms,
        }
    return result


async def _load_continuity_media_ids(
    session: AsyncSession, shots: list[Shot],
) -> dict[int, int]:
    """查询每个镜头「上一镜」当前最新版本的媒体 ID。

    尾帧续接会让视频内容依赖上一镜产物，须参与 input_hash；否则上一镜
    重绘后本镜仍会幂等复用旧视频，表现为「上镜变了、下镜没变」。

    Args:
        session: 数据库会话。
        shots: 本次待渲染镜头列表。

    Returns:
        ``{shot_id: 上一镜媒体ID}``；首镜或上一镜无版本时不出现在结果中。
    """
    if not shots:
        return {}
    by_board: dict[int, list[Shot]] = {}
    for shot in shots:
        by_board.setdefault(int(shot.storyboard_id), []).append(shot)
    result: dict[int, int] = {}
    for board_id, items in by_board.items():
        ordered = sorted(items, key=lambda row: int(row.order_index or 0))
        min_index = int(ordered[0].order_index or 0)
        cursor = (
            await session.execute(
                select(Shot)
                .where(
                    Shot.storyboard_id == board_id,
                    Shot.order_index < min_index,
                )
                .order_by(Shot.order_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        for shot in ordered:
            if cursor is not None:
                version = (
                    await session.execute(
                        select(ShotVersion)
                        .where(ShotVersion.shot_id == int(cursor.id))
                        .order_by(ShotVersion.version_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if version is not None and version.media_file_id:
                    result[int(shot.id)] = int(version.media_file_id)
            cursor = shot
    return result


async def _ensure_composed_first_frames(
    session: AsyncSession,
    shots: list[Shot],
) -> list[int]:
    """为缺少首帧的镜头补做整幅要素画面合成（渲染前置步骤）。

    背景（2026-11 修复）：``compose_shot_frame`` 此前只由分镜面板手动触发，
    渲染提交不调用，导致大多数镜头在 I2V 时只有单张资产图（角色/场景/道具
    三选一），画面与剧本描述结构性不符。

    约束：
    - 仅处理 ``first_frame_media_id`` 为空的镜头，已有首帧（人工/上次合成）
      一律复用，不覆盖；
    - 无场景基底且无角色资产的镜头跳过（合成只会得到空画布，不如单资产图）；
    - 单镜失败不阻断提交，记 warning 后由执行器回退单资产图。

    Args:
        session: 渲染提交的事务会话（合成产物随本事务一起提交/回滚）。
        shots: 待渲染镜头列表。

    Returns:
        成功补齐首帧的镜头 ID 列表。

    Raises:
        无：单镜异常被吞掉并记录日志。
    """
    from app.infra.storage import storage as default_storage
    from app.services import shot_frame_service

    composed: list[int] = []
    for shot in shots:
        if getattr(shot, "first_frame_media_id", None):
            continue
        if not getattr(shot, "scene_asset_id", None) and not (
            getattr(shot, "character_asset_ids", None) or []
        ):
            continue
        try:
            await shot_frame_service.compose_shot_frame(
                session, shot, storage=default_storage,
            )
            await session.flush()
            if getattr(shot, "first_frame_media_id", None):
                composed.append(int(shot.id))
        except Exception as exc:  # noqa: BLE001 - 首帧合成失败不阻断渲染提交
            logger.warning("渲染前首帧合成失败 shot_id=%s: %s", getattr(shot, "id", "?"), exc)
    return composed


async def _create_tasks_for_shots(
    session: AsyncSession,
    project: Project,
    shots: list[Shot],
    *,
    idem_key: str | None,
    batch_key: str,
) -> tuple[list[RenderTask], int, int]:
    """为镜头集合逐镜冻结并创建渲染任务（不提交，由调用方 commit）。

    每镜一条冻结（幂等键 ``{idem}:{shot_id}``；重建场景幂等命中复用原
    冻结）。总额预检失败（30001）时对本次新建的冻结逐一退款后重新抛出。

    Args:
        session: 数据库会话。
        project: 项目实例。
        shots: 待渲染镜头列表（须已锁定且有 final_prompt）。
        idem_key: 批次幂等键（regenerate 场景可为 None）。
        batch_key: 写入任务 input_params 的批次键。

    Returns:
        ``(任务列表, 冻结总额, 预估总额)`` 三元组。

    Raises:
        BizError: 30001 积分不足（data 携带 needed/available，先退款补偿）。
    """
    repo = RenderRepository(session)
    brepo = BillingRepository(session)

    # 0. 渲染前置：补齐整幅要素首帧（本地 0 成本合成）。
    # 必须放在预估之前——task_type 与 input_hash 都依赖 first_frame_media_id
    # （有首帧走 image_to_video，无首帧走 text_to_video）。
    composed_first_frames = await _ensure_composed_first_frames(session, shots)
    if composed_first_frames:
        logger.info(
            "渲染前首帧合成完成 project_id=%s shots=%s",
            int(project.id), composed_first_frames[:20],
        )

    # 1. 逐镜预估 + 路由单价（含 F5.13 音画对齐与 F3.13 锚点包）
    voice_alignment = await _load_voice_alignment(session, shots)
    anchor_packs = await anchor_service.build_anchor_packs(session, int(project.id), shots)
    continuity_media = await _load_continuity_media_ids(session, shots)
    plans: list[
        tuple[Shot, str, int, int, str, list[int], dict[str, Any], bool, dict[str, Any]]
    ] = []  # (shot, task_type, price, est, input_hash, anchor_ids, voice_ctx, mismatch, pack)
    estimated_total = 0
    for shot in shots:
        # P0 动态性：单镜时长必须落在 3–5 秒，超区间直接拒绝提交（避免
        # 生成端把 8s 长镜塞进 3s 时间线，或 1s 镜头只剩一个静止画面）。
        duration_value = int(shot.duration_ms)
        if not (
            motion_service.SHOT_DURATION_MIN_MS
            <= duration_value
            <= motion_service.SHOT_DURATION_MAX_MS
        ):
            raise BizError(
                ErrorCode.PARAM_INVALID,
                (
                    f"镜头 {shot.shot_no} 时长 {duration_value}ms 不在 3–5 秒区间，"
                    "请先在分镜中调整后重新提交"
                ),
            )
        task_type = resolve_task_type(shot)
        unit_price = await _video_unit_price(session, task_type)
        est = compute_estimated_credits(int(shot.duration_ms), unit_price)
        anchor_ids = [int(a) for a in (shot.asset_anchor_ids or []) if str(a).isdigit()]
        voice_ctx = voice_alignment.get(int(shot.id), {})
        clip_ids = list(voice_ctx.get("voice_clip_ids") or [])
        line_span = int(voice_ctx.get("line_duration_ms") or 0)
        # 镜头时长与台词总跨度偏差 >200ms 或 >10% 时标记音画不一致
        mismatch = bool(clip_ids) and abs(int(shot.duration_ms) - line_span) > max(
            200, int(line_span * 0.1)
        )
        pack = anchor_packs.get(int(shot.id), {})
        # 入参哈希使用「实际会发给模型」的三要素 prompt（与执行器构造逻辑
        # 一致），这样改了动作/运镜就会重新生成，而不是命中旧缓存。
        effective_prompt = motion_service.build_motion_prompt(
            shot, shot.final_prompt or ""
        )
        ih = build_input_hash(
            effective_prompt,
            int(shot.duration_ms),
            anchor_ids,
            clip_ids,
            str(pack.get("pack_hash") or ""),
            str(project.aspect_ratio or ""),
            int(shot.first_frame_media_id) if shot.first_frame_media_id else None,
            str(getattr(shot, "camera_move", None) or ""),
            continuity_media.get(int(shot.id)),
        )
        plans.append(
            (shot, task_type, unit_price, est, ih, anchor_ids, voice_ctx, mismatch, pack)
        )
        estimated_total += est

    # 2. 总额预检（读余额，不落冻结；30001 提前失败避免部分冻结残留）
    account = await brepo.get_account_by_user(int(project.owner_user_id))
    if account is None:
        raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
    available = int(account.balance_credits - account.frozen_credits)
    if available < estimated_total:
        raise BizError(
            ErrorCode.CREDIT_INSUFFICIENT,
            data={"needed": estimated_total, "available": available},
        )

    # 2.1 项目算力预算硬校验（P0-2 修复）
    # 现象：项目预算 500，批量提交冻结 1240 仍被受理，预算形同虚设。
    # 原因：此前只校验用户账户可用余额，没有校验 project.budget_credits。
    # 口径：已结算(spent) + 在途冻结(frozen) + 本次预估 ≤ 项目预算；
    #       budget_credits=0 表示不限额（跳过）。
    budget_usage = await _project_budget_usage(session, project, estimated_total)
    if budget_usage["budget_credits"] > 0 and (
        budget_usage["projected_credits"] > budget_usage["budget_credits"]
    ):
        raise BizError(
            ErrorCode.BUDGET_EXCEEDED,
            (
                f"超出项目算力预算（预算 {budget_usage['budget_credits']}，"
                f"已用 {budget_usage['committed_credits']}，"
                f"本次预估 {estimated_total}）"
            ),
            data={
                "budget_credits": budget_usage["budget_credits"],
                "spent_credits": budget_usage["spent_credits"],
                "frozen_credits": budget_usage["frozen_credits"],
                "committed_credits": budget_usage["committed_credits"],
                "estimated_credits": estimated_total,
                "remaining_budget": budget_usage["remaining_credits"],
            },
        )

    # 3. 逐镜冻结 + 建任务
    created_tasks: list[RenderTask] = []
    created_freezes: list[int] = []
    frozen_total = 0
    try:
        for shot, task_type, unit_price, est, ih, anchor_ids, voice_ctx, mismatch, pack in plans:
            fkey = batch_freeze_key(idem_key, int(shot.id)) if idem_key else (
                f"regen:{int(shot.id)}:{ih[:12]}:{new_request_id().lower()}"
            )
            freeze = await billing_service.find_freeze_by_idempotency_key(session, fkey)
            if freeze is None:
                freeze = await billing_service.precheck_and_freeze(
                    session,
                    account_id=int(account.id),
                    project_id=int(project.id),
                    task_type=task_type,
                    estimated_credits=est,
                    idempotency_key=fkey,
                )
                created_freezes.append(int(freeze.id))
            task = RenderTask(
                project_id=int(project.id),
                storyboard_id=int(shot.storyboard_id),
                shot_id=int(shot.id),
                task_type=task_type,
                status="queued",
                priority=resolve_priority(shot),
                model_key=None,
                retry_count=0,
                max_retries=2,
                estimated_cost_credits=est,
                actual_cost_credits=None,
                frozen_credits=int(freeze.frozen_credits),
                idempotency_key=(batch_task_key(idem_key, int(shot.id)) if idem_key else None),
                input_params={
                    "final_prompt": shot.final_prompt or "",
                    "negative_prompt": shot.negative_prompt,
                    "duration_ms": int(shot.duration_ms),
                    "anchor_ids": anchor_ids,
                    "voice_clip_ids": list(voice_ctx.get("voice_clip_ids") or []),
                    "voice_line_ids": list(voice_ctx.get("line_ids") or []),
                    "line_duration_ms": int(voice_ctx.get("line_duration_ms") or 0),
                    "duration_mismatch": mismatch,
                    "anchor_pack": {
                        "pack_hash": pack.get("pack_hash"),
                        "items": pack.get("items", []),
                        "missing": pack.get("missing", []),
                        "degradation": pack.get("degradation", {}),
                    },
                    "character_asset_ids": [
                        int(a) for a in (shot.character_asset_ids or []) if str(a).isdigit()
                    ],
                    "scene_asset_id": int(shot.scene_asset_id) if shot.scene_asset_id else None,
                    "freeze_id": int(freeze.id),
                    "batch_key": batch_key,
                    "unit_price_credits": unit_price,
                    "qc_round": 0,
                    # P1 修复：画幅与管线版本落库，便于审计与排查幂等复用
                    "aspect_ratio": str(project.aspect_ratio or ""),
                    "pipeline_version": RENDER_PIPELINE_VERSION,
                },
                input_hash=ih,
                # P2-10：音画对齐信息同时落独立列（此前只有 input_params 冗余）
                voice_clip_ids=list(voice_ctx.get("voice_clip_ids") or []),
                voice_line_ids=list(voice_ctx.get("line_ids") or []),
                line_duration_ms=int(voice_ctx.get("line_duration_ms") or 0),
                duration_mismatch=mismatch,
                queued_at=_utcnow(),
            )
            session.add(task)
            await session.flush()
            created_tasks.append(task)
            frozen_total += int(freeze.frozen_credits)
            # 镜头进入渲染队列
            shot.status = "render_queued"
    except BizError:
        # 兜底补偿：本次新建的冻结逐一全额释放（幂等命中的冻结属于历史任务，不动）
        for freeze_id in created_freezes:
            try:
                await billing_service.refund(session, freeze_id=freeze_id, reason="批量提交失败回滚")
            except BizError:  # noqa: PER203 已释放/不存在则跳过
                logger.warning("回滚退款失败 freeze_id=%s", freeze_id)
        raise
    return created_tasks, frozen_total, estimated_total


async def batch_submit(
    session: AsyncSession,
    project: Project,
    user_id: int,
    shot_ids: list[int] | None,
    idempotency_key_raw: str | None,
    strategy_tier: str = "balanced",
    max_credits: int = 0,
) -> dict[str, Any]:
    """批量提交渲染任务（202 异步模型 + 批次幂等）。

    流程：幂等回放（同 Idempotency-Key 返回首批任务）→ 镜头校验
    （未锁定/缺 final_prompt 跳过并记 warnings，全部不可用 60001）→
    逐镜冻结 + 建任务 + shot 置 render_queued → 提交执行器。

    V4.0：返回批次确认单字段（``route_summary / est_total_credits /
    est_total_seconds / per_batch``，AC-F5-01-2）并落 ``render_batches``
    行（策略档 / 插队额度 / 80% 预警防重发）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 提交人用户 ID。
        shot_ids: 镜头 ID 列表（空/None = 全部锁定镜头）。
        idempotency_key_raw: 请求头 Idempotency-Key 原始值（必填）。
        strategy_tier: 路由策略档 speed/balanced/quality（AC-F5-06-2）。
        max_credits: 本次生成上限（0 不限，AC-F5-01-2 S1）。

    Returns:
        ``{tasks, frozen_credits, estimated_credits, warnings, idempotent,
        batch_key, strategy_tier, per_batch, est_total_credits,
        est_total_seconds, route_summary, available_credits}``。

    Raises:
        BizError: 10001 缺少 Idempotency-Key；60001 无可用锁定镜头；
            30001 积分不足；60006 队列满。
    """
    from app.tasks.executor import render_executor  # 局部导入避免循环依赖

    idem_key = sanitize_idempotency_key(idempotency_key_raw)
    if not idem_key:
        raise BizError(ErrorCode.PARAM_INVALID, "缺少 Idempotency-Key 请求头")

    repo = RenderRepository(session)

    # 1. 批次幂等回放：同 Idempotency-Key 直接返回首批任务（不重复冻结）
    existing = await repo.list_tasks_by_idempotency_prefix(int(project.id), f"{idem_key}:")
    if existing:
        return {
            "tasks": [
                {"id": int(t.id), "shot_id": int(t.shot_id) if t.shot_id else None, "status": t.status}
                for t in existing
            ],
            "frozen_credits": sum(int(t.frozen_credits or 0) for t in existing),
            "estimated_credits": sum(int(t.estimated_cost_credits or 0) for t in existing),
            "warnings": [],
            "idempotent": True,
            "batch_key": idem_key,
            "strategy_tier": strategy_tier,
            "per_batch": PER_BATCH_SIZE,
            "est_total_credits": sum(int(t.estimated_cost_credits or 0) for t in existing),
            "est_total_seconds": sum(
                int(int_param((t.input_params or {}).get("duration_ms")) or 3000)
                for t in existing
            ) // 1000,
            "route_summary": {},
            "available_credits": None,
        }

    # 2. 解析镜头：显式列表按序；空列表 = 全部锁定镜头
    warnings: list[str] = []
    if shot_ids:
        found = {int(s.id): s for s in await repo.list_shots_by_ids(shot_ids)}
        ordered: list[Shot] = []
        for sid in shot_ids:
            shot = found.get(int(sid))
            if shot is None:
                warnings.append(f"镜头 {sid} 不存在，已跳过")
            elif int(shot.project_id) != int(project.id):
                warnings.append(f"镜头 {sid} 不属于当前项目，已跳过")
            else:
                ordered.append(shot)
        shots = ordered
    else:
        shots = await repo.list_renderable_shots(int(project.id))

    eligible = [
        s for s in shots
        if s.status in ("locked", "rendered", "render_failed") and (s.final_prompt or "").strip()
    ]
    eligible_ids = {int(s.id) for s in eligible}
    for shot in shots:
        if int(shot.id) not in eligible_ids:
            if shot.status not in ("locked", "rendered", "render_failed"):
                warnings.append(f"镜头 {shot.id} 状态为 {shot.status}（未锁定），已跳过")
            else:
                warnings.append(f"镜头 {shot.id} 缺少最终提示词，已跳过")
    if not eligible:
        raise BizError(ErrorCode.STORYBOARD_NOT_LOCKED, "没有可提交渲染的锁定镜头")

    # 3. 冻结 + 建任务（内部单事务语义；幂等键缺失时 10001 已在上游拦截）
    tasks, frozen_total, estimated_total = await _create_tasks_for_shots(
        session,
        project,
        eligible,
        idem_key=idem_key,
        batch_key=idem_key,
    )

    # V4.0：批次确认单 + 批次行（策略档 / 插队额度 / 80% 预警防重发）
    est_total_seconds = sum(int(s.duration_ms) for s in eligible) // 1000
    route_summary = await _build_route_summary(session)
    batch = (
        await session.execute(
            select(RenderBatch).where(
                RenderBatch.project_id == int(project.id),
                RenderBatch.batch_key == idem_key,
            )
        )
    ).scalar_one_or_none()
    if batch is None:
        batch = RenderBatch(
            project_id=int(project.id),
            batch_key=idem_key,
            strategy_tier=strategy_tier,
            shot_count=len(tasks),
            est_total_credits=estimated_total,
            est_total_seconds=est_total_seconds,
            route_summary=route_summary,
            status="running",
            created_by=user_id,
        )
        session.add(batch)
    else:
        batch.strategy_tier = strategy_tier
        batch.shot_count = len(tasks)
        batch.est_total_credits = estimated_total
        batch.est_total_seconds = est_total_seconds
        batch.route_summary = route_summary
        batch.status = "running"
    # 按策略档调整优先级权重（已锁模型优先，此处仅调默认批次序）
    if strategy_tier == "speed":
        for task in tasks:
            if task.priority > 5:
                task.priority = 5
    elif strategy_tier == "quality":
        for task in tasks:
            if task.priority < 5:
                task.priority = 5
    await session.commit()

    # 4. DB 提交后再入队执行器（崩溃安全：任务行已持久化）
    for task in tasks:
        await render_executor.submit(
            int(task.id), int(task.priority), int(project.owner_user_id)
        )

    available: int | None = None
    account = await BillingRepository(session).get_account_by_user(int(project.owner_user_id))
    if account is not None:
        available = int(account.balance_credits - account.frozen_credits)

    return {
        "tasks": [
            {"id": int(t.id), "shot_id": int(t.shot_id) if t.shot_id else None, "status": t.status}
            for t in tasks
        ],
        "frozen_credits": frozen_total,
        "estimated_credits": estimated_total,
        "warnings": warnings,
        "idempotent": False,
        "batch_key": idem_key,
        "strategy_tier": strategy_tier,
        "per_batch": PER_BATCH_SIZE,
        "est_total_credits": estimated_total,
        "est_total_seconds": est_total_seconds,
        "route_summary": route_summary,
        "available_credits": available,
        "max_credits": max_credits,
    }


async def _build_route_summary(session: AsyncSession) -> dict[str, Any]:
    """构造批次确认单的模型路由摘要（主力 + 备用）。

    Args:
        session: 请求级数据库会话。

    Returns:
        ``{primary, fallback, task_type}``；无路由配置时返回空字典。
    """
    from app.models.render import ModelRoute

    rows = (
        await session.execute(
            select(ModelRoute)
            .where(ModelRoute.task_type == "image_to_video", ModelRoute.is_active.is_(True))
            .order_by(ModelRoute.priority.asc())
            .limit(2)
        )
    ).scalars().all()
    if not rows:
        return {}
    return {
        "primary": rows[0].model_key,
        "fallback": rows[1].model_key if len(rows) > 1 else None,
        "task_type": "image_to_video",
    }


async def _get_task_in_project(
    session: AsyncSession, project: Project, task_id: int
) -> RenderTask:
    """加载渲染任务并校验归属项目。

    Args:
        session: 数据库会话。
        project: ACL 校验通过的项目实例。
        task_id: 任务 ID。

    Returns:
        RenderTask 实例。

    Raises:
        BizError: 60002 任务不存在或不属于该项目。
    """
    task = await RenderRepository(session).get_task(task_id)
    if task is None or int(task.project_id) != int(project.id):
        raise BizError(ErrorCode.TASK_NOT_FOUND)
    return task


async def cancel_task(session: AsyncSession, project: Project, task_id: int) -> dict[str, Any]:
    """取消渲染任务（仅 pending/queued 可取消）并全额退款。

    冻结状态为 frozen 时调用 :func:`billing_service.refund` 全额释放；
    已结算/已释放的冻结不再退款。任务置 cancelled，镜头回 locked，
    提交后发布 ``render.task.updated`` 并检查批次是否整体终结。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        task_id: 渲染任务 ID。

    Returns:
        ``{task_id, status, refunded_credits, shot_status}``。

    Raises:
        BizError: 60002 任务不存在；40001 当前状态不可取消
            （running → 「执行中不可取消」）。
    """
    task = await _get_task_in_project(session, project, task_id)
    if task.status in ("pending", "queued"):
        pass
    elif task.status == "running":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "执行中不可取消")
    else:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, f"任务状态 {task.status} 不可取消")

    refunded = 0
    freeze_id = int_param((task.input_params or {}).get("freeze_id"))
    if freeze_id is not None:
        freeze = await RenderRepository(session).get_freeze(int(freeze_id))
        if freeze is not None and freeze.status == "frozen":
            result = await billing_service.refund(session, freeze_id=freeze.id, reason="任务取消退款")
            refunded = int(result["released_credits"])

    task.status = "cancelled"
    task.failure_type = None
    task.failure_reason = "用户取消"
    task.finished_at = _utcnow()

    shot_status: str | None = None
    if task.shot_id is not None:
        shot = await RenderRepository(session).get_shot(int(task.shot_id))
        if shot is not None and shot.status in ("render_queued", "rendering"):
            shot.status = "locked"
            shot.locked_at = _utcnow()
            shot_status = shot.status

    batch_key = str_param((task.input_params or {}).get("batch_key")) or ""
    await session.commit()

    # 事件：先落库后发布
    await publish_task_event(int(project.id), int(task.id), int(task.shot_id) if task.shot_id else None, "cancelled")
    if batch_key:
        await publish_batch_completed_if_done(session, int(project.id), batch_key)

    return {
        "task_id": int(task.id),
        "status": task.status,
        "refunded_credits": refunded,
        "shot_status": shot_status,
    }


async def retry_task(session: AsyncSession, project: Project, task_id: int) -> dict[str, Any]:
    """重试渲染任务（failed/manual_required/cancelled → queued 重新入队）。

    冻结策略：原冻结仍为 frozen → 复用不重复扣款；已结算/已释放/过期 →
    按原预估额重新预检冻结（幂等键 ``{原键}:r{尝试}``，30001 带余额）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        task_id: 渲染任务 ID。

    Returns:
        ``{task_id, status, retry_count, reused_freeze}``。

    Raises:
        BizError: 60002 任务不存在；40001 状态不允许重试；
            60004 重试次数超限；30001 重新冻结时积分不足。
    """
    from app.tasks.executor import render_executor  # 局部导入避免循环依赖

    task = await _get_task_in_project(session, project, task_id)
    if task.status not in ("failed", "manual_required", "cancelled"):
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, f"任务状态 {task.status} 不允许重试")
    # 门槛与 batch_retry 对齐（retry_count >= max_retries + 3）：执行器内部
    # 重试耗尽后，用户手动重试仍应有 3 次余量（模型侧临时不可用场景）。
    if int(task.retry_count) >= int(task.max_retries) + 3:
        raise BizError(ErrorCode.RETRY_LIMIT_EXCEEDED)

    params = dict(task.input_params or {})
    reused_freeze = False
    freeze_id = int_param(params.get("freeze_id"))
    if freeze_id is not None:
        freeze = await RenderRepository(session).get_freeze(int(freeze_id))
        if freeze is not None and freeze.status == "frozen":
            reused_freeze = True
    if not reused_freeze:
        account = await BillingRepository(session).get_account_by_user(int(project.owner_user_id))
        if account is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
        new_key = (
            f"{task.idempotency_key}:r{int(task.retry_count) + 1}"
            if task.idempotency_key
            else f"retry:{int(task.id)}:{new_request_id().lower()}"
        )
        freeze = await billing_service.precheck_and_freeze(
            session,
            account_id=int(account.id),
            project_id=int(project.id),
            task_type=task.task_type,
            estimated_credits=max(1, int(task.estimated_cost_credits or 0)),
            idempotency_key=new_key,
        )
        params["freeze_id"] = int(freeze.id)
        task.frozen_credits = int(freeze.frozen_credits)

    task.retry_count = int(task.retry_count) + 1
    task.status = "queued"
    task.failure_type = None
    task.failure_reason = None
    task.finished_at = None
    task.queued_at = _utcnow()
    task.input_params = params

    shot_status: str | None = None
    if task.shot_id is not None:
        shot = await RenderRepository(session).get_shot(int(task.shot_id))
        if shot is not None and shot.status not in ("render_queued", "rendering"):
            shot.status = "render_queued"
            shot_status = shot.status

    await session.commit()

    await render_executor.submit(int(task.id), int(task.priority), int(project.owner_user_id))
    await publish_task_event(
        int(project.id), int(task.id), int(task.shot_id) if task.shot_id else None, "queued",
        attempt=int(task.retry_count),
    )
    return {
        "task_id": int(task.id),
        "status": task.status,
        "retry_count": int(task.retry_count),
        "reused_freeze": reused_freeze,
    }


async def regenerate_shot(
    session: AsyncSession, project: Project, user_id: int, shot_id: int
) -> dict[str, Any]:
    """单镜重新生成（等价 batch 单镜；新任务、旧任务保留）。

    允许镜头处于 locked / rendered / render_failed / qc_failed /
    needs_manual 状态（进行中或草稿态拒绝）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 提交人用户 ID。
        shot_id: 镜头 ID。

    Returns:
        ``{tasks, frozen_credits, estimated_credits, warnings}``。

    Raises:
        BizError: 60002 镜头不存在；40001 镜头状态不允许重新生成；
            30001 积分不足。
    """
    repo = RenderRepository(session)
    shot = await repo.get_shot(shot_id)
    if shot is None or int(shot.project_id) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")
    allowed = {"locked", "rendered", "render_failed", "qc_failed", "needs_manual"}
    if shot.status not in allowed:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID, f"镜头状态 {shot.status} 不允许重新生成",
        )
    if not (shot.final_prompt or "").strip():
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "镜头缺少最终提示词")

    batch_key = f"{_REGEN_BATCH_PREFIX}-{new_request_id().lower()}"
    tasks, frozen_total, estimated_total = await _create_tasks_for_shots(
        session, project, [shot], idem_key=None, batch_key=batch_key,
    )
    await session.commit()

    from app.tasks.executor import render_executor  # 局部导入避免循环依赖

    for task in tasks:
        await render_executor.submit(
            int(task.id), int(task.priority), int(project.owner_user_id)
        )
    return {
        "tasks": [
            {"id": int(t.id), "shot_id": int(t.shot_id) if t.shot_id else None, "status": t.status}
            for t in tasks
        ],
        "frozen_credits": frozen_total,
        "estimated_credits": estimated_total,
        "warnings": [],
        "batch_key": batch_key,
    }


# ---- 查询类 ----


def _shot_summary(shot: Shot | None) -> dict[str, Any] | None:
    """构造镜头摘要（列表/详情内嵌）。

    Args:
        shot: 镜头实例（可空）。

    Returns:
        摘要字典；入参为空返回 None。
    """
    if shot is None:
        return None
    return {
        "id": int(shot.id),
        "shot_no": int(shot.shot_no),
        "order_index": int(shot.order_index),
        "duration_ms": int(shot.duration_ms),
        "status": shot.status,
        "final_prompt": shot.final_prompt,
    }


async def get_task_detail(session: AsyncSession, project: Project, task_id: int) -> dict[str, Any]:
    """查询渲染任务详情（含产物、QC 报告与冻结/结算状态）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 view+）。
        task_id: 渲染任务 ID。

    Returns:
        详情字典。

    Raises:
        BizError: 60002 任务不存在或不属于该项目。
    """
    repo = RenderRepository(session)
    task = await _get_task_in_project(session, project, task_id)
    outputs = await repo.list_outputs(int(task.id))
    # 产物播放修复：批量读取产物媒体文件，向详情输出 media_url / mime_type。
    # 历史实现只返回 media_file_id，前端无法拿到可播放地址，只能显示占位文案。
    media_map: dict[int, MediaFile] = {}
    media_ids = [int(o.media_file_id) for o in outputs if o.media_file_id]
    if media_ids:
        media_rows = (
            await session.execute(select(MediaFile).where(MediaFile.id.in_(media_ids)))
        ).scalars().all()
        media_map = {int(m.id): m for m in media_rows}
    shot = await repo.get_shot(int(task.shot_id)) if task.shot_id else None

    qc_reports: list[dict[str, Any]] = []
    if shot is not None:
        qc_reports = [
            {
                "id": int(q.id),
                "result": q.result,
                "consistency_score": float(q.consistency_score) if q.consistency_score is not None else None,
                "round_no": int(q.round_no),
                "detail": q.detail,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            for q in await repo.list_qc_reports_for_shot(int(shot.id))
            if q.created_at is None or task.created_at is None or q.created_at >= task.created_at
        ]

    freeze_info: dict[str, Any] | None = None
    freeze_id = int_param((task.input_params or {}).get("freeze_id"))
    if freeze_id:
        freeze = await repo.get_freeze(int(freeze_id))
        if freeze is not None:
            freeze_info = {
                "freeze_id": int(freeze.id),
                "status": freeze.status,
                "frozen_credits": int(freeze.frozen_credits),
                "settled_credits": int(freeze.settled_credits) if freeze.settled_credits is not None else None,
                "released_credits": (
                    int(freeze.frozen_credits) if freeze.status == "released" else None
                ),
                "expires_at": freeze.expires_at.isoformat() if freeze.expires_at else None,
            }

    # 产物序列化：附带媒体文件的可播放地址与类型，供前端直接渲染播放器
    output_items: list[dict[str, Any]] = []
    for output in outputs:
        media = media_map.get(int(output.media_file_id)) if output.media_file_id else None
        output_items.append(
            {
                "id": int(output.id),
                "output_type": output.output_type,
                "output_index": int(output.output_index),
                "media_file_id": int(output.media_file_id) if output.media_file_id else None,
                "qc_report_id": output.qc_report_id,
                "is_selected": bool(output.is_selected),
                "media_url": media.url if media else None,
                "mime_type": media.mime_type if media else None,
                "duration_ms": int(media.duration_ms) if media and media.duration_ms else None,
            }
        )

    return {
        "id": int(task.id),
        "project_id": int(task.project_id),
        "shot_id": int(task.shot_id) if task.shot_id else None,
        "task_type": task.task_type,
        "status": task.status,
        "priority": int(task.priority),
        "model_key": task.model_key,
        "retry_count": int(task.retry_count),
        "max_retries": int(task.max_retries),
        "estimated_cost_credits": int(task.estimated_cost_credits or 0),
        "actual_cost_credits": int(task.actual_cost_credits) if task.actual_cost_credits is not None else None,
        "frozen_credits": int(task.frozen_credits or 0),
        "input_hash": task.input_hash,
        # P2-10：音画对齐信息（独立列，供前端/导出按列读取）
        "voice_clip_ids": task.voice_clip_ids or [],
        "voice_line_ids": task.voice_line_ids or [],
        "line_duration_ms": int(task.line_duration_ms) if task.line_duration_ms is not None else None,
        "duration_mismatch": task.duration_mismatch,
        # P0-3：降级占位产物标记（true 时本任务实际成本为 0）
        "degraded": bool((task.input_params or {}).get("degraded")),
        "failure_type": task.failure_type,
        "failure_reason": task.failure_reason,
        "user_language_error": task.user_language_error
        or (
            message_mapping.render_failure_message(task.failure_type, task.failure_reason)
            if task.failure_type
            else None
        ),
        "failure_detail": (
            message_mapping.render_failure_detail(
                task.failure_type, task.failure_reason, task.failure_reason
            )
            if task.failure_type
            else None
        ),
        "manual_pass": bool(task.manual_pass),
        "manual_pass_reason": task.manual_pass_reason,
        "manual_pass_by": int(task.manual_pass_by) if task.manual_pass_by else None,
        "manual_pass_at": task.manual_pass_at.isoformat() if task.manual_pass_at else None,
        "priority_source": task.priority_source,
        "queued_at": task.queued_at.isoformat() if task.queued_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "shot": _shot_summary(shot),
        "outputs": output_items,
        "qc_reports": qc_reports,
        "freeze": freeze_info,
    }


async def list_tasks(
    session: AsyncSession,
    project: Project,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = "-created_at",
) -> dict[str, Any]:
    """分页查询项目渲染任务列表（含镜头摘要与成本/失败类型）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 view+）。
        status: 状态筛选（None 全部）。
        page: 页码（≥1）。
        page_size: 每页条数（1~100）。
        sort: 排序 ``-created_at`` / ``created_at``。

    Returns:
        ``{items, page, page_size, total}``。

    Raises:
        BizError: 10001 排序参数非法。
    """
    if sort not in ("-created_at", "created_at"):
        raise BizError(ErrorCode.PARAM_INVALID, "sort 仅支持 created_at / -created_at")
    if page < 1 or not 1 <= page_size <= 100:
        raise BizError(ErrorCode.PARAM_INVALID, "分页参数非法")
    repo = RenderRepository(session)
    tasks, total = await repo.list_tasks(
        int(project.id), status=status, page=page, page_size=page_size, sort=sort
    )
    shot_ids = [int(t.shot_id) for t in tasks if t.shot_id is not None]
    shots = {int(s.id): s for s in await repo.list_shots_by_ids(shot_ids)}
    items = [
        {
            "id": int(t.id),
            "shot_id": int(t.shot_id) if t.shot_id else None,
            "task_type": t.task_type,
            "status": t.status,
            "priority": int(t.priority),
            "retry_count": int(t.retry_count),
            "estimated_cost_credits": int(t.estimated_cost_credits or 0),
            "actual_cost_credits": (
                int(t.actual_cost_credits) if t.actual_cost_credits is not None else None
            ),
            "failure_type": t.failure_type,
            "failure_reason": t.failure_reason,
            "user_language_error": t.user_language_error
            or (
                message_mapping.render_failure_message(t.failure_type, t.failure_reason)
                if t.failure_type
                else None
            ),
            "retryable": message_mapping.is_retryable(t.failure_type),
            "manual_pass": bool(t.manual_pass),
            "priority_source": t.priority_source,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "shot": _shot_summary(shots.get(int(t.shot_id))) if t.shot_id else None,
        }
        for t in tasks
    ]
    return {"items": items, "page": page, "page_size": page_size, "total": total}


# =====================================================================
# V4.0 生成执行域增量：仪表盘 / 批量重试 / 人工合格 / 按集锁定 / 插队
# =====================================================================

#: 仪表盘六态分组（AC-F5-07-1）
DASHBOARD_STATES: dict[str, tuple[str, ...]] = {
    "queued": ("pending", "queued"),
    "running": ("running", "retrying"),
    "qc_review": ("qc_review",),
    "succeeded": ("succeeded",),
    "failed": ("failed", "moderation_rejected", "abandoned"),
    "manual_required": ("manual_required",),
}

#: 生成中的活跃态（ETA 估算基数）
_ACTIVE_STATES: frozenset[str] = frozenset({"pending", "queued", "running", "retrying", "qc_review"})

#: ETA 估算滑动窗口（分钟，AC-F5-07-1【假设待验证：近 10 分钟滚动估算】）
ETA_WINDOW_MINUTES: int = 10

#: 速率波动阈值（> 50% 标注「波动较大」，AC-F5-07-2）
ETA_VOLATILITY_THRESHOLD: float = 0.50


async def get_dashboard(session: AsyncSession, project: Project) -> dict[str, Any]:
    """生成总览仪表盘：六态计数 + 总进度 + ETA + 实时消耗（AC-F5-07-1~2）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 view+）。

    Returns:
        ``{counts, total, progress_percent, eta_seconds, eta_volatile,
        consumed_credits, available_credits, rate_per_minute}``。
    """
    from datetime import timedelta

    from sqlalchemy import func as sa_func

    project_id = int(project.id)
    counts: dict[str, int] = {key: 0 for key in DASHBOARD_STATES}
    counts["cancelled"] = 0

    rows = (
        await session.execute(
            select(RenderTask.status, sa_func.count(RenderTask.id))
            .where(RenderTask.project_id == project_id)
            .group_by(RenderTask.status)
        )
    ).all()
    status_counts = {str(status): int(total) for status, total in rows}
    for group, states in DASHBOARD_STATES.items():
        counts[group] = sum(status_counts.get(state, 0) for state in states)
    counts["cancelled"] = status_counts.get("cancelled", 0)
    total = sum(status_counts.values())

    done = counts["succeeded"] + counts["failed"] + counts["manual_required"] + counts["cancelled"]
    progress = int(round(done / total * 100)) if total else 0

    # ETA：近 10 分钟完成速率滚动估算；速率不稳定（波动 > 50%）时标注波动
    window_start = _utcnow() - timedelta(minutes=ETA_WINDOW_MINUTES)
    finished_rows = (
        await session.execute(
            select(RenderTask.finished_at)
            .where(
                RenderTask.project_id == project_id,
                RenderTask.finished_at.is_not(None),
                RenderTask.finished_at >= window_start,
            )
            .order_by(RenderTask.finished_at.asc())
        )
    ).scalars().all()
    recent_done = len(finished_rows)
    rate_per_minute = recent_done / ETA_WINDOW_MINUTES
    remaining = counts["queued"] + counts["running"] + counts["qc_review"]
    eta_seconds: int | None = None
    eta_volatile = False
    if remaining and rate_per_minute > 0:
        eta_seconds = int(remaining / rate_per_minute * 60)
        # 波动检测：比较前半窗与后半窗的完成速率
        if len(finished_rows) >= 4:
            mid = len(finished_rows) // 2
            span = (ETA_WINDOW_MINUTES * 60) / 2
            early = mid / span
            late = (len(finished_rows) - mid) / span
            base = max(early, late, 1e-6)
            eta_volatile = abs(late - early) / base > ETA_VOLATILITY_THRESHOLD

    consumed = int(
        (
            await session.execute(
                select(sa_func.coalesce(sa_func.sum(RenderTask.actual_cost_credits), 0)).where(
                    RenderTask.project_id == project_id
                )
            )
        ).scalar_one()
        or 0
    )
    frozen = int(
        (
            await session.execute(
                select(sa_func.coalesce(sa_func.sum(RenderTask.frozen_credits), 0)).where(
                    RenderTask.project_id == project_id,
                    RenderTask.status.in_(tuple(_ACTIVE_STATES)),
                )
            )
        ).scalar_one()
        or 0
    )

    available = 0
    if project.owner_user_id:
        account = await BillingRepository(session).get_account_by_user(int(project.owner_user_id))
        if account is not None:
            available = int(account.balance_credits - account.frozen_credits)

    return {
        "counts": counts,
        "total": total,
        "progress_percent": min(100, max(0, progress)),
        "eta_seconds": eta_seconds,
        "eta_volatile": eta_volatile,
        "eta_hint": (
            "波动较大，仅供参考" if eta_volatile else ("可估算" if eta_seconds is not None else "暂无法估算")
        ),
        "consumed_credits": consumed,
        "frozen_credits": frozen,
        "available_credits": available,
        "rate_per_minute": round(rate_per_minute, 2),
        "window_minutes": ETA_WINDOW_MINUTES,
    }


async def manual_pass_task(
    session: AsyncSession,
    project: Project,
    task_id: int,
    *,
    reason: str,
    acknowledged: bool,
    user_id: int,
) -> dict[str, Any]:
    """人工标记合格，强制通过（AC-F5-09-1~3）。

    前置：任务处于 ``manual_required`` / ``failed`` 且质检不合格；必须勾选
    确认声明并填写放行理由（≥5 字）。通过后进入正常结算，原始质检得分保留，
    操作写 ``audit_logs``；同一镜头仅一次强制通过（AC-F5-09-2）；统计口径
    将该镜头标记 ``manual_pass`` 并从 M4 分母排除（AC-F5-09-3）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        task_id: 渲染任务 ID。
        reason: 放行理由。
        acknowledged: 是否已勾选确认声明。
        user_id: 操作人用户 ID。

    Returns:
        ``{task_id, status, manual_pass, settled_credits, shot_status}``。

    Raises:
        BizError: 10001 未勾选确认/理由不足；40001 状态不允许或已强制通过；
            60002 任务不存在。
    """
    from app.models.iam import AuditLog

    task = await _get_task_in_project(session, project, task_id)
    if not acknowledged:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "必须勾选确认声明后方可强制通过",
            error_detail="manual_pass_ack_required",
        )
    if len(reason.strip()) < 5:
        raise BizError(
            ErrorCode.PARAM_INVALID, "放行理由不少于 5 字", error_detail="manual_pass_reason_too_short"
        )
    if task.manual_pass:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "该镜头已强制通过，同一镜头仅一次",
            error_detail="manual_pass_already_used",
        )
    if task.status not in ("manual_required", "failed", "moderation_rejected"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"任务状态 {task.status} 不允许人工标记合格",
            data={"status": task.status},
        )

    settled = 0
    freeze_id = int_param((task.input_params or {}).get("freeze_id"))
    if freeze_id is not None:
        freeze = await RenderRepository(session).get_freeze(int(freeze_id))
        if freeze is not None and freeze.status == "frozen":
            detail = await billing_service.settle(
                session,
                freeze_id=int(freeze.id),
                actual_credits=max(1, int(task.estimated_cost_credits or 1)),
            )
            settled = int(detail.get("actual_credits", 0))

    task.status = "succeeded"
    task.manual_pass = True
    task.manual_pass_reason = reason.strip()[:100]
    task.manual_pass_by = user_id
    task.manual_pass_at = _utcnow()
    task.actual_cost_credits = settled or int(task.estimated_cost_credits or 0)
    task.finished_at = _utcnow()

    shot_status: str | None = None
    if task.shot_id:
        shot = await RenderRepository(session).get_shot(int(task.shot_id))
        if shot is not None:
            shot.status = "rendered"
            shot_status = shot.status

    session.add(
        AuditLog(
            actor_type="user",
            actor_user_id=user_id,
            project_id=int(project.id),
            action="render_manual_pass",
            target_type="render_task",
            target_id=int(task.id),
            detail={
                "reason": task.manual_pass_reason,
                "shot_id": int(task.shot_id) if task.shot_id else None,
                "settled_credits": settled,
            },
        )
    )
    await session.commit()
    await publish_task_event(
        int(task.project_id),
        int(task.id),
        int(task.shot_id) if task.shot_id else None,
        task.status,
    )
    return {
        "task_id": int(task.id),
        "status": task.status,
        "manual_pass": True,
        "manual_pass_reason": task.manual_pass_reason,
        "settled_credits": settled,
        "shot_status": shot_status,
    }


async def abandon_task(
    session: AsyncSession, project: Project, task_id: int
) -> dict[str, Any]:
    """放弃失败/待人工任务并全额解冻返还（AC-F5-04-3）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        task_id: 渲染任务 ID。

    Returns:
        ``{task_id, status, refunded_credits, shot_status}``。

    Raises:
        BizError: 40001 状态不允许；60002 任务不存在。
    """
    task = await _get_task_in_project(session, project, task_id)
    if task.status not in ("failed", "manual_required", "moderation_rejected"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"任务状态 {task.status} 不允许放弃",
            data={"status": task.status},
        )
    refunded = 0
    freeze_id = int_param((task.input_params or {}).get("freeze_id"))
    if freeze_id is not None:
        freeze = await RenderRepository(session).get_freeze(int(freeze_id))
        if freeze is not None and freeze.status == "frozen":
            result = await billing_service.refund(
                session, freeze_id=int(freeze.id), reason="用户放弃任务退款"
            )
            refunded = int(result.get("released_credits", 0))
    task.status = "abandoned"
    task.finished_at = _utcnow()
    shot_status: str | None = None
    if task.shot_id:
        shot = await RenderRepository(session).get_shot(int(task.shot_id))
        if shot is not None:
            shot.status = "render_failed"
            shot_status = shot.status
    await session.commit()
    await publish_task_event(
        int(task.project_id),
        int(task.id),
        int(task.shot_id) if task.shot_id else None,
        task.status,
    )
    return {
        "task_id": int(task.id),
        "status": task.status,
        "refunded_credits": refunded,
        "shot_status": shot_status,
    }


async def batch_retry(
    session: AsyncSession,
    project: Project,
    *,
    task_ids: list[int],
    delay_minutes: int,
    user_id: int,
) -> dict[str, Any]:
    """失败镜头批量重试（AC-F5-08-1~2）。

    统一以新幂等键建任务、按预估重新冻结；余额不足的镜头跳过并提示；
    ``moderation_rejected`` 类型不提供重试入口（GR-7）；``delay_minutes > 0``
    时登记延迟定时任务（到时自动批量重试，倒计时可取消）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        task_ids: 待重试任务 ID 列表。
        delay_minutes: 延迟分钟数（0 立即）。
        user_id: 操作人用户 ID。

    Returns:
        ``{accepted, skipped, delayed_task_ids, delay_minutes}``。

    Raises:
        BizError: 60002 任务不存在/不属于该项目。
    """
    accepted: list[int] = []
    skipped: list[dict[str, Any]] = []
    for task_id in task_ids:
        task = await RenderRepository(session).get_task(task_id)
        if task is None or int(task.project_id) != int(project.id):
            skipped.append({"task_id": task_id, "reason": "任务不存在或不属于该项目"})
            continue
        if task.failure_type == "moderation_rejected" or task.status == "moderation_rejected":
            skipped.append({
                "task_id": task_id,
                "reason": "内容审核未通过的任务不提供重试入口",
                "error_detail": "moderation_rejected",
            })
            continue
        if task.status not in ("failed", "manual_required"):
            skipped.append({
                "task_id": task_id,
                "reason": f"任务状态 {task.status} 不允许重试",
                "error_detail": "task_state_invalid",
            })
            continue
        if int(task.retry_count) >= int(task.max_retries) + 3:
            skipped.append({
                "task_id": task_id,
                "reason": "重试次数已达上限",
                "error_detail": "retry_limit_exceeded",
            })
            continue
        accepted.append(task_id)

    if delay_minutes > 0:
        from app.tasks.executor import render_executor

        render_executor.schedule_delayed_retry(
            project_id=int(project.id),
            task_ids=accepted,
            delay_minutes=delay_minutes,
            user_id=user_id,
        )
        return {
            "accepted": accepted,
            "skipped": skipped,
            "delayed_task_ids": accepted,
            "delay_minutes": delay_minutes,
            "message": f"已登记延迟 {delay_minutes} 分钟批量重试，可随时取消",
        }

    retried: list[int] = []
    for task_id in accepted:
        try:
            await retry_task(session, project, task_id)
            retried.append(task_id)
        except BizError as exc:
            skipped.append({
                "task_id": task_id,
                "reason": exc.message,
                "error_detail": exc.error_detail or f"code_{exc.code}",
            })
    return {
        "accepted": retried,
        "skipped": skipped,
        "delayed_task_ids": [],
        "delay_minutes": 0,
    }


async def lock_episode_versions(
    session: AsyncSession,
    project: Project,
    episode_id: int,
    *,
    selections: dict[str, int],
    user_id: int,
) -> dict[str, Any]:
    """按集一键锁定全部镜头版本（AC-F5-10-1~2）。

    默认锁定每镜头质检得分最高版本；``selections`` 可逐镜头改选。
    集内含待人工或失败镜头时拒绝并列出例外镜头。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        episode_id: 集 ID。
        selections: 镜头 ID → 版本号映射（可空）。
        user_id: 操作人用户 ID。

    Returns:
        ``{episode_id, locked: [{shot_id, version_no}], skipped, exceptions}``。

    Raises:
        BizError: 10002 集不存在；40001 存在例外镜头；60003 无可用版本。
    """
    from app.models.storyboard import ShotVersion

    repo = RenderRepository(session)
    shots = (
        await session.execute(
            select(Shot).where(
                Shot.episode_id == episode_id, Shot.project_id == int(project.id)
            ).order_by(Shot.order_index.asc())
        )
    ).scalars().all()
    if not shots:
        raise BizError(ErrorCode.NOT_FOUND, "该集尚无镜头")

    exceptions = [
        {
            "shot_id": int(s.id),
            "shot_no": int(s.shot_no),
            "status": s.status,
            "reason": (
                "待人工处理" if s.status == "needs_manual" else "生成失败"
            ),
        }
        for s in shots
        if s.status in ("needs_manual", "render_failed", "draft", "render_queued", "rendering")
    ]
    if exceptions:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "集内存在待人工或失败镜头，请先处理例外项",
            data={"exceptions": exceptions},
            error_detail="episode_lock_blocked",
        )

    locked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for shot in shots:
        versions = (
            await session.execute(
                select(ShotVersion)
                .where(ShotVersion.shot_id == int(shot.id))
                .order_by(ShotVersion.version_no.desc())
            )
        ).scalars().all()
        if not versions:
            # 尚无生成版本的镜头（如刚复制的镜头）跳过并说明，不阻断整集
            skipped.append(
                {
                    "shot_id": int(shot.id),
                    "shot_no": int(shot.shot_no),
                    "reason": "该镜头尚无生成版本，请先生成",
                }
            )
            continue
        wanted = selections.get(str(int(shot.id)))
        target = None
        if wanted is not None:
            target = next((v for v in versions if int(v.version_no) == wanted), None)
        if target is None:
            # 默认：质检得分最高版本（无 QC 时取最新版）
            scored: list[tuple[float, int, ShotVersion]] = []
            for version in versions:
                score = 0.0
                if version.qc_report_id:
                    qc = await repo.get_qc_report(int(version.qc_report_id))
                    if qc is not None and qc.consistency_score is not None:
                        score = float(qc.consistency_score)
                scored.append((score, int(version.version_no), version))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            target = scored[0][2]
        for version in versions:
            version.is_locked = int(version.id) == int(target.id)
            version.is_current = int(version.id) == int(target.id)
        shot.status = "locked"
        shot.locked = True
        shot.locked_at = _utcnow()
        locked.append({"shot_id": int(shot.id), "version_no": int(target.version_no)})
    await session.commit()
    logger.info(
        "按集锁定镜头版本 episode_id=%s locked=%s skipped=%s",
        episode_id, len(locked), len(skipped),
    )
    return {
        "episode_id": episode_id,
        "locked": locked,
        "skipped": skipped,
        "exceptions": [],
    }


async def jump_queue(
    session: AsyncSession,
    project: Project,
    *,
    batch_key: str,
    task_id: int,
    user_id: int,
) -> dict[str, Any]:
    """单镜头插队（AC-F5-01-3）：优先级升至当前批次队首。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（需 edit+）。
        batch_key: 批次键。
        task_id: 渲染任务 ID。
        user_id: 操作人用户 ID。

    Returns:
        ``{task_id, priority, priority_source, jump_used, free_quota}``。

    Raises:
        BizError: 60002 任务不存在；40001 已生成中不可插队；
            60004 本批次免费插队额度已用完。
    """
    from app.models.render import RenderBatch
    from app.services.template_packs import MAX_QUEUE_JUMPS

    task = await _get_task_in_project(session, project, task_id)
    if task.status == "running":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "已「生成中」的任务不可插队")
    if task.status not in ("pending", "queued", "retrying"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID, f"任务状态 {task.status} 不可插队"
        )

    batch = (
        await session.execute(
            select(RenderBatch).where(
                RenderBatch.project_id == int(project.id),
                RenderBatch.batch_key == batch_key,
            )
        )
    ).scalar_one_or_none()
    if batch is None:
        batch = RenderBatch(
            project_id=int(project.id),
            batch_key=batch_key,
            created_by=user_id,
        )
        session.add(batch)
        await session.flush()
    if int(batch.jump_used) >= MAX_QUEUE_JUMPS:
        raise BizError(
            ErrorCode.RETRY_LIMIT_EXCEEDED,
            f"本批次免费插队额度已用完（{MAX_QUEUE_JUMPS} 次）",
            data={"jump_used": int(batch.jump_used), "free_quota": MAX_QUEUE_JUMPS},
            error_detail="queue_jump_exhausted",
        )
    batch.jump_used = int(batch.jump_used) + 1
    task.priority = 1
    task.priority_source = "queue_jump"
    await session.commit()
    await publish_task_event(
        int(task.project_id),
        int(task.id),
        int(task.shot_id) if task.shot_id else None,
        task.status,
    )
    return {
        "task_id": int(task.id),
        "priority": int(task.priority),
        "priority_source": task.priority_source,
        "jump_used": int(batch.jump_used),
        "free_quota": MAX_QUEUE_JUMPS,
    }


async def label_shot_version(
    session: AsyncSession,
    *,
    version_id: int,
    label: str | None,
    note: str | None,
    user_id: int,
) -> dict[str, Any]:
    """给镜头版本打标签/备注（AC-F5-03-3）。

    Args:
        session: 请求级数据库会话。
        version_id: 镜头版本 ID。
        label: 预设标签（color_good/action_right/expression_natural/other）。
        note: 备注（≤100 字）。
        user_id: 操作人用户 ID（P1 团队审计）。

    Returns:
        ``{version_id, label, note}``。

    Raises:
        BizError: 10002 版本不存在。
    """
    from app.models.storyboard import ShotVersion

    version = await session.get(ShotVersion, version_id)
    if version is None:
        raise BizError(ErrorCode.NOT_FOUND, "镜头版本不存在")
    version.label = label
    version.note = (note or "")[:100] or None
    await session.commit()
    return {"version_id": int(version.id), "label": version.label, "note": version.note}

async def get_task_any_project(session: AsyncSession, task_id: int) -> RenderTask:
    """按 ID 加载渲染任务（不校验项目归属，供路由层反查项目做 ACL）。

    Args:
        session: 请求级数据库会话。
        task_id: 渲染任务 ID。

    Returns:
        RenderTask 实例。

    Raises:
        BizError: 60002 任务不存在。
    """
    task = await RenderRepository(session).get_task(task_id)
    if task is None:
        raise BizError(ErrorCode.TASK_NOT_FOUND)
    return task


async def get_episode_project_id(session: AsyncSession, episode_id: int) -> int:
    """按集 ID 反查项目 ID（供路由层做 ACL）。

    Args:
        session: 请求级数据库会话。
        episode_id: 集 ID。

    Returns:
        项目 ID。

    Raises:
        BizError: 10002 集不存在。
    """
    from app.models.script import Episode

    episode = await session.get(Episode, episode_id)
    if episode is None:
        raise BizError(ErrorCode.NOT_FOUND, "集不存在")
    return int(episode.project_id)
