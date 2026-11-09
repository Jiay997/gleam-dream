"""提示词子包：各 Agent 系统提示词（五段式，中文）。

模块划分：
- :mod:`app.graphs.prompts.screenwriter`：编剧 Agent（大纲/逐集/局部改写）；
- :mod:`app.graphs.prompts.compliance`：审核 Agent（内容安全扫描）。

统一约定（PRD 附录 B / 技术方案 §5.1）：每个提示词由五个固定段落组成，
顺序不可变——① 角色与目标 ② 项目上下文 ③ 工作区约定 ④ 预算与边界
⑤ 输出契约。
"""

from app.graphs.prompts.screenwriter import (
    build_episode_prompt,
    build_outline_prompt,
)
from app.graphs.prompts.compliance import build_compliance_prompt

__all__ = [
    "build_outline_prompt",
    "build_episode_prompt",
    "build_compliance_prompt",
]
