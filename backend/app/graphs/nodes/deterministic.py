"""确定性图节点与编排辅助设施模块。

包含三类内容：

1. **幂等辅助**：:func:`idempotency_lookup` / :func:`idempotency_register` /
   :func:`idempotency_complete`——节点级幂等（sha256(project_id:node:input_hash)），
   产物快照存 idempotency_keys.response_ref，重跑不重复扣费/生成；
2. **AgentRun 留痕辅助**：:func:`begin_agent_run` / :func:`finish_agent_run`——
   每个 LLM 节点写入 agent_runs（步数/token/成本/trace_id）；
3. **确定性节点**：
   - :func:`billing_precheck`：图入口计费守卫（precheck_and_freeze +
     draft→scripting 流转），LLM 节点绝不接触资金；
   - :func:`split_episodes_hook`：剧本落库钩子（scripts 新版本 +
     episodes/scenes/script_lines + project_files 登记）；
   - :func:`apply_human_edit`：HITL edit 分支（用 edited_payload 替换
     剧本集列表后重走落库）；
   - :func:`finalize_approve`：approve 分支（settle 冻结 + lock 剧本 +
     script_review→asset_preparing 流转）。

会话策略：节点运行于后台任务，全部使用独立短会话
（``async_session_factory()``），不在节点间共享 ORM 对象。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id, sha256_hash
from app.infra.event_bus import event_bus
from app.models.collab import AgentRun
from app.models.project import IdempotencyKey, Project, ProjectFile
from app.models.quality import ModerationRecord
from app.models.script import Episode, Scene, Script, ScriptLine
from app.repositories.billing_repo import BillingRepository
from app.services.billing_service import (
    build_freeze_idempotency_key,
    find_freeze_by_idempotency_key,
    precheck_and_freeze,
    settle,
)
from app.services.project_service import transition

logger = logging.getLogger("gleamdream.graphs.nodes")

# ---- 计费常量 ----
#: 每集剧本生成预估单价（积分）：预估 = target_episode_count × 50
CREDITS_PER_EPISODE = 50
#: 幂等登记有效期（天）
IDEMPOTENCY_TTL_DAYS = 7
#: HITL 审批单有效期（天，PRD F2：审批单 7 天过期）
APPROVAL_TTL_DAYS = 7

# ---- 剧本落库常量 ----
#: 中文 TTS 口播速率（字/秒），用于估算单集时长
_CHARS_PER_SECOND = 4.0


def utcnow() -> datetime:
    """获取当前 UTC 时间（timezone-aware）。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


# =====================================================================
# 幂等辅助（IdempotencyKey：node 级幂等，产物快照存 response_ref）
# =====================================================================


def node_idempotency_key(project_id: int, node_name: str, input_hash: str) -> str:
    """计算节点级幂等键 hash：sha256(project_id:node_name:input_hash)。

    Args:
        project_id: 项目 ID。
        node_name: 节点名（snake_case）。
        input_hash: 输入内容指纹（调用方自行定义的 sha256 hex）。

    Returns:
        64 位十六进制幂等键。
    """
    return sha256_hash(f"{project_id}:{node_name}:{input_hash}")


async def idempotency_lookup(
    project_id: int, node_name: str, input_hash: str
) -> dict[str, Any] | None:
    """按幂等键查找已登记的节点产物快照（命中即复用，不重复生成）。

    Args:
        project_id: 项目 ID。
        node_name: 节点名。
        input_hash: 输入指纹。

    Returns:
        产物快照 dict（response_ref）；未登记或未完成时返回 None。

    Raises:
        无（查询失败按未命中处理并告警）。
    """
    key_hash = node_idempotency_key(project_id, node_name, input_hash)
    try:
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.key_hash == key_hash,
                        IdempotencyKey.status == "done",
                    )
                )
            ).scalar_one_or_none()
            if row is not None and isinstance(row.response_ref, dict):
                return row.response_ref
    except Exception:  # noqa: BLE001 幂等查询失败不阻断主流程
        logger.warning("幂等查询失败 project_id=%s node=%s", project_id, node_name, exc_info=True)
    return None


