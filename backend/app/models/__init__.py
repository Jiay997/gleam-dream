"""ORM 模型汇总模块。

按域导入全部 68 张表模型（对齐《02 数据库设计文档》§5 与 §13.5 V4.0 增量、
《00 契约》§5 实体清单），供 Alembic autogenerate（依赖 ``Base.metadata``）
与业务层统一导入使用。

V4.0 新增表（11 张）：``script_runs`` / ``script_template_refs`` /
``export_presets`` / ``script_export_logs`` / ``storyboard_templates`` /
``director_notes`` / ``storyboard_ops`` / ``voice_bindings`` /
``subtitle_lines`` / ``mix_profiles`` / ``render_batches``（见各域模块）。

用法::

    from app.models import Project, Shot, RenderTask  # noqa: F401
    # 或
    from app.models import *  # noqa: F401,F403
"""

from __future__ import annotations

from app.models.analytics import AnalyticsEvent
from app.models.assistant import (
    AssistantAction,
    AssistantConversation,
    AssistantMessage,
)
from app.models.asset import (
    Asset,
    AssetVersion,
    FaceAuthorization,
    FaceEmbedding,
    MediaFile,
    TtsVoice,
    VoiceBinding,
)
from app.models.billing import (
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    Order,
    Payment,
    Plan,
    PriceRule,
    Subscription,
)
from app.models.collab import (
    AgentRun,
    ApprovalRequest,
    Notification,
    PromptTemplate,
    StylePreset,
    SystemConfig,
)
from app.models.distribution import (
    DistributionRecord,
    ExportJob,
    ExportPreset,
    PlatformAccount,
)
from app.models.iam import (
    AuditLog,
    OperationLog,
    ProjectMember,
    Team,
    TeamMember,
    User,
    UserIdentity,
)
from app.models.post import (
    BgmTrack,
    MixProfile,
    SubtitleLine,
    SubtitleStyle,
    Timeline,
    TimelineClip,
)
from app.models.project import IdempotencyKey, Project, ProjectFile, ProjectSnapshot
from app.models.quality import ComplianceFlag, ModerationRecord, QcReport
from app.models.render import (
    AiCallLog,
    ModelRegistry,
    ModelRoute,
    RenderBatch,
    RenderOutput,
    RenderTask,
)
from app.models.script import (
    Document,
    Episode,
    ParseConfidenceItem,
    Scene,
    Script,
    ScriptExportLog,
    ScriptLine,
    ScriptRun,
    ScriptTemplateRef,
)
from app.models.storyboard import (
    DirectorNote,
    Shot,
    ShotVersion,
    Storyboard,
    StoryboardLayout,
    StoryboardOp,
    StoryboardTemplate,
)
from app.models.v6 import (
    CgEdge,
    CgLayout,
    CgNode,
    CharacterComposition,
    CompositionGraph,
    CompositionSwitchLog,
    ConfigAudit,
    ConfigVersion,
    ContinuityCheck,
    ContinuityFact,
    ContinuitySnapshot,
    HumanFeedback,
    IdentityProfile,
    LogicWaiver,
    NegativeExample,
    ParamConfig,
    ParamDef,
    PhotoUpload,
    RenderPlan,
    ShotLineBinding,
    StoryBibleCharacter,
    StoryBibleProp,
    StoryBibleScene,
    VoiceClip,
    VoiceDriftRecord,
    VoiceParam,
    VoiceTrack,
)

__all__ = [
    # 域 A · IAM
    "User",
    "UserIdentity",
    "Team",
    "TeamMember",
    "ProjectMember",
    "AuditLog",
    "OperationLog",
    # 域 B · Billing
    "Plan",
    "Subscription",
    "CreditAccount",
    "CreditTransaction",
    "CreditFreeze",
    "Order",
    "Payment",
    "PriceRule",
    # 域 C · Project
    "Project",
    "ProjectSnapshot",
    "ProjectFile",
    "IdempotencyKey",
    # 域 D · Script（含 V4.0 新表）
    "Document",
    "Script",
    "Episode",
    "Scene",
    "ScriptLine",
    "ParseConfidenceItem",
    "ScriptRun",
    "ScriptTemplateRef",
    "ScriptExportLog",
    # 域 E · Asset（含 V4.0 新表）
    "Asset",
    "AssetVersion",
    "MediaFile",
    "FaceEmbedding",
    "FaceAuthorization",
    "TtsVoice",
    "VoiceBinding",
    # 域 F · Storyboard（含 V4.0 新表）
    "Storyboard",
    "Shot",
    "ShotVersion",
    "StoryboardLayout",
    "DirectorNote",
    "StoryboardOp",
    "StoryboardTemplate",
    # 域 G · Render（含 V4.0 新表）
    "ModelRegistry",
    "ModelRoute",
    "RenderBatch",
    "RenderTask",
    "RenderOutput",
    "AiCallLog",
    # 域 H · QC
    "QcReport",
    "ComplianceFlag",
    "ModerationRecord",
    # 域 I · Post（含 V4.0 新表）
    "Timeline",
    "TimelineClip",
    "SubtitleStyle",
    "SubtitleLine",
    "MixProfile",
    "BgmTrack",
    # 域 J · Distribution（含 V4.0 新表）
    "ExportJob",
    "ExportPreset",
    "PlatformAccount",
    "DistributionRecord",
    # 域 K · Collab
    "ApprovalRequest",
    "Notification",
    "AgentRun",
    "StylePreset",
    "PromptTemplate",
    "SystemConfig",
    # 域 L · Analytics（观测埋点，验收报告 7.1 P0 埋点最小集）
    "AnalyticsEvent",
    # 域 N · AI 创作助手（PRD V6.4 第 6 章）
    "AssistantConversation",
    "AssistantMessage",
    "AssistantAction",
    # 域 M · V6 增量（F9/F10/F11/5A/F3.14，共 27 张表）
    "ContinuityFact",
    "StoryBibleCharacter",
    "StoryBibleScene",
    "StoryBibleProp",
    "ContinuitySnapshot",
    "ContinuityCheck",
    "LogicWaiver",
    "HumanFeedback",
    "NegativeExample",
    "VoiceParam",
    "VoiceTrack",
    "VoiceClip",
    "VoiceDriftRecord",
    "ShotLineBinding",
    "CompositionGraph",
    "CgNode",
    "CgEdge",
    "CgLayout",
    "RenderPlan",
    "CompositionSwitchLog",
    "ParamDef",
    "ParamConfig",
    "ConfigVersion",
    "ConfigAudit",
    "PhotoUpload",
    "IdentityProfile",
    "CharacterComposition",
]
