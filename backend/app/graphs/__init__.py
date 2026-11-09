"""LangGraph 多智能体编排包（Task 6a · 编排引擎核心 + 编剧子图）。

导出内容：
- :class:`app.graphs.state.ProjectState`：编剧子图全局状态；
- :func:`app.graphs.script_graph.build_script_graph`：编剧子图装配（未编译）；
- :func:`app.graphs.script_graph.get_compiled_script_graph`：编译图单例
  （含 AsyncPostgresSaver 检查点）；
- :mod:`app.graphs.checkpointer`：Postgres 检查点装配；
- :mod:`app.graphs.contracts`：Agent 输出契约（pydantic）；
- :mod:`app.graphs.prompts`：五段式提示词（中文）；
- :mod:`app.graphs.nodes`：图节点（确定性/LLM/HITL）。

运行入口在 :mod:`app.services.orchestration_service`（后台任务消费
``graph.astream``，HITL 经 ``Command(resume=...)`` 恢复）。
"""

from app.graphs.checkpointer import (
    checkpointer_ready,
    close_checkpointer,
    delete_project_checkpoints,
    get_checkpointer,
    graph_config,
)
from app.graphs.contracts import (
    ComplianceReport,
    EpisodeScript,
    Outline,
    WriteTodos,
    episodes_to_structure,
)
from app.graphs.script_graph import build_script_graph, get_compiled_script_graph
from app.graphs.state import ProjectState

__all__ = [
    "ProjectState",
    "build_script_graph",
    "get_compiled_script_graph",
    "get_checkpointer",
    "close_checkpointer",
    "delete_project_checkpoints",
    "checkpointer_ready",
    "graph_config",
    "Outline",
    "EpisodeScript",
    "WriteTodos",
    "ComplianceReport",
    "episodes_to_structure",
]