async def idempotency_register(
    project_id: int, node_name: str, input_hash: str
) -> str:
    """登记幂等键（status=processing，on conflict do nothing 幂等）。

    Args:
        project_id: 项目 ID。
        node_name: 节点名。
        input_hash: 输入指纹。

    Returns:
        本节点的幂等键 hash（后续 complete 用）。

    Raises:
        无（登记失败仅告警——幂等是优化手段，不阻断生成）。
    """
    key_hash = node_idempotency_key(project_id, node_name, input_hash)
    try:
        async with async_session_factory() as session:
            stmt = (
                pg_insert(IdempotencyKey)
                .values(
                    project_id=project_id,
                    node_name=node_name[:64],
                    scope="agent_node",
                    key_hash=key_hash,
                    request_hash=input_hash,
                    status="processing",
                    first_seen_at=utcnow(),
                    expires_at=utcnow() + timedelta(days=IDEMPOTENCY_TTL_DAYS),
                )
                .on_conflict_do_nothing(index_elements=["key_hash"])
            )
            await session.execute(stmt)
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("幂等登记失败 project_id=%s node=%s", project_id, node_name, exc_info=True)
    return key_hash


async def idempotency_complete(
    key_hash: str, result_ref: dict[str, Any]
) -> None:
    """把节点产物快照写回幂等键（status=processing -> done）。

    Args:
        key_hash: 幂等键 hash。
        result_ref: 产物快照（JSONB 可序列化 dict）。

    Raises:
        无（写回失败仅告警——下次重跑会重新生成而非复用）。
    """
    try:
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    select(IdempotencyKey).where(IdempotencyKey.key_hash == key_hash)
                )
            ).scalar_one_or_none()
            if row is None:
                # 登记失败过：补插一条 done 记录
                session.add(
                    IdempotencyKey(
                        node_name="unknown",
                        scope="agent_node",
                        key_hash=key_hash,
                        status="done",
                        response_ref=result_ref,
                        first_seen_at=utcnow(),
                        expires_at=utcnow() + timedelta(days=IDEMPOTENCY_TTL_DAYS),
                    )
                )
            else:
                row.status = "done"
                row.response_ref = result_ref
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("幂等写回失败 key_hash=%s", key_hash[:12], exc_info=True)


# =====================================================================
# AgentRun 留痕辅助
# =====================================================================


async def begin_agent_run(
    project_id: int,
    node_name: str,
    agent_key: str,
    input_summary: dict[str, Any] | None = None,
) -> int:
    """创建一条 agent_runs 运行记录（status=running）。

    Args:
        project_id: 项目 ID。
        node_name: 节点名。
        agent_key: Agent 键（screenwriter/compliance/qc 等）。
        input_summary: 入参摘要（JSONB，敏感字段不得放入）。

    Returns:
        运行记录 ID。

    Raises:
        无（留痕失败返回 -1，不阻断主流程）。
    """
    try:
        async with async_session_factory() as session:
            run = AgentRun(
                project_id=project_id,
                node_name=node_name[:64],
                agent_key=agent_key,
                status="running",
                steps=0,
                input_summary=input_summary,
                started_at=utcnow(),
                trace_id=new_request_id(),
            )
            session.add(run)
            await session.commit()
            return int(run.id)
    except Exception:  # noqa: BLE001
        logger.warning("写入 agent_runs(running) 失败 node=%s", node_name, exc_info=True)
        return -1


