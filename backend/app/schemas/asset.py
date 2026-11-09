"""资产域 Pydantic Schema 模块（Task 7b）。

定义 /assets 与 /tts-voices 路由的请求体模型，以及 ORM -> 响应字典的
序列化函数（统一与 ``success_envelope`` 配套使用）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.asset import Asset, AssetVersion, FaceAuthorization, TtsVoice
from app.models.quality import QcReport

# ---- 资产版本类型 7 值（与 asset_versions.version_type CHECK 约束一致；
#      前端主用 5 种角色版本，scene_ref/prop_ref 服务 scene/prop 资产） ----
AssetVersionType = Literal[
    "portrait", "turnaround_front", "turnaround_side",
    "turnaround_back", "expression_set", "scene_ref", "prop_ref",
]


class AssetGenerateRequest(BaseModel):
    """资产生成请求体。

    Attributes:
        version_type: 版本类型（portrait/三视图/表情集；scene/prop 资产
            分别使用 scene_ref/prop_ref）。
        feedback: 用户调整要求（可空，拼入生成 prompt）。
    """

    model_config = ConfigDict(extra="forbid")

    version_type: AssetVersionType = Field(description="版本类型")
    feedback: str | None = Field(default=None, max_length=500, description="调整要求")


class AssetRegenerateRequest(BaseModel):
    """资产重新生成请求体（同 generate，新版本旧版保留）。

    Attributes:
        feedback: 用户调整要求（可空，拼入生成 prompt）。
    """

    model_config = ConfigDict(extra="forbid")

    feedback: str | None = Field(default=None, max_length=500, description="调整要求")


class AssetRejectRequest(BaseModel):
    """资产驳回请求体。

    Attributes:
        feedback: 驳回原因（必填，≥10 字，存入资产 meta.reject_feedback）。
    """

    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(min_length=10, max_length=1000, description="驳回原因（≥10 字）")


class VoiceBindRequest(BaseModel):
    """音色绑定请求体。

    Attributes:
        voice_id: 目标音色 ID（须存在且 available）。
    """

    model_config = ConfigDict(extra="forbid")

    voice_id: int = Field(ge=1, description="音色 ID")


class FaceAuthorizationRequest(BaseModel):
    """真人照片授权请求体。

    Attributes:
        authorization_type: 授权类型 self/authorized_other。
        agreement_version: 授权协议版本号（留痕）。
        subject_name: 被授权人姓名（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    authorization_type: str = Field(description="授权类型（self/authorized_other）")
    agreement_version: str = Field(min_length=1, max_length=32, description="协议版本")
    subject_name: str | None = Field(default=None, max_length=128, description="被授权人姓名")


class AssetLabelsRequest(BaseModel):
    """资产标签更新请求体（AC-F3-11-1~2，V4.0）。

    Attributes:
        labels: 标签数组（≤10 个，单标签 ≤12 字）。
    """

    model_config = ConfigDict(extra="forbid")

    labels: list[str] = Field(
        default_factory=list, max_length=10, description="标签数组（≤10 个）"
    )


class BatchRegenerateRequest(BaseModel):
    """批量重抽请求体（AC-F3-10-1~2，V4.0）。

    Attributes:
        asset_ids: 目标资产 ID 列表（1~20 个）。
        version_type: 版本类型（缺省 portrait）。
        feedback: 统一重抽反馈（5~100 字，AC-F3-03-3）。
        confirm: 是否已确认成本预估（未确认时返回预估不执行）。
    """

    model_config = ConfigDict(extra="forbid")

    asset_ids: list[int] = Field(min_length=1, max_length=20, description="资产 ID 列表")
    version_type: AssetVersionType = Field(default="portrait", description="版本类型")
    feedback: str = Field(min_length=5, max_length=100, description="统一重抽反馈（5~100 字）")
    confirm: bool = Field(default=False, description="是否已确认成本预估并执行")


class SceneVariantRequest(BaseModel):
    """场景变体生成请求体（AC-F3-08-1~2，V4.0）。

    Attributes:
        variants: 变体标签数组（时段：dawn/day/dusk/night；天气：sunny/rain/snow；
            最多各选 2 项）。
    """

    model_config = ConfigDict(extra="forbid")

    variants: list[str] = Field(
        min_length=1, max_length=4, description="变体标签（时段/天气，各≤2 项）"
    )


class FaceAuthorizationRevokeRequest(BaseModel):
    """真人授权撤回请求体（AC-F3-12-2，V4.0）。

    Attributes:
        confirm_name: 二次确认输入的角色名（须与资产名一致）。
        reason: 撤回原因（可空，≤200 字）。
    """

    model_config = ConfigDict(extra="forbid")

    confirm_name: str = Field(min_length=1, max_length=128, description="二次确认角色名")
    reason: str | None = Field(default=None, max_length=200, description="撤回原因")


# ---- ORM 序列化函数（输出统一为 JSON 兼容 dict） ----


