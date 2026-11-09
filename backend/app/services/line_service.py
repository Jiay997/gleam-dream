"""F2.12 台词清单与 F4.13 镜头台词绑定服务。

能力：
- 台词清单查询/编辑/拆分/时长预估/批量重算；
- 镜头 ↔ 台词多对多绑定（含偏移与顺序），未绑定台词清单；
- 音频驱动时长：镜头时长 = 绑定台词实测时长（或预估）之和 + 气口，
  并对超过 15s 的超长句给出 ``long_line`` 提示；
- 集级时长报告：镜头总时长、台词覆盖率、未绑定台词统计。

所有时长写入均遵守 ``shots.duration_ms`` 的 1000~15000ms 约束；被手工锁定
（``duration_manual=True``）的镜头默认不参与自动重算。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models.script import Episode, Scene, ScriptLine
from app.models.storyboard import Shot
from app.models.v6 import ShotLineBinding
from app.services import voice_service

#: 镜头尾部留白（毫秒）
_SHOT_TAIL_MS = 300
#: 单镜头最短/最长时长（与 shots CHECK 约束一致）
_MIN_SHOT_MS = 1000
_MAX_SHOT_MS = 15000


async def _episode_or_404(session: AsyncSession, project_id: int, ep_no: int) -> Episode:
    """按项目与集号查询分集。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        分集 ORM 实例。

    Raises:
        BizError: 10002 集不存在。
    """
    episode = (
        await session.execute(
            select(Episode).where(
                Episode.project_id == project_id, Episode.episode_no == ep_no
            )
        )
    ).scalars().first()
    if episode is None:
        raise BizError(ErrorCode.NOT_FOUND, "集不存在")
    return episode


async def list_lines(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    scene_no: int | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """查询台词清单（含实测/预估时长与配音状态）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        scene_no: 场号过滤（可空）。
        include_deleted: 是否包含软删除台词。

    Returns:
        台词列表（按场次与行号排序）。
    """
    await _episode_or_404(session, project_id, ep_no)
    # 历史台词可能缺少稳定键与预估时长，查询前幂等补齐
    await voice_service.ensure_line_keys(session, project_id)
    stmt = (
        select(ScriptLine, Scene.scene_no)
        .join(Scene, Scene.id == ScriptLine.scene_id)
        .join(Episode, Episode.id == Scene.episode_id)
        .where(
            ScriptLine.project_id == project_id,
            Episode.episode_no == ep_no,
        )
        .order_by(Scene.scene_no, ScriptLine.order_index)
    )
    if scene_no is not None:
        stmt = stmt.where(Scene.scene_no == scene_no)
    if not include_deleted:
        stmt = stmt.where(ScriptLine.is_deleted.is_(False))

    rows = (await session.execute(stmt)).all()
    items: list[dict[str, Any]] = []
    for line, current_scene_no in rows:
        items.append(
            {
                "id": line.id,
                "line_key": line.line_key,
                "scene_no": current_scene_no,
                "line_no": line.line_no,
                "order_index": line.order_index,
                "line_type": line.line_type,
                "speaker_type": line.speaker_type,
                "speaker_name": line.speaker_name,
                "character_asset_id": line.character_asset_id,
                "content": line.content,
                "emotion_tag": line.emotion_tag,
                "est_ms": line.est_ms or voice_service.estimate_ms(line.content),
                "actual_ms": line.actual_ms,
                "voice_status": line.voice_status,
                "source": line.source,
                "is_deleted": line.is_deleted,
            }
        )
    return items


async def update_line(
    session: AsyncSession,
    project_id: int,
    line_id: int,
    content: str | None,
    speaker_name: str | None,
    character_asset_id: int | None,
    emotion_tag: str | None,
) -> dict[str, Any]:
    """编辑台词并重算预估时长（文本变化时）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        line_id: 台词 ID。
        content: 新文本。
        speaker_name: 说话人。
        character_asset_id: 角色资产 ID。
        emotion_tag: 情绪标签。

    Returns:
        更新后的台词字典。

    Raises:
        BizError: 10002 台词不存在。
    """
    line = await session.get(ScriptLine, line_id)
    if line is None or line.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "台词不存在")
    if content is not None and content != line.content:
        line.content = content
        line.source = "edit"
        line.est_ms = voice_service.estimate_ms(content)
        # 文本变化 → 原配音失效，标记 dirty（下次生成时重配）
        line.voice_status = "dirty" if line.actual_ms else "pending"
    if speaker_name is not None:
        line.speaker_name = speaker_name
    if character_asset_id is not None:
        line.character_asset_id = character_asset_id
    if emotion_tag is not None:
        line.emotion_tag = emotion_tag
    await session.commit()
    return {
        "id": line.id,
        "content": line.content,
        "speaker_name": line.speaker_name,
        "character_asset_id": line.character_asset_id,
        "emotion_tag": line.emotion_tag,
        "est_ms": line.est_ms,
        "actual_ms": line.actual_ms,
        "voice_status": line.voice_status,
        "source": line.source,
    }


def _suggest_split_position(text: str) -> int:
    """在句读标点处建议拆分位置（修复测试报告 #22 的字符中点切断问题）。

    优先选择离文本中点最近的句读标点（标点之后切分，保证前半段以标点收尾）；
    全文无句读标点时回退为字符中点。

    Args:
        text: 原始台词文本。

    Returns:
        建议的拆分位置（切分点字符索引，1 <= position < len(text)）。

    Raises:
        无。
    """
    mid = max(1, len(text) // 2)
    sentence_puncts = "。！？；!?;…"
    candidates = [i + 1 for i, ch in enumerate(text) if ch in sentence_puncts]
    if not candidates:
        return mid
    best = min(candidates, key=lambda p: abs(p - mid))
    return best if best < len(text) else mid


async def _shift_lines_after(
    session: AsyncSession,
    scene_id: int,
    column: ColumnElement[int],
    after_value: int,
) -> None:
    """把同场景内指定列大于 after_value 的行整体后移一位（两段式大偏移，规避唯一约束）。

    2026-11 修复：本函数此前声明为同步函数却调用 ``session.execute``（返回
    协程且未 await），导致两条 UPDATE 实际从未执行；调用方 ``await
    _shift_lines_after(...)`` 又对 None 取 await，直接抛
    ``TypeError: object NoneType can't be used in 'await' expression``，
    台词拆分接口 500。现改为真正的异步执行，两条 UPDATE 顺序发出。

    Args:
        session: 数据库会话（由调用方提交）。
        scene_id: 场景 ID。
        column: 需要顺延的列（line_no 或 order_index）。
        after_value: 位移基准值（严格大于该值的行 +1）。

    Returns:
        无（执行两条 UPDATE，不提交）。

    Raises:
        无：SQL 执行异常向上传播给调用方事务。
    """
    await session.execute(
        update(ScriptLine)
        .where(ScriptLine.scene_id == scene_id, column > after_value)
        .values({column.key: column + 10000})
    )
    await session.execute(
        update(ScriptLine)
        .where(ScriptLine.scene_id == scene_id, column > after_value + 10000)
        .values({column.key: column - 9999})
    )


async def split_line(
    session: AsyncSession,
    project_id: int,
    line_id: int,
    position: int | None,
    content: str | None,
) -> dict[str, Any]:
    """拆分超长台词：保留前半段，插入后半段并顺延后续行号。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        line_id: 待拆分台词 ID。
        position: 拆分的字符位置。
        content: 自定义后半段文本（空则按位置切分）。

    Returns:
        原台词与新增台词简要信息。

    Raises:
        BizError: 10002 台词不存在；10001 拆分位置非法。
    """
    line = await session.get(ScriptLine, line_id)
    if line is None or line.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "台词不存在")

    original = line.content
    if content is None:
        if position is None:
            # 修复（测试报告 #22）：前端不再硬编码字符中点切分——未指定位置时
            # 由后端在句读标点处智能切分，避免把词语/句子拦腰截断。
            position = _suggest_split_position(original)
        if position <= 0 or position >= len(original):
            raise BizError(ErrorCode.PARAM_INVALID, "拆分位置必须在文本内部")
        first, second = original[:position].strip(), original[position:].strip()
    else:
        second = content.strip()
        first = original[: position] if position else original
        if not first.strip():
            raise BizError(ErrorCode.PARAM_INVALID, "拆分后的前半段不能为空")

    if not second:
        raise BizError(ErrorCode.PARAM_INVALID, "拆分后的后半段不能为空")

    # 唯一约束 (scene_id, line_no) 立即生效：先用大偏移避免逐行冲突。
    # 修复（测试报告 #23）：order_index 与 line_no 同步顺延——此前仅顺延
    # line_no，新行 order_index+1 与后续行重复，按 order_index 排序的
    # 列表会出现拆分后序号错乱。
    await _shift_lines_after(session, line.scene_id, ScriptLine.line_no, line.line_no)
    await _shift_lines_after(session, line.scene_id, ScriptLine.order_index, line.order_index)

    line.content = first
    line.est_ms = voice_service.estimate_ms(first)
    line.voice_status = "dirty"
    new_line = ScriptLine(
        scene_id=line.scene_id,
        project_id=line.project_id,
        line_no=line.line_no + 1,
        line_type=line.line_type,
        character_asset_id=line.character_asset_id,
        speaker_name=line.speaker_name,
        content=second,
        emotion_tag=line.emotion_tag,
        order_index=line.order_index + 1,
        speaker_type=line.speaker_type,
        est_ms=voice_service.estimate_ms(second),
        source="split",
    )
    session.add(new_line)
    await session.flush()
    new_line.line_key = f"ln_{new_line.id:010d}"
    await session.commit()
    return {
        "source": {"id": line.id, "content": line.content, "est_ms": line.est_ms},
        "created": {"id": new_line.id, "line_no": new_line.line_no, "content": new_line.content},
    }


async def estimate(
    session: AsyncSession,
    project_id: int,
    line_id: int,
    content: str | None,
    rate: float,
) -> dict[str, Any]:
    """预估单句时长（可传入未保存文本做试算）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        line_id: 台词 ID。
        content: 试算文本（空则用已存文本）。
        rate: 语速倍率。

    Returns:
        含 ``est_ms`` 与是否回写标志的字典。

    Raises:
        BizError: 10002 台词不存在。
    """
    line = await session.get(ScriptLine, line_id)
    if line is None or line.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "台词不存在")
    text = content if content is not None else line.content
    est = voice_service.estimate_ms(text, rate)
    if content is None:
        line.est_ms = est
        await session.commit()
    return {"line_id": line_id, "est_ms": est, "rate": rate, "persisted": content is None}


async def get_shot_bindings(
    session: AsyncSession, project_id: int, shot_id: int
) -> dict[str, Any]:
    """查询镜头绑定的台词与未绑定台词清单。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        shot_id: 镜头 ID。

    Returns:
        含绑定项、镜头时长与同场未绑定台词的字典。

    Raises:
        BizError: 10002 镜头不存在。
    """
    shot = await session.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")
    rows = (
        await session.execute(
            select(ShotLineBinding, ScriptLine)
            .join(ScriptLine, ScriptLine.id == ShotLineBinding.line_id)
            .where(ShotLineBinding.shot_id == shot_id)
            .order_by(ShotLineBinding.order_index, ShotLineBinding.id)
        )
    ).all()
    bound_ids = {line.id for _, line in rows}
    candidates: list[dict[str, Any]] = []
    if shot.scene_id is not None:
        scene_lines = (
            await session.execute(
                select(ScriptLine)
                .where(
                    ScriptLine.scene_id == shot.scene_id,
                    ScriptLine.is_deleted.is_(False),
                    ScriptLine.line_type.in_(("dialogue", "narration")),
                )
                .order_by(ScriptLine.order_index)
            )
        ).scalars().all()
        candidates = [
            {
                "line_id": line.id,
                "content": line.content,
                "speaker_name": line.speaker_name,
                "est_ms": line.est_ms,
                "actual_ms": line.actual_ms,
                "voice_status": line.voice_status,
            }
            for line in scene_lines
            if line.id not in bound_ids
        ]
    return {
        "shot_id": shot.id,
        "duration_ms": shot.duration_ms,
        "duration_source": shot.duration_source,
        "duration_manual": shot.duration_manual,
        "line_coverage_pct": float(shot.line_coverage_pct),
        "long_line": shot.long_line,
        "bindings": [
            {
                "binding_id": binding.id,
                "line_id": line.id,
                "content": line.content,
                "speaker_name": line.speaker_name,
                "offset_ms": binding.offset_ms,
                "order_index": binding.order_index,
                "actual_ms": line.actual_ms,
                "est_ms": line.est_ms,
                "voice_status": line.voice_status,
            }
            for binding, line in rows
        ],
        "candidates": candidates,
    }


async def replace_shot_bindings(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    shot_id: int,
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    """全量重建镜头台词绑定并重算时长。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        shot_id: 镜头 ID。
        bindings: 绑定项列表（line_id/offset_ms/order_index）。

    Returns:
        更新后的绑定与镜头信息。

    Raises:
        BizError: 10002 镜头/台词不存在；10001 台词不属于本项目。
    """
    shot = await session.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")

    line_ids = [int(item["line_id"]) for item in bindings]
    if line_ids:
        lines = (
            await session.execute(
                select(ScriptLine).where(
                    ScriptLine.id.in_(line_ids), ScriptLine.project_id == project_id
                )
            )
        ).scalars().all()
        found = {line.id for line in lines}
        missing = [lid for lid in line_ids if lid not in found]
        if missing:
            raise BizError(ErrorCode.PARAM_INVALID, f"台词不存在或不属于本项目: {missing}")

    await session.execute(delete_bindings_stmt(shot_id))
    for index, item in enumerate(sorted(bindings, key=lambda x: int(x.get("order_index", 0)))):
        session.add(
            ShotLineBinding(
                project_id=project_id,
                shot_id=shot_id,
                line_id=int(item["line_id"]),
                offset_ms=int(item.get("offset_ms", 0)),
                order_index=int(item.get("order_index", index)),
                created_by=int(user_id) if str(user_id).isdigit() else None,
            )
        )
    await session.flush()
    await recalculate_shot_duration(session, project_id, shot_id, force_manual=False, commit=False)
    await session.commit()
    return await get_shot_bindings(session, project_id, shot_id)


def delete_bindings_stmt(shot_id: int):  # noqa: ANN201 - 简单 SQL 构造器
    """构造删除镜头全部绑定的语句。

    Args:
        shot_id: 镜头 ID。

    Returns:
        SQLAlchemy Delete 语句。
    """
    from sqlalchemy import delete

    return delete(ShotLineBinding).where(ShotLineBinding.shot_id == shot_id)


async def recalculate_shot_duration(
    session: AsyncSession,
    project_id: int,
    shot_id: int,
    force_manual: bool,
    commit: bool = True,
) -> bool:
    """按绑定台词音频时长重算镜头时长。

    规则：
    - 有实测用实测，否则用预估；
    - 基础时长 = Σ(台词时长 + 偏移差) + 尾部留白；
    - 超过 ``_MAX_SHOT_MS`` 钳制并置 ``long_line``；
    - 低于 1000ms 钳制到 1000ms；
    - 手工锁定且未 force 时不修改。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        shot_id: 镜头 ID。
        force_manual: 是否覆盖手工锁定。
        commit: 是否提交事务（漂移处理时为 False）。

    Returns:
        是否发生变更。

    Raises:
        BizError: 10002 镜头不存在。
    """
    shot = await session.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")
    if shot.duration_manual and not force_manual:
        return False

    rows = (
        await session.execute(
            select(ShotLineBinding, ScriptLine)
            .join(ScriptLine, ScriptLine.id == ShotLineBinding.line_id)
            .where(ShotLineBinding.shot_id == shot_id)
            .order_by(ShotLineBinding.order_index)
        )
    ).all()
    if not rows:
        return False

    total = _SHOT_TAIL_MS
    last_offset = 0
    for binding, line in rows:
        duration = line.actual_ms or line.est_ms or voice_service.estimate_ms(line.content)
        total += duration + max(0, binding.offset_ms - last_offset)
        last_offset = binding.offset_ms

    long_line = total > _MAX_SHOT_MS
    target = min(_MAX_SHOT_MS, max(_MIN_SHOT_MS, total))
    changed = target != shot.duration_ms or shot.duration_source != "audio"
    shot.duration_ms = target
    shot.duration_source = "audio"
    shot.long_line = long_line
    shot.duration_drift_ms = total - target
    coverage = sum(1 for _ in rows)
    scene_total = (
        await session.execute(
            select(ScriptLine).where(
                ScriptLine.scene_id == shot.scene_id,
                ScriptLine.is_deleted.is_(False),
                ScriptLine.line_type.in_(("dialogue", "narration")),
            )
        )
    ).scalars().all()
    shot.line_coverage_pct = round(min(100.0, coverage / max(1, len(scene_total)) * 100), 2)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return changed


async def recalc_episode(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    force_manual: bool,
    max_shot_ms: int,
) -> dict[str, Any]:
    """批量重算某集所有有绑定镜头的时长。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        force_manual: 是否覆盖手工锁定。
        max_shot_ms: 单镜头上限。

    Returns:
        含变更镜头清单、超长镜头与集总时长的字典。
    """
    episode = await _episode_or_404(session, project_id, ep_no)
    shots = (
        await session.execute(
            select(Shot)
            .where(Shot.project_id == project_id, Shot.episode_id == episode.id)
            .order_by(Shot.order_index)
        )
    ).scalars().all()
    changed: list[int] = []
    long_lines: list[int] = []
    for shot in shots:
        if await recalculate_shot_duration(
            session, project_id, shot.id, force_manual=force_manual, commit=False
        ):
            changed.append(shot.id)
        if shot.duration_ms >= max_shot_ms:
            long_lines.append(shot.id)
    await session.commit()
    report = await episode_duration_report(session, project_id, ep_no)
    return {
        "ep_no": ep_no,
        "changed_shot_ids": changed,
        "long_line_shot_ids": long_lines,
        "report": report,
    }


async def episode_duration_report(
    session: AsyncSession, project_id: int, ep_no: int
) -> dict[str, Any]:
    """生成集级时长/覆盖率报告。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        含镜头数、总时长、目标时长差、覆盖率与未绑定台词的字典。
    """
    episode = await _episode_or_404(session, project_id, ep_no)
    shots = (
        await session.execute(
            select(Shot)
            .where(Shot.project_id == project_id, Shot.episode_id == episode.id)
            .order_by(Shot.order_index)
        )
    ).scalars().all()
    shot_ids = [shot.id for shot in shots]
    bindings = []
    if shot_ids:
        bindings = (
            await session.execute(
                select(ShotLineBinding).where(ShotLineBinding.shot_id.in_(shot_ids))
            )
        ).scalars().all()
    bound_line_ids = {b.line_id for b in bindings}

    lines = (
        await session.execute(
            select(ScriptLine)
            .join(Scene, Scene.id == ScriptLine.scene_id)
            .where(
                Scene.episode_id == episode.id,
                ScriptLine.is_deleted.is_(False),
                ScriptLine.line_type.in_(("dialogue", "narration")),
            )
        )
    ).scalars().all()
    unbound = [
        {"line_id": line.id, "content": line.content, "speaker_name": line.speaker_name}
        for line in lines
        if line.id not in bound_line_ids
    ]

    total_ms = sum(shot.duration_ms for shot in shots)
    target_sec = episode.target_duration_seconds or episode.est_duration_sec or 0
    coverage = len(bound_line_ids) / max(1, len(lines)) * 100
    return {
        "ep_no": ep_no,
        "shot_count": len(shots),
        "total_ms": total_ms,
        "total_seconds": round(total_ms / 1000, 2),
        "target_seconds": target_sec,
        "diff_seconds": round(total_ms / 1000 - target_sec, 2),
        "line_count": len(lines),
        "bound_line_count": len(bound_line_ids),
        "unbound_lines": unbound,
        "line_coverage_pct": round(coverage, 2),
        "long_line_shots": [shot.id for shot in shots if shot.long_line],
    }