async def finish_agent_run(
    run_id: int,
    *,
    status: str,
    steps: int = 1,
    tokens_total: int = 0,
    cost_credits: int = 0,
    latency_ms: int | None = None,
    trace_id: str | None = None,
    output_summary: dict[str, Any] | None = None,
    error_reason: str | None = None,
) -> None:
    """完结一条 agent_runs 记录（succeeded/failed/interrupted）。

    Args:
        run_id: 运行记录 ID（-1 表示留痕失败过，跳过）。
        status: 终态 running/succeeded/failed/interrupted。
        steps: 步数（≥0）。
        tokens_total: 总 token 数。
        cost_credits: 成本积分。
        latency_ms: 耗时毫秒。
        trace_id: 链路 ID（取最后一次模型调用的 trace）。
        output_summary: 出参摘要。
        error_reason: 失败原因。

    Raises:
        无（留痕失败仅告警，不阻断主流程）。
    """
    if run_id < 0:
        return
    try:
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                return
            run.status = status
            run.steps = steps
            run.total_tokens = max(0, tokens_total)
            run.cost_credits = max(0, cost_credits)
            if latency_ms is not None:
                run.latency_ms = latency_ms
            if trace_id:
                run.trace_id = trace_id[:64]
            run.output_summary = output_summary
            run.error_reason = error_reason[:1000] if error_reason else None
            run.finished_at = utcnow()
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("完结 agent_runs 失败 run_id=%s", run_id, exc_info=True)


# =====================================================================
# 确定性节点
# =====================================================================


async def billing_precheck(state: dict[str, Any]) -> dict[str, Any]:
    """图入口计费守卫节点：余额预检 + 冻结 + draft→scripting 流转。

    预估成本 = target_episode_count × :data:`CREDITS_PER_EPISODE`；
    冻结幂等键 = build_freeze_idempotency_key(project_id, task, input_hash)，
    命中且仍 frozen 时复用同一冻结（reject 改写不重复冻结）；
    命中但已 settled/released 时（历史流程已完成）重新冻结开启新一轮。

    Args:
        state: 编排状态（需 project_id/user_id/idea）。

    Returns:
        状态更新 {estimated_credits, freeze_id}。

    Raises:
        BizError: 30001 积分不足；10002 项目不存在；40001 状态不允许流转。
    """
    project_id = int(state["project_id"])
    user_id = int(state["user_id"])
    idea = str(state.get("idea") or "")
    async with async_session_factory() as session:
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if project is None:
            raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

        episode_count = int(project.target_episode_count)
        estimated = episode_count * CREDITS_PER_EPISODE
        input_hash = sha256_hash(f"{user_id}:{episode_count}:{idea}")
        idem_key = build_freeze_idempotency_key(project_id, "script_generate", input_hash)

        # 幂等复用：命中且仍冻结 → 同一 freeze 跨 reject 改写复用
        freeze = None
        existing = await find_freeze_by_idempotency_key(session, idem_key)
        if existing is not None and existing.status == "frozen":
            freeze = existing
        account = await BillingRepository(session).get_account_by_user(user_id)
        if account is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
        if freeze is None:
            freeze = await precheck_and_freeze(
                session,
                account_id=int(account.id),
                project_id=project_id,
                task_type="script_generate",
                estimated_credits=estimated,
                idempotency_key=idem_key,
            )

        # 状态流转：仅 draft 进入 scripting（重启恢复/scripting 重入不重复流转）
        if project.status == "draft":
            await transition(
                project, "scripting", "启动剧本生成", session=session, user_id=user_id
            )

        # V4.0：登记生成运行态（支撑生成中终止按集结算，AC-F2-12-1~3）
        from app.services import script_run_service

        run = await script_run_service.start_run(
            session,
            project_id=project_id,
            total_episodes=episode_count,
            frozen_credits=int(freeze.frozen_credits),
            user_id=user_id,
            idempotency_key=f"script_run:{idem_key}",
            freeze_id=int(freeze.id),
        )
        await session.commit()
        await script_run_service.mark_plan_node(project_id, "outline", "running")

        await event_bus.publish(
            project_id,
            "progress",
            {
                "step": "billing_precheck",
                "progress": 5,
                "detail": f"已冻结 {estimated} 积分（预估 {episode_count} 集）",
            },
        )
        return {
            "estimated_credits": estimated,
            "freeze_id": int(freeze.id),
            "script_run_id": int(run.id),
        }


