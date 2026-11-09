"""LangGraph 全局状态定义模块（对齐 PRD §6.3 / 技术方案 §6.1）。

定义编剧子图共享的 :class:`ProjectState` TypedDict：

- **身份与输入**：project_id / user_id / 创作要素（idea、题材、画风、集数）；
- **创作产物**：以"引用 + 摘要"入 State，全文落 Script 表与 project_files；
- **执行态**：write_todos 计划（Annotated 追加合并）、质检轮次、
  审批决策快照；
- **计费守卫**：freeze_id / estimated_credits / actual_cost_credits——
  资金字段的真值以数据库为准，State 仅作镜像传递（技术方案 §6.2）。

设计约定：
- 所有节点幂等（节点输出按 project_id + 节点名 + 输入 hash 登记
  idempotency_keys），重跑不重复扣费；
- LLM 节点绝不调用计费函数，资金操作只发生在确定性节点
  （billing_precheck / finalize_approve / 失败退款）。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ProjectState(TypedDict, total=False):
    """编剧子图全局状态（LangGraph State，节点间以部分更新合并）。

    Attributes:
        project_id: 项目 ID（thread_id = str(project_id)）。
        user_id: 发起用户 ID（项目 Owner）。
        action: 当前编排动作标识（本阶段固定 "generate_script"）。
        idea: 一句话创意（取项目 synopsis 或请求覆盖值）。
        style_preset_key: 画风预设键 live_action/anime/chinese_style/three_d。
        aspect_ratio: 画面比例 9:16/16:9/1:1。
        genre: 题材。
        target_episode_count: 目标集数。
        script_summary: 剧本摘要（引用 + 概要，全文落库）。
        outline: 大纲产物（剧名/logline/人物小传/分集计划，契约 Outline）。
        write_todos: 编剧计划清单（大纲→人物→分集→对白，追加合并）。
        episodes_script: 逐集剧本产物列表（契约 EpisodeScript，含集/场/行）。
        qc_rounds: 审核/自检轮次（flag 弱化重审一次的计数）。
        compliance_findings: 审核发现（flag 时注入重生成提示词）。
        human_feedback: 用户 reject 时的反馈原文（DR-4）。
        decision: 最近一次 HITL 决策快照 {action, feedback, edited_payload}。
        freeze_id: 当前活跃冻结记录 ID（同一 freeze 跨 reject 改写复用）。
        script_run_id: 当前剧本生成运行态 ID（V4.0，支撑生成中终止按集结算）。
        estimated_credits: 预估积分（按集数×单价）。
        actual_cost_credits: 实际消耗积分（LLM 节点按网关成本累计，上限为冻结额）。
        script_id: 当前剧本版本 ID（每次落库产生新版本）。
        approval_id: 最近创建的审批单 ID。
        error: 运行错误摘要（失败时写入）。
    """

    # ---------- 身份与输入 ----------
    project_id: int
    user_id: int
    action: str
    idea: str
    style_preset_key: str
    aspect_ratio: str
    genre: str
    target_episode_count: int

    # ---------- 创作产物（引用 + 摘要） ----------
    script_summary: str | None
    outline: dict[str, Any] | None
    write_todos: Annotated[list[dict[str, Any]], operator.add]
    episodes_script: list[dict[str, Any]]

    # ---------- 质量与合规 ----------
    qc_rounds: int
    compliance_findings: list[dict[str, Any]]

    # ---------- 人机协同 ----------
    human_feedback: str | None
    decision: dict[str, Any] | None

    # ---------- 计费守卫（真值以 DB 为准） ----------
    freeze_id: int | None
    script_run_id: int | None
    estimated_credits: int
    actual_cost_credits: int

    # ---------- 落库引用 ----------
    script_id: int | None
    approval_id: int | None

    # ---------- 运行观测 ----------
    error: str | None
