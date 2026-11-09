"""V6.4 增量请求模型（F9 / F4.13 / F10 / F11 / 5A / F3.14）。

约定：
- 所有写请求 ``extra="forbid"``，避免拼写错误静默忽略；
- 文本长度、数值边界与 PRD 验收口径保持一致（如理由 ≥10 字、风格化 0~100）；
- 枚举使用 ``Literal``，由 FastAPI 统一转换为 10001 参数校验失败。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 制作流程模式
PipelineMode = Literal["voice_first", "classic"]
#: 配音轨状态
VoiceTrackStatus = Literal[
    "not_started", "previewing_done", "voice_ready", "voice_locked", "voice_drift"
]
#: 对齐来源
AlignmentSource = Literal["whisperx", "tts_timestamp", "human", "none"]
#: 编排节点类型
NodeType = Literal[
    "clip", "group", "title", "audio_overlay", "compose", "branch", "start", "end"
]
#: 视图类型
ViewType = Literal["track", "node"]
#: 转场类型
Transition = Literal["cut", "dissolve", "fade", "wipe", "slide"]


# =====================================================================
# F9 配音轨 / F2.12 台词清单 / F4.13 台词绑定
# =====================================================================


class VoiceParamConfirmRequest(BaseModel):
    """确认角色音色参数（样音试听通过后调用）。

    Attributes:
        voice_id: 音色 ID（tts_voices.id）。
        rate: 语速倍率（0.5~2.0）。
        pitch: 音调偏移（-6~+6）。
        emotion_default: 默认情绪标签。
        sample_audio_key: 已试听样音对象键（可空）。
        sample_duration_ms: 样音时长（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    voice_id: int | None = Field(default=None, description="音色 ID")
    rate: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率")
    pitch: int = Field(default=0, ge=-6, le=6, description="音调偏移")
    emotion_default: str | None = Field(default=None, max_length=32, description="默认情绪")
    sample_audio_key: str | None = Field(default=None, max_length=255, description="样音对象键")
    sample_duration_ms: int | None = Field(default=None, ge=0, description="样音时长")


class VoiceSampleRequest(BaseModel):
    """生成角色样音请求。

    Attributes:
        character_asset_id: 角色资产 ID。
        text: 试听文本（默认由服务端取角色首句台词）。
        rate: 语速倍率。
        pitch: 音调偏移。
    """

    model_config = ConfigDict(extra="forbid")

    character_asset_id: int = Field(description="角色资产 ID")
    text: str | None = Field(default=None, max_length=200, description="试听文本")
    rate: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率")
    pitch: int = Field(default=0, ge=-6, le=6, description="音调偏移")


class VoiceGenerateRequest(BaseModel):
    """批量配音请求（逐句生成，可指定子集）。

    Attributes:
        line_ids: 指定台词 ID 列表（空表示整集全部台词）。
        force: 是否强制重配（忽略缓存与已生成音频）。
    """

    model_config = ConfigDict(extra="forbid")

    line_ids: list[int] = Field(default_factory=list, description="台词 ID 列表")
    force: bool = Field(default=False, description="强制重配")


class VoiceTrackUpdateRequest(BaseModel):
    """配音轨参数调整（句间气口）。

    Attributes:
        gap_ms: 句间气口（0~1000ms）。
        pipeline_mode: 项目制作流程模式。
    """

    model_config = ConfigDict(extra="forbid")

    gap_ms: int | None = Field(default=None, ge=0, le=1000, description="句间气口")
    pipeline_mode: PipelineMode | None = Field(default=None, description="制作流程")


class VoiceGateDecisionRequest(BaseModel):
    """关口 V 审批决策。

    Attributes:
        decision: approve/reject。
        comment: 备注（reject 必填）。
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"] = Field(description="审批结论")
    comment: str | None = Field(default=None, max_length=500, description="备注")


class VoiceClipRegenerateRequest(BaseModel):
    """单句重配请求。

    Attributes:
        rate: 临时语速倍率（空表示沿用角色参数）。
        pitch: 临时音调偏移。
        reason: 重配原因。
    """

    model_config = ConfigDict(extra="forbid")

    rate: float | None = Field(default=None, ge=0.5, le=2.0, description="语速倍率")
    pitch: int | None = Field(default=None, ge=-6, le=6, description="音调偏移")
    reason: str | None = Field(default=None, max_length=128, description="重配原因")


class VoiceDriftDecisionRequest(BaseModel):
    """时长漂移处理决策。

    Attributes:
        action: apply/dismiss。
        level: 指定处理级别（可空，服务端按阈值给出建议）。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["apply", "dismiss"] = Field(description="处理动作")
    level: Literal["absorb", "recalc", "regenerate"] | None = Field(
        default=None, description="处理级别"
    )


