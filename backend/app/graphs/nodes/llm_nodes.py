"""LLM Agent 图节点模块（结构化 LLM 调用 + JSON 契约校验 + 自修循环）。

不引入 deepagents（与 langgraph 1.2 兼容性未验证），每个 Agent 节点
实现为「五段式提示词 → model_gateway.generate(llm/llm_text) →
pydantic 契约校验 → 失败自修重试（≤2 次，错误信息拼回提示词）」。

节点清单：
- :func:`generate_outline`：编剧——大纲 + 人物 + 分集计划 + write_todos；
- :func:`generate_episodes`：编剧——逐集生成「集/场/行」，支持 reject
  局部改写（仅重生成受影响集，其余从幂等缓存复用）；
- :func:`compliance_review`：审核 Agent——内容安全扫描，block 终止、
  flag 弱化重审一次（qc_rounds 计数）。

工程约定：
- **计费隔离**：LLM 节点绝不调用计费函数，仅把网关返回的 cost_credits
  累计到 state（actual_cost_credits），结算由确定性节点执行；
- **节点级幂等**：入口算幂等键（含输入指纹），命中直接复用
  response_ref 产物快照，重跑不重复调用模型；
- **AgentRun 留痕**：每个节点写入 agent_runs（steps=1，token/成本/
  trace_id 取多次调用累计/末次值）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.errors import BizError, ErrorCode
from app.graphs.contracts import (
    ComplianceReport,
    EpisodeScript,
    Outline,
    contract_error_text,
    episodes_to_structure,
)
from app.graphs.nodes.deterministic import (
    begin_agent_run,
    affected_episodes_from_feedback,
    finish_agent_run,
    idempotency_complete,
    idempotency_lookup,
    idempotency_register,
    write_moderation_record,
)
from app.graphs.prompts.compliance import build_compliance_prompt
from app.graphs.prompts.screenwriter import build_episode_prompt, build_outline_prompt
from app.infra.event_bus import event_bus
from app.infra.model_gateway.base import GenRequest
from app.infra.model_gateway.gateway import model_gateway

logger = logging.getLogger("gleamdream.graphs.llm_nodes")

#: LLM 自修重试上限（首次调用 + ≤2 次自修）
MAX_SELF_FIX_RETRIES = 2
#: 弱化重审轮次上限（flag 自动弱化重审一次）
MAX_FLAG_ROUNDS = 1
#: LLM 单次调用最大输出 token（8000：整集剧本 JSON 约 6k 字符，
#: 过小会被截断导致 JSON 解析失败触发自修重试链）
LLM_MAX_TOKENS = 8000

T = TypeVar("T", bound=BaseModel)


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
        # 剥离 ```json ... ``` 围栏
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


async def _call_llm_contract(
    *,
    system: str,
    user: str,
    contract_type: type[T],
    project_id: int,
    run_id: int,
    temperature: float = 0.7,
) -> tuple[T, dict[str, Any]]:
    """执行一次「LLM 调用 → JSON 提取 → 契约校验 → 自修循环」。

    自修策略：解析/校验失败时，把原始输出片段与错误信息拼回提示词
    重试，最多 :data:`MAX_SELF_FIX_RETRIES` 次；重试后仍失败抛
    BizError(80001)。

    Args:
        system: 五段式系统提示词。
        user: 用户提示词。
        contract_type: pydantic 契约模型（Outline/EpisodeScript/ComplianceReport）。
        project_id: 项目 ID（网关日志关联）。
        run_id: AgentRun 记录 ID（留痕）。
        temperature: 采样温度（默认 0.7）。

    Returns:
        (契约对象, 调用累计 meta)——meta 含 tokens_in/tokens_out/
        cost_credits/model_key/trace_id/latency_ms/attempt。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规。
    """
    accumulated: dict[str, Any] = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_credits": 0,
        "latency_ms": 0,
        "model_key": None,
        "trace_id": None,
    }
    last_error = ""
    last_output = ""
    for attempt in range(1 + MAX_SELF_FIX_RETRIES):
        user_now = user
        if attempt > 0 and last_error:
            user_now = (
                f"{user}\n\n【自修要求】你上一次的输出不合规，请修正后重新输出。"
                f"错误：{last_error}\n"
                f"上次输出片段：{last_output[:600]}\n"
                "请严格只输出符合契约的 JSON。"
            )
        request = GenRequest(
            capability="llm",
            prompt=user_now,
            system_prompt=system,
            params={"temperature": temperature, "max_tokens": LLM_MAX_TOKENS, "response_json": True},
        )
        result = await model_gateway.generate(
            "llm", "llm_text", request, project_id=project_id
        )
        accumulated["tokens_in"] += int(result.meta.get("tokens_in") or 0)
        accumulated["tokens_out"] += int(result.meta.get("tokens_out") or 0)
        accumulated["cost_credits"] += int(result.meta.get("cost_credits") or 0)
        accumulated["latency_ms"] += int(result.meta.get("duration_ms") or 0)
        accumulated["model_key"] = result.meta.get("model_key")
        accumulated["trace_id"] = result.meta.get("trace_id")

        if not result.ok or not result.text:
            last_error = f"模型调用失败：{result.error or '空响应'}"
            last_output = ""
            logger.warning("LLM 调用失败 attempt=%s node_run=%s err=%s", attempt, run_id, last_error)
            continue
        try:
            payload = _extract_json(result.text)
            contract_obj = contract_type.model_validate(payload)
            accumulated["attempt"] = attempt
            return contract_obj, accumulated
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            last_error = contract_error_text(exc)
            last_output = result.text
            logger.warning(
                "契约校验失败 attempt=%s contract=%s err=%s",
                attempt, contract_type.__name__, last_error[:300],
            )
    raise BizError(
        ErrorCode.MODEL_UNAVAILABLE,
        f"模型输出持续不合规（{contract_type.__name__}），已重试 {MAX_SELF_FIX_RETRIES} 次",
        data={"last_error": last_error[:500]},
    )


def _todos_from_outline(outline: Outline) -> list[dict[str, Any]]:
    """把大纲契约中的 write_todos 转为 State 追加结构。

    Args:
        outline: 大纲契约对象。

    Returns:
        [{step, title, detail}, ...]。
    """
    return [
        {"step": t.step, "title": t.title, "detail": t.detail}
        for t in outline.write_todos.todos
    ]


async def generate_outline(state: dict[str, Any]) -> dict[str, Any]:
    """编剧节点一：生成大纲 + 人物小传 + 分集计划 + write_todos。

    幂等键输入指纹：user_id + idea + genre + style + 集数；命中直接
    复用 response_ref（不再追加 write_todos，避免重跑重复累积）。

    Args:
        state: 编排状态（需 project_id/user_id/idea 等）。

    Returns:
        状态更新 {outline, write_todos?}。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规。
    """
    project_id = int(state["project_id"])
    run_id = await begin_agent_run(
        project_id, "generate_outline", "screenwriter",
        input_summary={"idea_preview": str(state.get("idea") or "")[:200]},
    )
    started = time.monotonic()
    try:
        input_hash_src = json.dumps(
            {
                "user_id": state.get("user_id"),
                "idea": state.get("idea"),
                "genre": state.get("genre"),
                "style": state.get("style_preset_key"),
                "episodes": state.get("target_episode_count"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        from app.core.ids import sha256_hash

        input_hash = sha256_hash(input_hash_src)
        cached = await idempotency_lookup(project_id, "generate_outline", input_hash)
        if cached and cached.get("outline"):
            latency_ms = int((time.monotonic() - started) * 1000)
            await finish_agent_run(
                run_id,
                status="succeeded",
                steps=0,
                latency_ms=latency_ms,
                output_summary={"idempotent_hit": True},
            )
            await event_bus.publish(
                project_id, "progress",
                {"step": "generate_outline", "progress": 20, "detail": "命中幂等缓存，复用大纲"},
            )
            # P2-2 修复：幂等复用同样视为大纲完成，落账计划节点
            from app.services import script_run_service

            await script_run_service.mark_plan_node(project_id, "outline", "done")
            return {"outline": cached["outline"]}

        key_hash = await idempotency_register(project_id, "generate_outline", input_hash)
        system, user = build_outline_prompt(state)
        outline_obj, meta = await _call_llm_contract(
            system=system, user=user, contract_type=Outline,
            project_id=project_id, run_id=run_id,
        )
        outline_dict = outline_obj.model_dump(mode="json")
        await idempotency_complete(key_hash, {"outline": outline_dict})
        latency_ms = int((time.monotonic() - started) * 1000)
        await finish_agent_run(
            run_id,
            status="succeeded",
            steps=1,
            tokens_total=int(meta.get("tokens_in", 0)) + int(meta.get("tokens_out", 0)),
            cost_credits=int(meta.get("cost_credits", 0)),
            latency_ms=latency_ms,
            trace_id=meta.get("trace_id"),
            output_summary={"title": outline_dict.get("title"), "model_key": meta.get("model_key")},
        )
        await event_bus.publish(
            project_id, "progress",
            {
                "step": "generate_outline",
                "progress": 20,
                "detail": f"大纲完成：《{outline_dict.get('title')}》",
                "write_todos": _todos_from_outline(outline_obj),
            },
        )
        # P2-2 修复：大纲节点完成即落账（此前 outline 计划节点永远停留在
        # running，前端 WRITE_TODOS 面板与 runs 兜底轮询因此始终 0 推进）
        from app.services import script_run_service

        await script_run_service.mark_plan_node(project_id, "outline", "done")
        return {"outline": outline_dict, "write_todos": _todos_from_outline(outline_obj)}
    except BizError as exc:
        await finish_agent_run(
            run_id, status="failed", error_reason=f"{exc.code}: {exc.message}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        raise


async def generate_episodes(state: dict[str, Any]) -> dict[str, Any]:
    """编剧节点二：逐集生成「集/场/行」结构化剧本（支持局部改写）。

    局部改写语义（AC-F2-04-1）：state.decision.action == "reject" 时，
    从反馈解析受影响集号（「第 N 集」），未提及的集从幂等缓存复用；
    compliance 弱化重审（state.compliance_findings 非空）时按建议
    重新生成受影响集。

    幂等：逐集键 = project_id + generate_episodes + (大纲指纹 + 集号 +
    反馈 + 审核发现指纹)——未受影响集天然命中缓存，不重复调用模型。

    Args:
        state: 编排状态（需 project_id/outline/episodes_script 等）。

    Returns:
        状态更新 {episodes_script}。

    Raises:
        BizError: 80001 模型不可用或输出持续不合规；40002 大纲缺失。
    """
    project_id = int(state["project_id"])
    outline = state.get("outline")
    if not outline:
        raise BizError(ErrorCode.SCRIPT_STRUCTURE_INVALID, "缺少大纲产物，无法逐集生成")

    episode_count = int(state.get("target_episode_count") or len(outline.get("episode_plans", [])))
    plans = {int(p["episode_no"]): p for p in outline.get("episode_plans", [])}

    decision = state.get("decision") or {}
    human_feedback = state.get("human_feedback") or (
        decision.get("feedback") if decision.get("action") == "reject" else None
    )
    findings = state.get("compliance_findings") or []
    existing = {int(e["episode_no"]): e for e in (state.get("episodes_script") or [])}

    # 局部改写范围：reject 反馈解析；弱化重审时重生成含风险定位的集
    if decision.get("action") == "reject":
        affected = affected_episodes_from_feedback(human_feedback, episode_count)
    elif findings:
        located = {
            int(num)
            for f in findings
            for num in re.findall(r"第\s*(\d+)\s*集", str(f.get("location") or ""))
        }
        affected = located if located else set(range(1, episode_count + 1))
    else:
        affected = set(range(1, episode_count + 1))

    from app.core.ids import sha256_hash

    run_id = await begin_agent_run(
        project_id, "generate_episodes", "screenwriter",
        input_summary={
            "affected_episodes": sorted(affected),
            "rewrite": bool(human_feedback),
            "compliance_fix": bool(findings),
        },
    )
    started = time.monotonic()
    tokens_total = 0
    cost_total = 0
    trace_id: str | None = None
    try:
        outline_fp = sha256_hash(json.dumps(outline, ensure_ascii=False, sort_keys=True))
        findings_fp = sha256_hash(json.dumps(findings, ensure_ascii=False, sort_keys=True))
        feedback_fp = sha256_hash(human_feedback or "")
        previous_summaries = [existing[n] for n in sorted(existing)] or None

        episodes_result: dict[int, dict[str, Any]] = {}
        from app.services import script_run_service

        await script_run_service.mark_plan_node(project_id, "characters", "done")
        await script_run_service.mark_plan_node(project_id, "episodes", "running")
        for ep_no in range(1, episode_count + 1):
            # V4.0 协作式终止（AC-F2-12-1）：每集开始前自查终止位，
            # 已终止则收尾当前已完成集，不再发起新的模型调用。
            if await script_run_service.is_cancel_requested(project_id):
                logger.info(
                    "检测到生成终止请求 project_id=%s，停在已完成 %s 集",
                    project_id, len(episodes_result),
                )
                await finish_agent_run(
                    run_id,
                    status="failed",
                    error_reason="用户终止生成",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    output_summary={"episodes": len(episodes_result), "cancelled": True},
                )
                return {
                    "episodes_script": [episodes_result[n] for n in sorted(episodes_result)],
                    "compliance_findings": [],
                    "error": "用户终止生成",
                }
            if ep_no not in affected and ep_no in existing:
                # 未受影响集：引用原版本，不改动（AC-F2-04-1）
                episodes_result[ep_no] = existing[ep_no]
                continue

            input_hash = sha256_hash(
                json.dumps(
                    {"outline": outline_fp, "ep": ep_no, "fb": feedback_fp, "qc": findings_fp},
                    sort_keys=True,
                )
            )
            cached = await idempotency_lookup(project_id, "generate_episodes", input_hash)
            if cached and cached.get("episode"):
                episodes_result[ep_no] = cached["episode"]
                continue

            key_hash = await idempotency_register(project_id, "generate_episodes", input_hash)
            plan = plans.get(ep_no) or {
                "episode_no": ep_no,
                "title": f"第 {ep_no} 集",
                "synopsis": outline.get("logline") or "",
                "hook": "",
            }
            system, user = build_episode_prompt(
                state, outline, plan,
                human_feedback=human_feedback if ep_no in affected else None,
                compliance_findings=findings if ep_no in affected else None,
                previous_episodes=previous_summaries,
            )
            ep_obj, meta = await _call_llm_contract(
                system=system, user=user, contract_type=EpisodeScript,
                project_id=project_id, run_id=run_id, temperature=0.8,
            )
            # 集号强制对齐（防止模型漂移）
            ep_dict = ep_obj.model_dump(mode="json")
            ep_dict["episode_no"] = ep_no

            # P0 剧本模块优化：结构质量自评（3 秒钩子/冲突密度/结尾悬念/
            # 台词质量，纯规则零成本）；低于阈值自动带着扣分项重写一次，
            # 两稿取分高者，避免弱钩子剧本流入分镜与渲染造成链路浪费。
            from app.services import script_quality_service

            quality = script_quality_service.evaluate_episode(ep_dict)
            rewritten = False
            if quality.get("should_rewrite"):
                rewrite_hint = script_quality_service.build_rewrite_feedback(quality)
                rewrite_system = system + f"\n\n【重写指令】{rewrite_hint}"
                logger.info(
                    "剧本自评未达标，触发自动重写 project_id=%s ep=%s score=%s",
                    project_id, ep_no, quality.get("score"),
                )
                try:
                    ep_obj2, meta2 = await _call_llm_contract(
                        system=rewrite_system, user=user, contract_type=EpisodeScript,
                        project_id=project_id, run_id=run_id, temperature=0.85,
                    )
                    ep_dict2 = ep_obj2.model_dump(mode="json")
                    ep_dict2["episode_no"] = ep_no
                    quality2 = script_quality_service.evaluate_episode(ep_dict2)
                    tokens_total += int(meta2.get("tokens_in", 0)) + int(meta2.get("tokens_out", 0))
                    cost_total += int(meta2.get("cost_credits", 0))
                    # 两稿择优：结构分与视觉可拍性加权（视觉 10 分制 ×2），
                    # 避免「结构分更高但视觉要素更差」的稿子覆盖好稿。
                    def _quality_rank(report: dict) -> int:
                        """计算稿件综合排序值（结构分 + 视觉分×2）。

                        Args:
                            report: script_quality_service 评估报告。

                        Returns:
                            综合分值（越大越好）。
                        """
                        return int(report.get("score", 0)) + int(
                            report.get("visual_score", 0)
                        ) * 2

                    if _quality_rank(quality2) > _quality_rank(quality):
                        ep_dict, quality, rewritten = ep_dict2, quality2, True
                except BizError:
                    logger.warning(
                        "剧本自评重写调用失败，保留原稿 project_id=%s ep=%s",
                        project_id, ep_no,
                    )
            episodes_result[ep_no] = ep_dict
            script_quality_reports = state.get("script_quality_reports") or {}
            script_quality_reports[str(ep_no)] = {
                **quality, "rewritten": rewritten,
            }
            state["script_quality_reports"] = script_quality_reports
            await idempotency_complete(key_hash, {"episode": ep_dict})
            tokens_total += int(meta.get("tokens_in", 0)) + int(meta.get("tokens_out", 0))
            cost_total += int(meta.get("cost_credits", 0))
            trace_id = meta.get("trace_id") or trace_id
            previous_summaries = [episodes_result[n] for n in sorted(episodes_result)]
            # V4.0：逐集落进度，使终止请求的按集结算依据尽量贴近真实完成集数
            await script_run_service.record_progress(project_id, len(episodes_result))

        episodes_list = [episodes_result[n] for n in sorted(episodes_result)]
        # 2026-11 QA 修复（质检不可见）：结构/视觉自评报告此前只存在于图状态，
        # 生成结束后即丢失，用户与验收方无法查看"是否质检、哪里不合格"。
        # 这里在生成结束时写入 project.config，项目详情接口可直接读取。
        try:
            from app.core.db import async_session_factory
            from app.models.project import Project

            quality_reports = state.get("script_quality_reports") or {}
            if quality_reports:
                async with async_session_factory() as quality_session:
                    project_row = await quality_session.get(Project, project_id)
                    if project_row is not None:
                        project_config = dict(project_row.config or {})
                        project_config["_script_quality_reports"] = quality_reports
                        project_row.config = project_config
                        await quality_session.commit()
        except Exception:  # noqa: BLE001 - 质检落库失败不影响剧本产物
            logger.warning(
                "剧本质检报告落库失败 project_id=%s", project_id, exc_info=True
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        await finish_agent_run(
            run_id,
            status="succeeded",
            steps=1,
            tokens_total=tokens_total,
            cost_credits=cost_total,
            latency_ms=latency_ms,
            trace_id=trace_id,
            output_summary={
                "episodes": len(episodes_list),
                "regenerated": sorted(affected),
                "script_quality": {
                    ep: {
                        "score": rep.get("score"),
                        "verdict": rep.get("verdict"),
                        "rewritten": rep.get("rewritten", False),
                    }
                    for ep, rep in (state.get("script_quality_reports") or {}).items()
                },
            },
        )
        await event_bus.publish(
            project_id, "progress",
            {
                "step": "generate_episodes",
                "progress": 45,
                "detail": f"逐集剧本完成（{len(episodes_list)} 集）",
            },
        )
        # P2-2 修复：分集节点完成即落账（此前 episodes 计划节点永远停留在
        # running，WRITE_TODOS 面板与 runs 兜底轮询推不满进度）
        await script_run_service.mark_plan_node(project_id, "episodes", "done")
        # 审核发现已消费，清空避免影响下一轮
        return {"episodes_script": episodes_list, "compliance_findings": []}
    except BizError as exc:
        await finish_agent_run(
            run_id, status="failed", error_reason=f"{exc.code}: {exc.message}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        raise


async def compliance_review(state: dict[str, Any]) -> dict[str, Any]:
    """审核 Agent 节点：内容安全扫描（block 终止 / flag 弱化重审一次）。

    判定语义：
    - ``pass``：放行进入落库钩子；
    - ``flag``：qc_rounds < MAX_FLAG_ROUNDS 时 qc_rounds+1 并回编剧
      弱化重生成；已达上限则降级放行（moderation 记 verdict=review）；
    - ``block``：抛 BizError(80002) 终止流程（上层退款 + 项目 failed），
      不提供绕过入口（PRD NFR-C2）。

    幂等键含 qc_rounds（区分同一结构的不同审核轮次）。

    Args:
        state: 编排状态（需 project_id/outline/episodes_script）。

    Returns:
        状态更新 {compliance_findings, qc_rounds}。

    Raises:
        BizError: 80002 内容审核未通过（block）；80001 审核输出不合规。
    """
    project_id = int(state["project_id"])
    outline = state.get("outline") or {}
    structure = episodes_to_structure(outline, state.get("episodes_script") or [])
    qc_rounds = int(state.get("qc_rounds") or 0)

    run_id = await begin_agent_run(
        project_id, "compliance_review", "compliance",
        input_summary={"round": qc_rounds + 1},
    )
    started = time.monotonic()
    try:
        from app.core.ids import sha256_hash

        structure_fp = sha256_hash(json.dumps(structure, ensure_ascii=False, sort_keys=True))
        input_hash = sha256_hash(f"{structure_fp}:{qc_rounds}")
        cached = await idempotency_lookup(project_id, "compliance_review", input_hash)
        if cached and cached.get("report"):
            report_dict = cached["report"]
            meta = dict(cached.get("meta") or {})
        else:
            key_hash = await idempotency_register(project_id, "compliance_review", input_hash)
            system, user = build_compliance_prompt(structure)
            report_obj, meta = await _call_llm_contract(
                system=system, user=user, contract_type=ComplianceReport,
                project_id=project_id, run_id=run_id, temperature=0.2,
            )
            report_dict = report_obj.model_dump(mode="json")
            await idempotency_complete(
                key_hash, {"report": report_dict, "meta": {"trace_id": meta.get("trace_id")}}
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        await write_moderation_record(
            project_id, report_dict,
            structure=structure,
            latency_ms=int(meta.get("latency_ms") or latency_ms),
            trace_id=meta.get("trace_id"),
        )
        await finish_agent_run(
            run_id,
            status="succeeded",
            steps=1,
            tokens_total=int(meta.get("tokens_in", 0)) + int(meta.get("tokens_out", 0)),
            cost_credits=int(meta.get("cost_credits", 0)),
            latency_ms=latency_ms,
            trace_id=meta.get("trace_id"),
            output_summary={"verdict": report_dict.get("verdict"), "round": qc_rounds + 1},
        )

        verdict = report_dict.get("verdict")
        findings = report_dict.get("findings") or []
        if verdict == "block":
            # 高危违规：终止流程，不提供绕过（BizError 由上层兜底退款）
            raise BizError(
                ErrorCode.THIRD_PARTY_REVIEW_FAILED,
                "剧本内容审核未通过（block），流程已终止",
                data={"findings": findings[:10]},
            )
        if verdict == "flag" and qc_rounds < MAX_FLAG_ROUNDS:
            await event_bus.publish(
                project_id, "progress",
                {
                    "step": "compliance_review",
                    "progress": 60,
                    "detail": f"审核发现 {len(findings)} 项风险，自动弱化重审",
                },
            )
            return {"compliance_findings": findings, "qc_rounds": qc_rounds + 1}

        await event_bus.publish(
            project_id, "progress",
            {
                "step": "compliance_review",
                "progress": 60,
                "detail": "审核通过" if verdict == "pass" else "弱化重审后放行（已记录 review）",
            },
        )
        return {"compliance_findings": [], "qc_rounds": qc_rounds}
    except BizError as exc:
        if exc.code == ErrorCode.THIRD_PARTY_REVIEW_FAILED:
            await finish_agent_run(
                run_id, status="succeeded",
                latency_ms=int((time.monotonic() - started) * 1000),
                output_summary={"verdict": "block"},
            )
        else:
            await finish_agent_run(
                run_id, status="failed", error_reason=f"{exc.code}: {exc.message}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        raise
