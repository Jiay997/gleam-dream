"""分镜域 Pydantic Schema 模块（Task 7b）。

定义 /storyboards 与 /shots 路由的请求体模型，以及 ORM -> 响应字典的
序列化函数（统一与 ``success_envelope`` 配套使用）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.storyboard import Shot, Storyboard

# ---- 枚举（与 DB CHECK 约束一致） ----
ShotSizeType = Literal[
    "extreme_wide", "wide", "medium", "close_up", "extreme_close_up", "over_shoulder"
]
CameraMoveType = Literal[
    "static", "pan", "tilt", "dolly_in", "dolly_out", "tracking", "orbit", "handheld", "crane"
]
EmotionTagType = Literal[
    "calm", "joy", "anger", "sadness", "fear", "surprise", "tension", "romance"
]


class StoryboardGenerateRequest(BaseModel):
    """分镜生成发起请求体。

    Attributes:
        episode_no: 集号（≥1，须为当前剧本已存在的分集）。
        pacing_mode: 叙事节奏模式（V4.0，AC-F4-07-1）：快节奏/均衡/慢节奏；
            缺省时读取/沿用项目全剧策略（默认 balanced）。
        template_id: 分镜模板 ID（V4.0，AC-F4-10-1，可空）。
        target_shot_count: 目标镜头数（P1-7：用户显式控制，3~60，可空）。
        target_duration_ms: 目标单集时长（毫秒，10s~10min，可空）。
        max_shot_duration_ms: 单镜头时长上限（毫秒，1s~15s，可空）。
    """

    model_config = ConfigDict(extra="forbid")

    episode_no: int = Field(ge=1, description="集号")
    pacing_mode: Literal["fast", "balanced", "slow"] | None = Field(
        default=None, description="节奏模式 fast/balanced/slow"
    )
    template_id: str | None = Field(default=None, max_length=64, description="分镜模板 ID")
    target_shot_count: int | None = Field(
        default=None, ge=3, le=60, description="目标镜头数（3~60）"
    )
    target_duration_ms: int | None = Field(
        default=None, ge=10_000, le=600_000, description="目标单集时长（毫秒）"
    )
    max_shot_duration_ms: int | None = Field(
        default=None, ge=1_000, le=15_000, description="单镜头时长上限（毫秒）"
    )


class ShotPatchRequest(BaseModel):
    """镜头编辑请求体（PATCH 语义，仅显式传入字段生效）。

    Attributes:
        image_prompt: 关键帧 prompt。
        action_desc: 人物动作描述（视频动态性三要素之一）。
        final_prompt: 最终生成 prompt（可显式覆盖三要素自动组装结果）。
        shot_size: 景别（6 值枚举）。
        camera_move: 运镜（9 值枚举）。
        duration_ms: 时长（毫秒，1000~15000，违反返回 10001）。
        emotion_tag: 情绪标签（8 值枚举，传 null 清除）。
    """

    model_config = ConfigDict(extra="forbid")

    image_prompt: str | None = Field(default=None, max_length=2000, description="关键帧 prompt")
    action_desc: str | None = Field(
        default=None, max_length=2000, description="人物动作描述（动词）"
    )
    final_prompt: str | None = Field(
        default=None, max_length=2000, description="最终生成 prompt"
    )
    shot_size: ShotSizeType | None = Field(default=None, description="景别")
    camera_move: CameraMoveType | None = Field(default=None, description="运镜")
    duration_ms: int | None = Field(
        default=None, ge=1000, le=15000, description="时长（毫秒）"
    )
    emotion_tag: EmotionTagType | None = Field(default=None, description="情绪标签")


class ShotReorderRequest(BaseModel):
    """镜头重排请求体。

    Attributes:
        shot_id: 要移动的镜头 ID。
        insert_before_shot_id: 插入到该镜头之前（与 after 二选一）。
        insert_after_shot_id: 插入到该镜头之后（与 before 二选一）。
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: int = Field(ge=1, description="要移动的镜头 ID")
    insert_before_shot_id: int | None = Field(default=None, ge=1, description="前插锚点镜头")
    insert_after_shot_id: int | None = Field(default=None, ge=1, description="后插锚点镜头")


class ShotAnchorRequest(BaseModel):
    """镜头锚点绑定请求体。

    Attributes:
        character_asset_ids: 出场角色资产 ID 列表（全部须为 locked 状态）。
        scene_asset_id: 场景资产 ID（可空，须为 locked 状态）。
    """

    model_config = ConfigDict(extra="forbid")

    character_asset_ids: list[int] = Field(default_factory=list, description="角色资产 ID 列表")
    scene_asset_id: int | None = Field(default=None, ge=1, description="场景资产 ID")