def _estimate_episode_seconds(episode: dict[str, Any]) -> int:
    """按行字数估算单集目标时长（clamp 到 60-180 秒，PRD F2.5）。

    Args:
        episode: 单集剧本契约产物（EpisodeScript dict）。

    Returns:
        目标时长（秒）。
    """
    total_chars = 0
    for scene in episode.get("scenes", []):
        for line in scene.get("lines", []):
            total_chars += len(str(line.get("content") or ""))
    seconds = int(round(total_chars / _CHARS_PER_SECOND))
    return max(60, min(180, seconds))


def _normalize_time_of_day(value: str | None) -> str | None:
    """把契约 6 值时段收敛为 DB 4 值（day/night/dawn/dusk）。

    Args:
        value: 契约时段值。

    Returns:
        DB 合法时段值；非法入参返回 None。
    """
    from app.graphs.contracts import TIME_OF_DAY_DB_MAP

    if not value:
        return None
    return TIME_OF_DAY_DB_MAP.get(value)


def _normalize_emotion(value: str | None) -> str | None:
    """校验情绪标签是否在 DB 8 值内（非法置 None，保证落库不违约）。

    Args:
        value: 契约情绪值。

    Returns:
        合法情绪标签或 None。
    """
    allowed = {"calm", "joy", "anger", "sadness", "fear", "surprise", "tension", "romance"}
    return value if value in allowed else None


async def _next_version_no(session: AsyncSession, project_id: int) -> int:
    """查询项目当前最大剧本版本号并返回下一版本号。

    Args:
        session: 异步数据库会话（调用方管理事务）。
        project_id: 项目 ID。

    Returns:
        下一版本号（≥1）。
    """
    current = (
        await session.execute(
            select(func.max(Script.version_no)).where(Script.project_id == project_id)
        )
    ).scalar_one()
    return int(current or 0) + 1


async def _upsert_project_file(
    session: AsyncSession,
    *,
    project_id: int,
    path: str,
    file_type: str,
    content: str,
    agent_run_id: int | None,
) -> None:
    """登记/覆盖一条 project_files 虚拟工作区文件索引。

    Args:
        session: 异步数据库会话（调用方管理事务）。
        project_id: 项目 ID。
        path: 工作区路径（/project/ 前缀）。
        file_type: 文件类型（script/json 等）。
        content: 文本内容（用于 hash 与体积）。
        agent_run_id: 产出运行 ID（可空）。

    Raises:
        无（内容为空时跳过）。
    """
    if not content:
        return
    await session.execute(
        delete(ProjectFile).where(ProjectFile.project_id == project_id, ProjectFile.path == path)
    )
    session.add(
        ProjectFile(
            project_id=project_id,
            path=path,
            file_type=file_type,
            content_hash=sha256_hash(content),
            size_bytes=len(content.encode("utf-8")),
            agent_run_id=agent_run_id if (agent_run_id is not None and agent_run_id > 0) else None,
        )
    )


