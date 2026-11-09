"""图节点子包。

模块划分：
- :mod:`app.graphs.nodes.deterministic`：确定性节点（计费守卫/落库/
  锁定结算/人工编辑）与幂等、AgentRun 辅助设施；
- :mod:`app.graphs.nodes.llm_nodes`：LLM Agent 节点（大纲/逐集/审核，
  结构化输出 + 自修循环）；
- :mod:`app.graphs.nodes.hitl`：HITL 节点（审批单双写 + interrupt）。

职责边界（PRD §6.1）：编排与执行分离——状态流转、计费、权限、中断
由确定性节点承担，LLM 只在"需要智能"的节点运行且绝不接触资金。
"""

from app.graphs.nodes.hitl import create_approval, wait_for_decision
from app.graphs.nodes.deterministic import (
    apply_human_edit,
    billing_precheck,
    finalize_approve,
    split_episodes_hook,
)
from app.graphs.nodes.llm_nodes import (
    compliance_review,
    generate_episodes,
    generate_outline,
)

__all__ = [
    "billing_precheck",
    "generate_outline",
    "generate_episodes",
    "compliance_review",
    "split_episodes_hook",
    "create_approval",
    "wait_for_decision",
    "apply_human_edit",
    "finalize_approve",
]