class StoryboardDecisionRequest(BaseModel):
    """分镜审批决策请求体。

    Attributes:
        action: 决策动作 approve/reject。
        feedback: 反馈原文（reject 时必填且 ≥10 字）。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"] = Field(description="决策动作")
    feedback: str | None = Field(default=None, max_length=2000, description="反馈原文")


class LayoutNode(BaseModel):
    """画布布局节点。

    Attributes:
        shot_id: 镜头 ID。
        x: 画布 X 坐标。
        y: 画布 Y 坐标。
        w: 节点宽度。
        h: 节点高度。
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: int = Field(ge=1, description="镜头 ID")
    x: float = Field(description="X 坐标")
    y: float = Field(description="Y 坐标")
    w: float = Field(ge=0, description="宽度")
    h: float = Field(ge=0, description="高度")


class StoryboardLayoutRequest(BaseModel):
    """画布布局保存请求体（upsert）。

    Attributes:
        nodes: 布局节点列表。
        viewport: 视口状态 {x, y, zoom}（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    nodes: list[LayoutNode] = Field(description="布局节点列表")
    viewport: dict[str, Any] | None = Field(default=None, description="视口状态")


# ---- V4.0 增量请求体 ----


class ShotBatchPatchRequest(BaseModel):
    """批量修改镜头参数请求体（AC-F4-08-2~3，V4.0）。

    Attributes:
        shot_ids: 目标镜头 ID 列表（≥2 个，同一分镜板内）。
        camera_move: 统一运镜（可空，不传则不修改）。
        duration_ms: 统一时长（毫秒，1000~15000，可空）。
        emotion_tag: 统一情绪标签（可空）。
        shot_size: 统一景别（可空）。
        confirm: 是否已确认变更摘要（未确认时返回摘要不执行）。
    """

    model_config = ConfigDict(extra="forbid")

    shot_ids: list[int] = Field(min_length=1, max_length=200, description="镜头 ID 列表")
    camera_move: CameraMoveType | None = Field(default=None, description="统一运镜")
    duration_ms: int | None = Field(default=None, ge=1000, le=15000, description="统一时长")
    emotion_tag: EmotionTagType | None = Field(default=None, description="统一情绪标签")
    shot_size: ShotSizeType | None = Field(default=None, description="统一景别")
    confirm: bool = Field(default=False, description="是否已确认变更摘要")


class ShotCopyRequest(BaseModel):
    """复制镜头请求体（AC-F4-08-1，V4.0）。

    Attributes:
        mode: 粘贴位置 after(选中镜头后) / tail(集尾)。
        anchor_shot_id: after 模式下的锚点镜头 ID。
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["after", "tail"] = Field(default="tail", description="粘贴位置")
    anchor_shot_id: int | None = Field(default=None, ge=1, description="锚点镜头 ID")


class FrameRedrawRequest(BaseModel):
    """草稿静帧重出请求体（AC-F4-03-3，V4.0）。

    Attributes:
        feedback: 重出反馈（≤50 字）。
        mode: compose=本地合成整幅要素帧（0 成本）；generate=文生图（≤3 次）。
    """

    model_config = ConfigDict(extra="forbid")

    feedback: str | None = Field(default=None, max_length=50, description="重出反馈（≤50 字）")
    mode: Literal["compose", "generate"] = Field(
        default="compose",
        description="compose=本地合成整幅要素帧（0 成本）；generate=文生图（≤3 次）",
    )


class FrameComposeRequest(BaseModel):
    """整板批量合成首帧请求体。

    Attributes:
        shot_ids: 指定镜头 ID 列表；为空表示整板全部镜头。
    """

    model_config = ConfigDict(extra="forbid")

    shot_ids: list[int] | None = Field(default=None, description="指定镜头；空=整板")


class StoryboardRollbackRequest(BaseModel):
    """画布操作历史回退请求体（AC-F4-09-2，V4.0）。

    Attributes:
        storyboard_id: 分镜板 ID。
        seq: 回退目标步骤序号（回退到该步结束时状态）。
        confirm: 是否二次确认。
    """

    model_config = ConfigDict(extra="forbid")

    storyboard_id: int = Field(ge=1, description="分镜板 ID")
    seq: int = Field(ge=1, description="回退目标步骤序号")
    confirm: bool = Field(default=False, description="是否二次确认")