async def persist_script_version(
    state: dict[str, Any], *, status: str = "pending_confirm"
) -> dict[str, Any]:
    """把大纲 + 逐集剧本落库为新版本剧本（核心落库函数，单事务）。

    产物：scripts（version_no 递增 + structure JSONB）、episodes（含
    时长估算与钩子）、scenes、script_lines、project_files（ep{n}.md
    与 script.json）、projects.current_script_id 引用更新。

    Args:
        state: 编排状态（需 project_id/user_id/outline/episodes_script）。
        status: 剧本初始状态（默认 pending_confirm）。

    Returns:
        {script_id, version_no}。

    Raises:
        BizError: 10002 项目不存在；40002 大纲/剧集产物缺失。
    """
    project_id = int(state["project_id"])
    user_id = int(state["user_id"])
    outline = state.get("outline")
    episodes = state.get("episodes_script") or []
    if not outline or not episodes:
        raise BizError(ErrorCode.SCRIPT_STRUCTURE_INVALID, "大纲或剧集产物缺失，无法落库")

    from app.graphs.contracts import episodes_to_structure
    from app.graphs.prompts.screenwriter import episode_to_markdown, structure_json_text

    structure = episodes_to_structure(outline, episodes)
    structure_text = structure_json_text(structure)

    async with async_session_factory() as session:
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if project is None:
            raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

        version_no = await _next_version_no(session, project_id)
        prev_script_id = (
            (
                await session.execute(
                    select(Script.id)
                    .where(Script.project_id == project_id)
                    .order_by(Script.version_no.desc())
                    .limit(1)
                )
            )
            .scalar_one_or_none()
        )

        total_words = sum(
            len(str(line.get("content") or ""))
            for ep in episodes
            for scene in ep.get("scenes", [])
            for line in scene.get("lines", [])
        )
        script = Script(
            project_id=project_id,
            version_no=version_no,
            parent_script_id=prev_script_id,
            source_type="generated",
            status=status,
            title=str(outline.get("title") or project.title)[:255],
            logline=outline.get("logline"),
            genre=str(outline.get("genre") or project.genre or "")[:32],
            total_episodes=len(episodes),
            total_words=total_words,
            structure=structure,
            created_by=user_id,
        )
        session.add(script)
        await session.flush()

        # 每集时长与钩子（确定性估算：字数/4 秒速，clamp 60-180 秒）
        for idx, ep in enumerate(sorted(episodes, key=lambda e: int(e["episode_no"]))):
            episode_row = Episode(
                script_id=script.id,
                project_id=project_id,
                episode_no=int(ep["episode_no"]),
                title=str(ep.get("title") or "")[:255],
                synopsis=ep.get("summary"),
                target_duration_seconds=_estimate_episode_seconds(ep),
                hook_text=ep.get("hook"),
                order_index=idx,
                status="draft",
            )
            session.add(episode_row)
            await session.flush()

            for scene in ep.get("scenes", []):
                scene_row = Scene(
                    episode_id=episode_row.id,
                    project_id=project_id,
                    scene_no=int(scene["scene_no"]),
                    title=None,
                    location=str(scene.get("location") or "")[:255],
                    time_of_day=_normalize_time_of_day(scene.get("time_of_day")),
                    int_ext=scene.get("int_ext") if scene.get("int_ext") in ("int", "ext") else None,
                    summary=scene.get("summary"),
                    order_index=int(scene["scene_no"]) - 1,
                )
                session.add(scene_row)
                await session.flush()

                for line_idx, line in enumerate(scene.get("lines", [])):
                    session.add(
                        ScriptLine(
                            scene_id=scene_row.id,
                            project_id=project_id,
                            line_no=line_idx + 1,
                            line_type=line.get("line_type"),
                            speaker_name=str(line.get("speaker") or "")[:64] or None,
                            content=str(line.get("content") or ""),
                            emotion_tag=_normalize_emotion(line.get("emotion")),
                            order_index=line_idx,
                        )
                    )

            # 虚拟工作区：每集 markdown 剧本
            await _upsert_project_file(
                session,
                project_id=project_id,
                path=f"/project/{project_id}/script/ep{int(ep['episode_no']):02d}.md",
                file_type="script",
                content=episode_to_markdown(outline, ep),
                agent_run_id=None,
            )

        # 虚拟工作区：全剧结构化 JSON
        await _upsert_project_file(
            session,
            project_id=project_id,
            path=f"/project/{project_id}/script/script.json",
            file_type="json",
            content=structure_text,
            agent_run_id=None,
        )

        # 引用与预算镜像更新
        project.current_script_id = int(script.id)
        project.lock_version += 1
        await session.commit()

    await event_bus.publish(
        project_id,
        "progress",
        {
            "step": "script_persisted",
            "progress": 70,
            "detail": f"剧本 v{version_no} 已落库（{len(episodes)} 集）",
        },
    )
    return {"script_id": int(script.id), "version_no": version_no}


