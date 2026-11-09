"""后期域请求/响应模型（Task 9 · Pydantic schemas）。

定义后期域 API 的请求体结构。响应实际经
:func:`app.core.errors.success_envelope` 包络输出，本模块的响应模型
主要用于文档化与内部构造校验。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TtsPreviewRequest(BaseModel):
    """TTS 试听请求体。

    Attributes:
        episode_no: 集号（可选；缺省取全剧前 3 句台词）。
    """

    model_config = ConfigDict(extra="forbid")

    episode_no: int | None = Field(default=None, ge=1, description="集号（可选）")


class SubtitlesRequest(BaseModel):
    """字幕生成请求体。

    Attributes:
        style_id: 字幕样式 ID（可选；缺省不绑定样式）。
    """

    model_config = ConfigDict(extra="forbid")

    style_id: int | None = Field(default=None, description="字幕样式 ID（可选）")


class BgmSetRequest(BaseModel):
    """BGM 设置请求体。

    Attributes:
        bgm_track_id: BGM 曲目 ID。
        volume_db: 音量（dB，默认 -6）。
        sidechain: 是否启用闪避（人声压 BGM，默认开启）。
    """

    model_config = ConfigDict(extra="forbid")

    bgm_track_id: int = Field(description="BGM 曲目 ID")
    volume_db: float = Field(default=-6.0, ge=-60.0, le=6.0, description="音量（dB）")
    sidechain: bool = Field(default=True, description="是否启用闪避")


class TimelineClipIn(BaseModel):
    """时间线片段输入项。

    Attributes:
        id: 片段 ID（可选，前端已有片段标识）。
        track_type: 轨道类型（video/audio/subtitle/bgm）。
        shot_id: 关联镜头 ID（可选；video 轨用于解析镜头版本源）。
        start_ms: 时间线起点（毫秒，≥0）。
        duration_ms: 片段时长（毫秒，>0）。
        content: 片段内容（字幕文本/样式等，可选）。
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, description="前端片段标识（可选）")
    track_type: Literal["video", "audio", "subtitle", "bgm"] = Field(description="轨道类型")
    shot_id: int | None = Field(default=None, description="关联镜头 ID（可选）")
    start_ms: int = Field(ge=0, description="时间线起点（毫秒）")
    duration_ms: int = Field(gt=0, description="片段时长（毫秒）")
    content: dict[str, Any] | None = Field(default=None, description="片段内容")


class TimelineSaveRequest(BaseModel):
    """时间线重建请求体。

    Attributes:
        clips: 片段列表（1~500 条，全删全插式重建）。
    """

    model_config = ConfigDict(extra="forbid")

    clips: list[TimelineClipIn] = Field(
        min_length=1, max_length=500, description="片段列表（全删全插）"
    )


class PostDecideRequest(BaseModel):
    """成片审批决策请求体（HITL-4）。

    Attributes:
        action: 决策动作（approve/reject）。
        feedback: 驳回反馈（reject 时必填且 ≥10 字）。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"] = Field(description="决策动作")
    feedback: str | None = Field(default=None, max_length=500, description="驳回反馈")


class BgmRecommendItem(BaseModel):
    """BGM 推荐项（文档化结构）。

    Attributes:
        id: 曲目 ID。
        title: 曲名。
        license_type: 授权类型（self/third_party）。
    """

    id: int
    title: str
    license_type: str | None


# ---- V4.0 增量请求体 ----


class VoiceParamsRequest(BaseModel):
    """逐角色配音参数请求体（AC-F6-07-1，V4.0）。

    Attributes:
        rate: 语速（0.80~1.50，步进 0.05）。
        pitch_shift: 语调偏移档位（-3~3）。
        emotion_override: 情绪标签按场覆盖列表（[{scene_no, emotion}]）。
    """

    model_config = ConfigDict(extra="forbid")

    rate: float = Field(default=1.0, ge=0.80, le=1.50, description="语速 0.80~1.50")
    pitch_shift: int = Field(default=0, ge=-3, le=3, description="语调偏移档位 ±3")
    emotion_override: list[dict[str, Any]] = Field(
        default_factory=list, description="情绪标签按场覆盖"
    )


class SubtitleLinePatchRequest(BaseModel):
    """逐条字幕编辑请求体（AC-F6-08-1~2，V4.0）。

    Attributes:
        text: 新字幕文本（可空，不传不改）。
        start_ms: 新起始毫秒（可空，出入点 ±0.5s 微调）。
        end_ms: 新结束毫秒（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=200, description="字幕文本")
    start_ms: int | None = Field(default=None, ge=0, description="起始毫秒")
    end_ms: int | None = Field(default=None, ge=1, description="结束毫秒")


class SubtitleBatchStyleRequest(BaseModel):
    """批量修改字幕样式请求体（AC-F6-08-3，V4.0）。

    Attributes:
        style_id: 目标字幕样式 ID。
        scope: 作用范围 all（全片）/episode（按集）/speaker（按角色）。
        speaker: 按角色应用时的说话人。
    """

    model_config = ConfigDict(extra="forbid")

    style_id: int = Field(ge=1, description="字幕样式 ID")
    scope: Literal["all", "episode", "speaker"] = Field(default="all", description="作用范围")
    speaker: str | None = Field(default=None, max_length=64, description="按角色应用时的说话人")


class BgmReplaceRequest(BaseModel):
    """BGM 手动替换请求体（AC-F6-09-1，V4.0）。

    Attributes:
        bgm_track_id: 替换为的 BGM 曲目 ID。
        volume_db: 音量（dB，可空沿用自动混音 -6dB）。
    """

    model_config = ConfigDict(extra="forbid")

    bgm_track_id: int = Field(ge=1, description="BGM 曲目 ID")
    volume_db: float | None = Field(default=None, ge=-60.0, le=6.0, description="音量（dB）")


class MixProfileRequest(BaseModel):
    """混音画像请求体（AC-F6-10-1~2，V4.0）。

    Attributes:
        track_volumes: 逐轨音量偏移 {dialogue/bgm/sfx: dB}（±6dB）。
        track_muted: 静音轨列表。
        loudness_target: 响度目标 LUFS（-30~-6，默认 -14）。
        normalize_enabled: 是否启用响度标准化。
    """

    model_config = ConfigDict(extra="forbid")

    track_volumes: dict[str, float] | None = Field(
        default=None, description="逐轨音量偏移（±6dB）"
    )
    track_muted: list[str] | None = Field(default=None, description="静音轨列表")
    loudness_target: float | None = Field(
        default=None, ge=-30.0, le=-6.0, description="响度目标 LUFS"
    )
    normalize_enabled: bool | None = Field(default=None, description="响度标准化开关")