def voice_out(voice: TtsVoice) -> dict[str, Any]:
    """将音色 ORM 对象序列化为响应字典。

    Args:
        voice: 音色 ORM 实例。

    Returns:
        音色响应字典。
    """
    return {
        "id": voice.id,
        "voice_key": voice.voice_key,
        "name": voice.name,
        "gender": voice.gender,
        "language": voice.language,
        "style_tags": voice.style_tags,
        "provider": voice.provider,
        "status": voice.status,
    }


def asset_version_out(version: AssetVersion, media_url: str | None = None) -> dict[str, Any]:
    """将资产版本 ORM 对象序列化为响应字典。

    Args:
        version: 资产版本 ORM 实例。
        media_url: 产物媒体访问 URL（可空）。

    Returns:
        版本响应字典。
    """
    return {
        "id": version.id,
        "asset_id": version.asset_id,
        "version_type": version.version_type,
        "version_no": version.version_no,
        "media_file_id": version.media_file_id,
        "media_url": media_url,
        "prompt": version.prompt,
        "seed": version.seed,
        "model_key": version.model_key,
        "qc_report_id": version.qc_report_id,
        "is_current": version.is_current,
        "variant_tag": version.variant_tag,
        "revision_notes": version.revision_notes,
        "cumulative_cost": version.cumulative_cost,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def qc_report_out(qc: QcReport) -> dict[str, Any]:
    """将质检报告 ORM 对象序列化为响应字典。

    Args:
        qc: QC 报告 ORM 实例。

    Returns:
        QC 报告响应字典。
    """
    return {
        "id": qc.id,
        "target_type": qc.target_type,
        "target_id": qc.target_id,
        "target_version_id": qc.target_version_id,
        "result": qc.result,
        "consistency_score": float(qc.consistency_score) if qc.consistency_score is not None else None,
        "detail": qc.detail,
        "round_no": qc.round_no,
        "created_at": qc.created_at.isoformat() if qc.created_at else None,
    }


def asset_out(asset: Asset, *, shot_refs: int | None = None) -> dict[str, Any]:
    """将资产 ORM 对象序列化为响应字典。

    Args:
        asset: 资产 ORM 实例。
        shot_refs: 引用镜头数（详情接口携带，列表接口可省）。

    Returns:
        资产响应字典。
    """
    data: dict[str, Any] = {
        "id": asset.id,
        "project_id": asset.project_id,
        "asset_type": asset.asset_type,
        "name": asset.name,
        "description": asset.description,
        "status": asset.status,
        "source": asset.source,
        "current_version_id": asset.current_version_id,
        "voice_id": asset.voice_id,
        "voice_status": asset.voice_status,
        "tags": asset.tags,
        "labels": asset.labels or [],
        "meta": asset.meta,
        "approved_at": asset.approved_at.isoformat() if asset.approved_at else None,
        "locked_at": asset.locked_at.isoformat() if asset.locked_at else None,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "updated_at": asset.updated_at.isoformat() if asset.updated_at else None,
    }
    if shot_refs is not None:
        data["shot_refs"] = shot_refs
    return data


def asset_detail_out(
    asset: Asset,
    versions: list[dict[str, Any]],
    qc_reports: list[dict[str, Any]],
    voice: dict[str, Any] | None,
    shot_refs: int,
) -> dict[str, Any]:
    """组装资产详情响应字典（详情 + 版本 + QC + 音色 + 引用镜头数）。

    Args:
        asset: 资产 ORM 实例。
        versions: 版本响应字典列表（asset_version_out 产物）。
        qc_reports: QC 报告响应字典列表（qc_report_out 产物）。
        voice: 音色响应字典（未绑定为 None）。
        shot_refs: 引用镜头数。

    Returns:
        资产详情字典。
    """
    data = asset_out(asset, shot_refs=shot_refs)
    data["versions"] = versions
    data["qc_reports"] = qc_reports
    data["voice"] = voice
    return data


def face_authorization_out(auth: FaceAuthorization) -> dict[str, Any]:
    """将真人授权记录 ORM 对象序列化为响应字典。

    Args:
        auth: 授权记录 ORM 实例。

    Returns:
        授权记录响应字典。
    """
    return {
        "id": auth.id,
        "asset_id": auth.asset_id,
        "project_id": auth.project_id,
        "authorization_type": auth.authorization_type,
        "protocol_version": auth.protocol_version,
        "subject_name": auth.subject_name,
        "status": auth.status,
        "revoked_at": auth.revoked_at.isoformat() if auth.revoked_at else None,
        "revoke_reason": auth.revoke_reason,
        "ip": str(auth.ip) if auth.ip else None,
        "authorized_at": auth.authorized_at.isoformat() if auth.authorized_at else None,
        "expires_at": auth.expires_at.isoformat() if auth.expires_at else None,
    }


class VoicePreviewRequest(BaseModel):
    """音色试听 / 重新生成样音请求体。

    Attributes:
        text: 试听文本（可空；空则使用该音色的默认示例句，≤80 字）。
        rate: 语速倍率（0.5–2.0）。
        pitch: 音调偏移（-12–12 半音）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(
        default=None, max_length=80, description="试听文本（空则使用默认示例句）"
    )
    rate: float = Field(default=1.0, ge=0.5, le=2.0, description="语速倍率")
    pitch: int = Field(default=0, ge=-12, le=12, description="音调偏移（半音）")