async def split_episodes_hook(state: dict[str, Any]) -> dict[str, Any]:
    """分集拆分钩子节点（确定性）：时长估算 + 结尾钩子 + 剧本落库。

    大纲/剧集产物的结构已由 LLM 节点契约校验，本节点做确定性收尾：
    每集时长按字数估算（1-3 分钟）、钩子文本回填（缺失时用集简介兜底）、
    产物落库为新剧本版本并登记虚拟工作区文件。幂等键基于
    structure 内容 hash——同内容重跑直接复用已落库版本。

    Args:
        state: 编排状态（需 project_id/user_id/outline/episodes_script）。

    Returns:
        状态更新 {script_id}。

    Raises:
        BizError: 40002 剧本结构缺失。
    """
    project_id = int(state["project_id"])
    outline = state.get("outline") or {}
    episodes = state.get("episodes_script") or []

    from app.graphs.contracts import episodes_to_structure

    structure = episodes_to_structure(outline, episodes)
    input_hash = sha256_hash(json.dumps(structure, ensure_ascii=False, sort_keys=True))
    cached = await idempotency_lookup(project_id, "split_episodes_hook", input_hash)
    if cached and cached.get("script_id"):
        await event_bus.publish(
            project_id,
            "progress",
            {"step": "split_episodes_hook", "progress": 70, "detail": "命中幂等缓存，复用剧本版本"},
        )
        return {"script_id": int(cached["script_id"])}

    key_hash = await idempotency_register(project_id, "split_episodes_hook", input_hash)
    result = await persist_script_version(state)
    await idempotency_complete(
        key_hash, {"script_id": result["script_id"], "version_no": result["version_no"]}
    )
    # V4.0：运行态记录已完成集数与产出剧本版本（终止结算依据）
    from app.services import script_run_service

    await script_run_service.record_progress(project_id, len(episodes))
    await script_run_service.mark_plan_node(project_id, "dialogues", "done")
    return {"script_id": result["script_id"]}


async def apply_human_edit(state: dict[str, Any]) -> dict[str, Any]:
    """HITL edit 分支节点（确定性）：用 edited_payload 替换剧集产物。

    语义：用户在审批台直接编辑剧本（edited_payload 为 {episodes: [...]}
    或 [...] 数组，元素结构同 EpisodeScript 契约）。本节点把
    episodes_script 替换为编辑后的列表（逐项做基本结构校验），随后由
    拓扑进入 split_episodes_hook 重建剧本版本、再创建新审批单。

    Args:
        state: 编排状态（decision.edited_payload 必须存在）。

    Returns:
        状态更新 {episodes_script, decision(保留)}。

    Raises:
        BizError: 10001 edited_payload 缺失或结构非法。
    """
    decision = state.get("decision") or {}
    payload = decision.get("edited_payload")
    if not payload:
        raise BizError(ErrorCode.PARAM_INVALID, "edit 决策缺少 edited_payload")

    episodes = payload.get("episodes") if isinstance(payload, dict) else payload
    if not isinstance(episodes, list) or not episodes:
        raise BizError(ErrorCode.PARAM_INVALID, "edited_payload 须为 {episodes: [...]} 或数组")

    normalized: list[dict[str, Any]] = []
    for ep in episodes:
        if not isinstance(ep, dict) or "episode_no" not in ep or "scenes" not in ep:
            raise BizError(ErrorCode.PARAM_INVALID, "edited_payload 中存在非法集结构")
        normalized.append(ep)

    return {"episodes_script": normalized}


