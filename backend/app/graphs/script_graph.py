"""编剧子图构建模块（Task 6a 核心拓扑）。

图拓扑（编排与执行分离，PRD §6.4）::

    START → billing_precheck（确定性：冻结 + draft→scripting）
          → generate_outline（编剧 LLM：大纲+人物+分集计划+write_todos）
          → generate_episodes（编剧 LLM：逐集生成/局部改写）
          → compliance_review（审核 LLM：block 终止 / flag 弱化重审一次）
              ├─ revise（flag 首轮）→ generate_episodes（弱化重生成）
              └─ split（pass/末轮 flag）→ split_episodes_hook（确定性落库）
          → create_approval（确定性：审批单双写 + scripting→script_review）
          → wait_for_decision（interrupt 挂起，HITL）
              ├─ approve → finalize_approve（settle + lock + →asset_preparing）→ END
              ├─ reject  → generate_episodes（局部改写，复用同一冻结）
              └─ edit    → apply_human_edit → split_episodes_hook → create_approval

HITL 采用「create_approval + wait_for_decision」双节点模式：审批单
创建不放在 interrupt 节点内，规避 LangGraph 恢复重放副作用。

检查点：AsyncPostgresSaver（thread_id=str(project_id)），interrupt
状态持久化，服务重启后凭 pending 审批单即可断点续跑。
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graphs.checkpointer import get_checkpointer
from app.graphs.nodes.deterministic import (
    apply_human_edit,
    billing_precheck,
    finalize_approve,
    split_episodes_hook,
)
from app.graphs.nodes.hitl import compliance_branch, create_approval, decision_branch, wait_for_decision
from app.graphs.nodes.llm_nodes import compliance_review, generate_episodes, generate_outline
from app.graphs.state import ProjectState

logger = logging.getLogger("gleamdream.graphs.script")

# 进程级编译图缓存（结构固定 + checkpointer 单例，复用避免重复编译）
_compiled: CompiledStateGraph | None = None


def build_script_graph() -> StateGraph:
    """构建编剧子图（未编译，注入 checkpointer 后 compile）。

    Returns:
        装配好节点与条件边的 StateGraph 实例。

    Raises:
        无（节点函数为纯装配引用）。
    """
    g: StateGraph = StateGraph(ProjectState)
    g.add_node("billing_precheck", billing_precheck)
    g.add_node("generate_outline", generate_outline)
    g.add_node("generate_episodes", generate_episodes)
    g.add_node("compliance_review", compliance_review)
    g.add_node("split_episodes_hook", split_episodes_hook)
    g.add_node("create_approval", create_approval)
    g.add_node("wait_for_decision", wait_for_decision)
    g.add_node("apply_human_edit", apply_human_edit)
    g.add_node("finalize_approve", finalize_approve)

    g.add_edge(START, "billing_precheck")
    g.add_edge("billing_precheck", "generate_outline")
    g.add_edge("generate_outline", "generate_episodes")
    g.add_edge("generate_episodes", "compliance_review")
    g.add_conditional_edges(
        "compliance_review",
        compliance_branch,
        {"revise": "generate_episodes", "split": "split_episodes_hook"},
    )
    g.add_edge("split_episodes_hook", "create_approval")
    g.add_edge("create_approval", "wait_for_decision")
    g.add_conditional_edges(
        "wait_for_decision",
        decision_branch,
        {
            "approve": "finalize_approve",
            "reject": "generate_episodes",
            "edit": "apply_human_edit",
        },
    )
    g.add_edge("apply_human_edit", "split_episodes_hook")
    g.add_edge("finalize_approve", END)
    return g


async def get_compiled_script_graph() -> CompiledStateGraph:
    """获取编译后的编剧子图单例（惰性装配 checkpointer，幂等）。

    首次调用时初始化 AsyncPostgresSaver（含 setup 建框架表）并编译图；
    之后复用同一编译实例（图结构不可变）。若缓存实例绑定的
    checkpointer 与当前事件循环不一致（checkpointer 因跨循环已重建），
    重新编译以持有新 saver——编译为纯内存操作，开销可忽略。

    Returns:
        编译后、checkpointer 绑定当前事件循环的编剧子图。

    Raises:
        Exception: checkpointer 初始化失败（数据库不可达等）向上传播。
    """
    global _compiled
    saver = await get_checkpointer()
    if _compiled is not None and getattr(_compiled.checkpointer, "loop", None) is saver.loop:
        return _compiled
    _compiled = build_script_graph().compile(checkpointer=saver)
    logger.info("编剧子图编译完成（checkpointer=AsyncPostgresSaver, thread_id=project_id）")
    return _compiled