class LineUpdateRequest(BaseModel):
    """台词编辑（F2.12）。

    Attributes:
        content: 台词文本。
        speaker_name: 说话人。
        character_asset_id: 绑定角色资产（空表示不修改）。
        emotion_tag: 情绪标签。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=2000, description="台词文本")
    speaker_name: str | None = Field(default=None, max_length=64, description="说话人")
    character_asset_id: int | None = Field(default=None, description="角色资产 ID")
    emotion_tag: str | None = Field(default=None, max_length=32, description="情绪标签")


class LineSplitRequest(BaseModel):
    """超长台词拆分（插入一条新台词并重排后续行号）。

    Attributes:
        position: 拆分位置（字符索引，1~len-1）。
        content: 自定义拆分段文本（空表示按位置切分）。
    """

    model_config = ConfigDict(extra="forbid")

    position: int | None = Field(default=None, ge=1, description="字符位置")
    content: str | None = Field(default=None, min_length=1, max_length=2000, description="拆分段文本")


class LineEstimateRequest(BaseModel):
    """台词时长预估请求。

    Attributes:
        content: 待估文本（空表示按当前台词内容重算）。
        rate: 语速倍率。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, max_length=2000, description="待估文本")
    rate: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率")


class ShotLineBindingItem(BaseModel):
    """单条镜头台词绑定项。"""

    model_config = ConfigDict(extra="forbid")

    line_id: int = Field(description="台词 ID")
    offset_ms: int = Field(default=0, ge=0, description="镜头内偏移")
    order_index: int = Field(default=0, ge=0, description="镜头内顺序")


class ShotLineBindingsRequest(BaseModel):
    """重建镜头台词绑定（全量覆盖语义）。

    Attributes:
        bindings: 绑定项数组（按顺序写入）。
    """

    model_config = ConfigDict(extra="forbid")

    bindings: list[ShotLineBindingItem] = Field(default_factory=list, description="绑定项")


class ShotDurationRecalcRequest(BaseModel):
    """镜头音频驱动时长重算请求。

    Attributes:
        force_manual: 是否覆盖手工锁定的时长。
        max_shot_ms: 单镜头上限（超出需拆分）。
    """

    model_config = ConfigDict(extra="forbid")

    force_manual: bool = Field(default=False, description="覆盖手工锁定")
    max_shot_ms: int = Field(default=15000, ge=1000, le=15000, description="单镜头上限")


# =====================================================================
# F10 合成编排
# =====================================================================


class NodeCreateRequest(BaseModel):
    """新增编排节点。

    Attributes:
        node_type: 节点类型。
        node_key: 稳定键（可空，服务端生成）。
        ref_id: 引用 ID。
        ref_kind: 引用类型。
        params: 节点参数。
        label: 展示标签。
        parent_id: 父分组节点。
        ui_pos: 坐标。
        order_hint: 排序。
    """

    model_config = ConfigDict(extra="forbid")

    node_type: NodeType = Field(description="节点类型")
    node_key: str | None = Field(default=None, max_length=64, description="节点键")
    ref_id: str | None = Field(default=None, max_length=64, description="引用 ID")
    ref_kind: str | None = Field(default=None, max_length=16, description="引用类型")
    params: dict[str, Any] = Field(default_factory=dict, description="节点参数")
    label: str | None = Field(default=None, max_length=255, description="标签")
    parent_id: int | None = Field(default=None, description="父节点 ID")
    ui_pos: dict[str, Any] | None = Field(default=None, description="坐标")
    order_hint: int = Field(default=0, ge=0, description="排序")


class NodeUpdateRequest(BaseModel):
    """编辑编排节点（部分更新）。

    Attributes:
        params: 节点参数（整体替换）。
        label: 展示标签。
        ui_pos: 坐标。
        order_hint: 排序。
        parent_id: 父节点。
        status: normal/stale/disabled。
    """

    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] | None = Field(default=None, description="节点参数")
    label: str | None = Field(default=None, max_length=255, description="标签")
    ui_pos: dict[str, Any] | None = Field(default=None, description="坐标")
    order_hint: int | None = Field(default=None, ge=0, description="排序")
    parent_id: int | None = Field(default=None, description="父节点")
    status: Literal["normal", "stale", "disabled"] | None = Field(default=None, description="状态")


