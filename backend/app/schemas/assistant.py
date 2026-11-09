"""AI 创作助手请求 / 响应契约（PRD V6.4 第 6 章）。

约定：
- 面板消息与动作执行请求均 ``extra="forbid"``，避免静默忽略拼写错误；
- 动作执行必须显式 ``confirm=true``，防止“点开即执行”绕过 HITL；
- LLM 输出使用独立的宽松模型（``extra="ignore"``），非法字段由服务层清洗。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 助手可应答的 Agent 标识（对齐 PRD 6.2 Agent 角色总表 + general 兜底）
AgentKey = Literal[
    "screenwriter",
    "parser",
    "art_director",
    "director",
    "voice",
    "producer",
    "qc",
    "compliance",
    "post",
    "distributor",
    "general",
]

#: 助手可执行的动作白名单（渲染/导出/分发/审批/预算修改永久不开放）
ActionType = Literal[
    "script_generate",
    "script_rewrite",
    "script_expand",
    "script_continue",
    "storyboard_generate",
    "line_update",
    "continuity_check",
]

#: 动作状态
ActionStatus = Literal[
    "pending",
    "executing",
    "accepted",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
]


class AssistantMessageRequest(BaseModel):
    """发送助手消息请求体（POST /projects/{project_id}/assistant/messages）。

    Attributes:
        content: 用户消息（1~2000 字）。
        episode_no: 当前聚焦集号（≥1）。
        client_message_id: 客户端消息 ID（1~64 字，幂等键）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=2000, description="用户消息")
    episode_no: int = Field(default=1, ge=1, description="聚焦集号")
    client_message_id: str = Field(
        min_length=1, max_length=64, description="客户端消息 ID（幂等键）"
    )


class AssistantActionExecuteRequest(BaseModel):
    """确认执行助手动作请求体。

    Attributes:
        confirm: 必须为显式 True，缺失或 false 一律 10001。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True] = Field(description="显式确认执行")


class AssistantProposedAction(BaseModel):
    """LLM 产出的动作提议（宽松校验，服务层再做白名单与参数裁剪）。

    Attributes:
        action_type: 动作类型（须命中白名单）。
        title: 卡片标题（≤80 字）。
        summary: 卡片摘要（≤300 字）。
        params: 服务端动作参数。
        risk: 风险提示（可空，≤200 字）。
    """

    model_config = ConfigDict(extra="ignore")

    action_type: str = Field(default="", description="动作类型")
    title: str = Field(default="", description="卡片标题")
    summary: str = Field(default="", description="卡片摘要")
    params: dict[str, Any] = Field(default_factory=dict, description="动作参数")
    risk: str | None = Field(default=None, description="风险提示")


class AssistantAnswerPayload(BaseModel):
    """LLM 回答输出契约（JSON 模式）。

    Attributes:
        agent_key: 应答 Agent 标识。
        answer: 回答正文（≤4000 字）。
        suggestions: 后续建议（≤3 条）。
        proposed_actions: 动作提议（≤2 条）。
        risk_notes: 风险提示（≤3 条）。
    """

    model_config = ConfigDict(extra="ignore")

    agent_key: str = Field(default="general", description="应答 Agent")
    answer: str = Field(default="", description="回答正文")
    suggestions: list[str] = Field(default_factory=list, description="后续建议")
    proposed_actions: list[AssistantProposedAction] = Field(
        default_factory=list, description="动作提议"
    )
    risk_notes: list[str] = Field(default_factory=list, description="风险提示")


class AssistantSelfCheckPayload(BaseModel):
    """生成类回答的独立质检输出契约。

    Attributes:
        passed: 质检是否通过。
        issues: 问题清单（≤3 条）。
        revised_answer: 修正后的回答（通过时可为空）。
    """

    model_config = ConfigDict(extra="ignore")

    passed: bool = Field(default=True, description="是否通过")
    issues: list[str] = Field(default_factory=list, description="问题清单")
    revised_answer: str = Field(default="", description="修正回答")