class DirectorNoteRequest(BaseModel):
    """导演策略（节奏模式）写入请求体（AC-F4-07-1~3，V4.0）。

    Attributes:
        pacing_mode: 节奏模式 fast/balanced/slow。
        episode_no: 集号（可空；空表示全剧策略）。
        closeup_density: 特写密度（0~1，可空，按模式默认值补齐）。
        dialogue_camera_rule: 对话机位规则文本（可空）。
        template_id: 套用分镜模板 ID（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    pacing_mode: Literal["fast", "balanced", "slow"] = Field(description="节奏模式")
    episode_no: int | None = Field(default=None, ge=1, description="集号（空=全剧）")
    closeup_density: float | None = Field(default=None, ge=0, le=1, description="特写密度")
    dialogue_camera_rule: str | None = Field(default=None, max_length=500, description="对话机位规则")
    template_id: str | None = Field(default=None, max_length=64, description="分镜模板 ID")


class StoryboardTemplateRequest(BaseModel):
    """保存自定义分镜模板请求体（AC-F4-10-2，V4.0）。

    Attributes:
        name: 模板名（≤128 字）。
        kind: 模板类型。
        rules: 机位规则条目（≤10 条，每条 ≤50 字）。
        description: 适用说明（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="模板名")
    kind: Literal["dialogue", "action", "transition", "custom"] = Field(
        default="custom", description="模板类型"
    )
    rules: list[str] = Field(min_length=1, max_length=10, description="机位规则条目")
    description: str | None = Field(default=None, max_length=500, description="适用说明")


# ---- ORM 序列化函数（输出统一为 JSON 兼容 dict） ----
def storyboard_out(storyboard: Storyboard, *, episode_no: int | None = None) -> dict[str, Any]:
    """将分镜板 ORM 对象序列化为响应字典。

    Args:
        storyboard: 分镜板 ORM 实例。
        episode_no: 集号（列表接口携带）。

    Returns:
        分镜板响应字典。
    """
    data: dict[str, Any] = {
        "id": storyboard.id,
        "project_id": storyboard.project_id,
        "episode_id": storyboard.episode_id,
        "version_no": storyboard.version_no,
        "status": storyboard.status,
        "shot_count": storyboard.shot_count,
        "estimated_duration_ms": storyboard.estimated_duration_ms,
        "locked_at": storyboard.locked_at.isoformat() if storyboard.locked_at else None,
        "created_at": storyboard.created_at.isoformat() if storyboard.created_at else None,
    }
    if episode_no is not None:
        data["episode_no"] = episode_no
    return data


def _frame_elements(spec: dict | None) -> dict[str, Any] | None:
    """把构图 spec 精简为前端展示用的要素清单。

    Args:
        spec: CompositionSpec 字典（可空）。

    Returns:
        ``{setting, subjects, props, focus, degraded}``；无 spec 时返回 None。
    """
    if not isinstance(spec, dict):
        return None
    setting = spec.get("setting") or {}
    subjects = [
        str(item.get("name"))
        for item in (spec.get("subjects") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    props = [
        str(item.get("name"))
        for item in (spec.get("props") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {
        "setting": {"name": setting.get("name"), "present": bool(setting.get("name"))},
        "subjects": subjects,
        "props": props,
        "focus": spec.get("focus"),
        "degraded": list(spec.get("degraded") or []),
    }


def shot_out(shot: Shot, *, media_url: str | None = None) -> dict[str, Any]:
    """将镜头 ORM 对象序列化为响应字典。

    Args:
        shot: 镜头 ORM 实例。
        media_url: 当前版本首帧媒体 URL（可空）。

    Returns:
        镜头响应字典。
    """
    data: dict[str, Any] = {
        "id": shot.id,
        "storyboard_id": shot.storyboard_id,
        "shot_no": shot.shot_no,
        "order_index": shot.order_index,
        "shot_size": shot.shot_size,
        "camera_move": shot.camera_move,
        "emotion_tag": shot.emotion_tag,
        "duration_ms": shot.duration_ms,
        "dialogue": shot.dialogue,
        "action_desc": shot.action_desc,
        "image_prompt": shot.image_prompt,
        "final_prompt": shot.final_prompt,
        # Q-012：锚点与场景/道具引用必须在详情接口可见，否则前端与验收方
        # 无法看到“本镜对应剧本原文/因果/剧情功能”，DB 有值但接口丢失。
        "script_anchor": shot.script_anchor,
        "asset_anchor_ids": shot.asset_anchor_ids,
        "character_asset_ids": shot.character_asset_ids,
        "scene_asset_id": shot.scene_asset_id,
        "scene_id": shot.scene_id,
        "prop_asset_ids": shot.prop_asset_ids,
        "status": shot.status,
        "locked": shot.locked,
        "upstream_changed": shot.upstream_changed,
        "redraft_count": shot.redraft_count,
        "composition_spec": shot.composition_spec,
        "frame_elements": _frame_elements(shot.composition_spec),
        "locked_at": shot.locked_at.isoformat() if shot.locked_at else None,
        "created_at": shot.created_at.isoformat() if shot.created_at else None,
    }
    if media_url is not None:
        data["media_url"] = media_url
    return data