class EdgeCreateRequest(BaseModel):
    """新增编排边。

    Attributes:
        from_node_id: 起点节点 ID。
        to_node_id: 终点节点 ID。
        transition: 转场。
        duration_ms: 转场时长。
        order_hint: 排序。
    """

    model_config = ConfigDict(extra="forbid")

    from_node_id: int = Field(description="起点节点 ID")
    to_node_id: int = Field(description="终点节点 ID")
    transition: Transition = Field(default="cut", description="转场")
    duration_ms: int = Field(default=0, ge=0, le=3000, description="转场时长")
    order_hint: int = Field(default=0, ge=0, description="排序")


class SwitchViewRequest(BaseModel):
    """视图切换请求（轨道 ↔ 节点）。

    Attributes:
        to_view: 目标视图。
    """

    model_config = ConfigDict(extra="forbid")

    to_view: ViewType = Field(description="目标视图")


class CompileRequest(BaseModel):
    """编译请求（生成不可变渲染计划）。

    Attributes:
        preview: 是否试渲计划（不计入成片版本）。
        force: 图未变更时是否强制新建计划。
    """

    model_config = ConfigDict(extra="forbid")

    preview: bool = Field(default=False, description="是否试渲")
    force: bool = Field(default=False, description="强制重新编译")


class ApplyOrderRequest(BaseModel):
    """把主路径节点顺序回写分镜板。

    Attributes:
        confirm_impacts: 是否确认接受影响的镜头列表。
    """

    model_config = ConfigDict(extra="forbid")

    confirm_impacts: bool = Field(default=False, description="确认影响")


# =====================================================================
# F11 参数中心
# =====================================================================


class ParamUpdateRequest(BaseModel):
    """保存参数变更（单次可多键）。

    Attributes:
        changes: 参数键 → 新值。
        reason: 危险参数变更理由（≥10 字）。
        scope: 作用域 project/episode/user_pref。
        scope_id: 作用域标识（episode 模式填集号）。
    """

    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any] = Field(min_length=1, description="参数键→新值")
    reason: str | None = Field(default=None, max_length=500, description="变更理由")
    scope: Literal["project", "episode", "user_pref"] = Field(default="project", description="作用域")
    scope_id: str = Field(default="0", max_length=32, description="作用域标识")


class ParamRollbackRequest(BaseModel):
    """回滚到指定配置版本。

    Attributes:
        version_no: 目标版本号。
        reason: 回滚理由。
    """

    model_config = ConfigDict(extra="forbid")

    version_no: int = Field(ge=1, description="目标版本号")
    reason: str | None = Field(default=None, max_length=500, description="回滚理由")


class ParamRestoreRequest(BaseModel):
    """恢复默认参数。

    Attributes:
        param_keys: 指定参数键（空表示全部恢复默认）。
        reason: 操作理由。
    """

    model_config = ConfigDict(extra="forbid")

    param_keys: list[str] = Field(default_factory=list, description="参数键列表")
    reason: str | None = Field(default=None, max_length=500, description="操作理由")


class ParamReassessRequest(BaseModel):
    """按当前参数重评估历史结果（不自动改写，返回对照）。

    Attributes:
        ep_no: 限定集号（空表示项目全部）。
        target: 重评估对象 shot/script/composition。
    """

    model_config = ConfigDict(extra="forbid")

    ep_no: int | None = Field(default=None, ge=1, description="集号")
    target: Literal["shot", "script", "composition"] = Field(default="shot", description="对象")


class ParamAdoptRequest(BaseModel):
    """采纳重评估结论（写回质检裁决）。"""

    model_config = ConfigDict(extra="forbid")

    check_ids: list[int] = Field(min_length=1, description="待采纳的校验结果 ID")


# =====================================================================
# 5A Story Bible / 连续性 / 结构化反馈 / F2.13 逻辑规则
# =====================================================================


class ContinuityFactUpsertRequest(BaseModel):
    """写入/修正场次连续性事实。

    Attributes:
        facts: 事实主体。
        source: 产出方式。
    """

    model_config = ConfigDict(extra="forbid")

    facts: dict[str, Any] = Field(description="事实主体")
    source: Literal["agent", "inferred", "edit"] = Field(default="edit", description="产出方式")


class FactRebuildRequest(BaseModel):
    """从剧本重建连续性事实。

    Attributes:
        ep_no: 集号（空表示全部集）。
        overwrite: 是否覆盖人工编辑过的版本。
    """

    model_config = ConfigDict(extra="forbid")

    ep_no: int | None = Field(default=None, ge=1, description="集号")
    overwrite: bool = Field(default=False, description="覆盖人工事实")