async def finalize_approve(state: dict[str, Any]) -> dict[str, Any]:
    """HITL approve 分支节点（确定性）：结算冻结 + 锁定剧本 + 状态流转。

    动作序列（单流程顺序提交）：
    1. settle：actual = max(1, min(actual_cost_credits, 冻结额))；
    2. lock 剧本：status=locked + locked_at + projects.current_script_id；
    3. 项目状态 script_review → asset_preparing（资产子图下一阶段）；
    4. 发布 run.completed 事件。

    Args:
        state: 编排状态（需 project_id/script_id/freeze_id/actual_cost_credits）。
            decision.target_script_id 存在时以其为锁定目标（审批单重指校正）。

    Returns:
        状态更新 {actual_cost_credits}。

    Raises:
        BizError: 30005 冻结不存在（含冻结过期被释放的场景）。
    """
    project_id = int(state["project_id"])
    script_id = state.get("script_id")
    freeze_id = state.get("freeze_id")
    actual = max(1, int(state.get("actual_cost_credits") or 0))

    # 2026-11 修复（遗留 D）：审批单可能被 script_service._ensure_script_approval
    # 重指到重新生成的最新版本（target_id 更新），而 checkpoint state.script_id
    # 仍是首次生成的旧版本。approve 时以用户实际批准的审批单 target 为准，
    # 否则会锁定旧稿并把 projects.current_script_id 回拨（912/913 事故）。
    decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    target_script_id = decision.get("target_script_id")
    if target_script_id:
        try:
            script_id = int(target_script_id)
        except (TypeError, ValueError):
            logger.warning(
                "finalize_approve 决策载荷 target_script_id 非法，回退 state.script_id：%r",
                target_script_id,
            )

    async with async_session_factory() as session:
        # 1. 结算（settle 内部行锁 + 三件套流水）
        if freeze_id:
            summary = await settle(session, freeze_id=int(freeze_id), actual_credits=actual)
            actual = int(summary["actual_credits"])

        # 2. 锁定剧本 + 项目引用/消耗更新
        if script_id:
            script = await session.get(Script, int(script_id))
            if script is not None:
                script.status = "locked"
                script.locked_at = utcnow()
            project = (
                await session.execute(
                    select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if project is not None:
                project.current_script_id = int(script_id)
                # spent_credits 由 settle 统一按实际结算额累加，此处不再重复
                project.lock_version += 1
        await session.commit()

    # 3. 状态流转 script_review → asset_preparing
    async with async_session_factory() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is not None and project.status == "script_review":
            await transition(
                project, "asset_preparing", "剧本审批通过", session=session, user_id=None
            )

    await event_bus.publish(
        project_id,
        "run.completed",
        {"step": "finalize_approve", "progress": 100, "actual_cost_credits": actual},
    )
    return {"actual_cost_credits": actual}


async def write_moderation_record(
    project_id: int,
    report: dict[str, Any],
    *,
    structure: dict[str, Any],
    latency_ms: int,
    trace_id: str | None,
) -> int:
    """把一轮审核结论写入 moderation_records（只追加，监管追溯 NFR-C5）。

    verdict 映射：pass→pass / flag→review / block→block。

    Args:
        project_id: 项目 ID。
        report: 审核契约产物（ComplianceReport dict）。
        structure: 被审核的结构化剧本（请求快照摘要）。
        latency_ms: 审核调用耗时（毫秒）。
        trace_id: 链路 ID。

    Returns:
        审核记录 ID（失败返回 -1，不阻断主流程）。

    Raises:
        无（落库失败仅告警）。
    """
    verdict_map = {"pass": "pass", "flag": "review", "block": "block"}
    try:
        async with async_session_factory() as session:
            record = ModerationRecord(
                project_id=project_id,
                provider="llm_compliance",
                api_name="chat.completions",
                verdict=verdict_map.get(str(report.get("verdict")), "review"),
                request_snapshot={
                    "structure_preview": str(structure)[:1000],
                    "episode_count": len(structure.get("episodes", [])),
                },
                response_snapshot=report,
                cost_credits=0,
                latency_ms=latency_ms,
                trace_id=trace_id[:64] if trace_id else None,
            )
            session.add(record)
            await session.commit()
            return int(record.id)
    except Exception:  # noqa: BLE001
        logger.warning("写入 moderation_records 失败 project_id=%s", project_id, exc_info=True)
        return -1


def affected_episodes_from_feedback(feedback: str | None, total: int) -> set[int]:
    """从用户反馈解析受影响集号（局部改写范围，AC-F2-04-1）。

    规则：匹配「第 N 集」模式；无匹配时返回全集（整体改写）。

    Args:
        feedback: 用户反馈原文（可空）。
        total: 总集数。

    Returns:
        受影响集号集合（1-based，全集时为 {1..total}）。
    """
    if not feedback:
        return set(range(1, total + 1))
    numbers = [int(n) for n in re.findall(r"第\s*(\d+)\s*集", feedback)]
    valid = {n for n in numbers if 1 <= n <= total}
    return valid if valid else set(range(1, total + 1))