class StoryBibleCharacterUpsertRequest(BaseModel):
    """Story Bible 角色档案写入。

    Attributes:
        char_key: 角色稳定键。
        name: 角色名。
        char_asset_id: 角色资产 ID。
        base_look: 基础造型。
        body_desc: 体态描述。
        voice_id: 音色 ID。
        outfits: 服装变体。
        current_state: 角色状态。
        extra: 扩展字段。
    """

    model_config = ConfigDict(extra="forbid")

    char_key: str = Field(min_length=1, max_length=64, description="角色稳定键")
    name: str = Field(min_length=1, max_length=128, description="角色名")
    char_asset_id: int | None = Field(default=None, description="角色资产 ID")
    base_look: str | None = Field(default=None, max_length=5000, description="基础造型")
    body_desc: str | None = Field(default=None, max_length=5000, description="体态描述")
    voice_id: int | None = Field(default=None, description="音色 ID")
    outfits: list[dict[str, Any]] = Field(default_factory=list, description="服装变体")
    current_state: Literal["alive", "dead", "offstage", "unknown"] = Field(
        default="alive", description="角色状态"
    )
    extra: dict[str, Any] | None = Field(default=None, description="扩展字段")


class StoryBibleSceneUpsertRequest(BaseModel):
    """Story Bible 场景档案写入。"""

    model_config = ConfigDict(extra="forbid")

    location: str | None = Field(default=None, max_length=255, description="地点")
    layout_ref: str | None = Field(default=None, max_length=255, description="空间布局引用")
    key_points: list[dict[str, Any]] = Field(default_factory=list, description="关键点位")
    time_of_day: str | None = Field(default=None, max_length=16, description="时段")
    elapsed_minutes: int | None = Field(default=None, ge=0, description="剧情内分钟")
    weather: str | None = Field(default=None, max_length=64, description="天气")
    lighting_base: str | None = Field(default=None, max_length=255, description="光线基线")
    is_flashback: bool = Field(default=False, description="是否闪回")


class StoryBiblePropUpsertRequest(BaseModel):
    """Story Bible 道具档案写入。"""

    model_config = ConfigDict(extra="forbid")

    prop_key: str = Field(min_length=1, max_length=64, description="道具稳定键")
    name: str = Field(min_length=1, max_length=128, description="道具名")
    prop_asset_id: int | None = Field(default=None, description="道具资产 ID")
    state_machine: list[dict[str, Any]] = Field(default_factory=list, description="状态链")
    holder_by_scene: dict[str, str] = Field(default_factory=dict, description="持有人映射")
    ref_images: list[str] = Field(default_factory=list, description="参考图")
    current_state: str | None = Field(default=None, max_length=64, description="当前状态")


class ContinuityRunRequest(BaseModel):
    """运行连续性校验（L1 + L2）。

    Attributes:
        ep_no: 集号。
        shot_ids: 指定镜头（空表示整集）。
        use_bible: 是否启用 Story Bible 期望状态比对。
    """

    model_config = ConfigDict(extra="forbid")

    ep_no: int = Field(ge=1, description="集号")
    shot_ids: list[int] = Field(default_factory=list, description="镜头 ID 列表")
    use_bible: bool = Field(default=True, description="启用 Bible 比对")


class LogicCheckRequest(BaseModel):
    """运行 F2.13 逻辑/连续性规则检查。

    Attributes:
        ep_no: 集号。
        rule_codes: 指定规则（空表示全部）。
    """

    model_config = ConfigDict(extra="forbid")

    ep_no: int = Field(ge=1, description="集号")
    rule_codes: list[str] = Field(default_factory=list, description="规则编号")


class LogicWaiverRequest(BaseModel):
    """逻辑规则豁免（忽略并留痕）。

    Attributes:
        rule_code: 规则编号。
        target_ref: 定位引用。
        reason: 理由（≥10 字）。
    """

    model_config = ConfigDict(extra="forbid")

    rule_code: Literal["LR-1", "LR-2", "LR-3", "LR-4", "LR-5", "LR-6"] = Field(description="规则编号")
    target_ref: str = Field(min_length=1, max_length=128, description="定位引用")
    reason: str = Field(min_length=10, max_length=500, description="豁免理由")


class HumanFeedbackRequest(BaseModel):
    """结构化反馈（维度化 reject 表单）。

    Attributes:
        target_type: 对象类型。
        target_id: 对象 ID。
        dimensions: 维度数组（≥1）。
        scope: 作用域。
        expectation: 期望枚举。
        expectation_text: 期望补充。
        keep_items: 保留项。
        text: 补充说明（≤200 字）。
        source_gate: 来源关口。
    """

    model_config = ConfigDict(extra="forbid")

    target_type: Literal["shot", "line", "episode", "final_cut"] = Field(description="对象类型")
    target_id: int = Field(description="对象 ID")
    dimensions: list[str] = Field(min_length=1, description="维度数组")
    scope: dict[str, Any] = Field(default_factory=dict, description="作用域")
    expectation: str | None = Field(default=None, max_length=32, description="期望枚举")
    expectation_text: str | None = Field(default=None, max_length=500, description="期望文本")
    keep_items: list[str] = Field(default_factory=list, description="保留项")
    text: str | None = Field(default=None, max_length=200, description="补充说明")
    source_gate: Literal["gate1", "gateV", "gate2", "gate3", "gate4"] = Field(description="来源关口")


class NegativeExampleRequest(BaseModel):
    """写入反例库（错-改-对）。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(min_length=1, max_length=32, description="维度")
    character_asset_id: int | None = Field(default=None, description="角色资产 ID")
    scene_ref: str | None = Field(default=None, max_length=128, description="场次引用")
    issue_desc: str = Field(min_length=1, max_length=2000, description="问题描述")
    evidence_ref: dict[str, Any] | None = Field(default=None, description="证据引用")
    fix_action: str | None = Field(default=None, max_length=128, description="修正动作")
    passed: bool = Field(default=False, description="修正后是否通过")
    source: Literal["reject", "misjudge", "manual_fix"] = Field(default="reject", description="来源")


# =====================================================================
# F3.14 照片合成角色
# =====================================================================


class PhotoUploadRequest(BaseModel):
    """登记一张照片（客户端完成上传后提交元数据与模型分析结果）。

    Attributes:
        batch_key: 批次键（空则服务端生成）。
        file_name: 文件名。
        mime: MIME。
        size_bytes: 文件大小（≤20MB）。
        width: 宽。
        height: 高。
        sha256: 内容指纹。
        declaration: 用途声明。
        authorization_confirmed: 授权确认。
        analysis: 四道闸所需的模型分析结果。
    """

    model_config = ConfigDict(extra="forbid")

    batch_key: str | None = Field(default=None, max_length=32, description="批次键")
    file_name: str = Field(min_length=1, max_length=255, description="文件名")
    mime: str = Field(min_length=3, max_length=64, description="MIME")
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024, description="文件大小（≤20MB）")
    width: int | None = Field(default=None, ge=1, le=20000, description="宽")
    height: int | None = Field(default=None, ge=1, le=20000, description="高")
    sha256: str | None = Field(default=None, min_length=64, max_length=64, description="内容指纹")
    declaration: Literal["self", "authorized_other"] = Field(default="self", description="用途声明")
    authorization_confirmed: bool = Field(default=False, description="授权确认")
    analysis: dict[str, Any] = Field(default_factory=dict, description="模型分析结果")


class IdentityProfileRequest(BaseModel):
    """创建身份档（照片聚类 + 主图指定）。

    Attributes:
        batch_key: 批次键。
        name: 身份档名称。
        main_photo_id: 主图 ID（空则自动选质量最高）。
        photo_ids: 照片 ID 列表（空则该批次全部可用照片）。
    """

    model_config = ConfigDict(extra="forbid")

    batch_key: str = Field(min_length=1, max_length=32, description="批次键")
    name: str = Field(min_length=1, max_length=128, description="身份档名称")
    main_photo_id: int | None = Field(default=None, description="主图 ID")
    photo_ids: list[int] = Field(default_factory=list, description="照片 ID 列表")


class CompositionCreateRequest(BaseModel):
    """发起照片合成角色（M1 身份保持）。

    Attributes:
        profile_id: 身份档 ID。
        style_preset_id: 画风预设 ID。
        stylization: 风格化强度（0~100）。
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: int = Field(description="身份档 ID")
    style_preset_id: int | None = Field(default=None, description="画风预设 ID")
    stylization: int = Field(default=50, ge=0, le=100, description="风格化强度")


class ComposeFromPlanRequest(BaseModel):
    """按编译计划发起合成（F10 / V6.12）。

    Attributes:
        plan_hash: 期望的计划哈希（可选；与当前图不一致时拒绝）。
    """

    model_config = ConfigDict(extra="forbid")

    plan_hash: str | None = Field(
        default=None, max_length=64, description="期望的计划哈希（可选）"
    )
