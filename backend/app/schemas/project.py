"""项目域 Pydantic Schema 模块。

定义 /projects 与 /teams 路由的请求体模型，以及 ORM -> 响应字典的
序列化函数（统一与 ``success_envelope`` 配套使用）。

包含：
- 项目创建/更新/状态流转/快照创建 请求模型；
- 团队创建/成员邀请/项目权限设置 请求模型；
- ``project_out`` / ``snapshot_out`` / ``team_out`` / ``member_out`` 序列化函数。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.iam import ProjectMember, Team, TeamMember, User
from app.models.project import Project, ProjectSnapshot

# ---- 画风预设 4 值（与 projects.style_preset_key CHECK 约束一致） ----
StylePresetKey = Literal["live_action", "anime", "chinese_style", "three_d"]
# ---- 画面比例 3 值（与 projects.aspect_ratio CHECK 约束一致） ----
AspectRatio = Literal["9:16", "16:9", "1:1"]
# ---- 项目三级权限 ----
PermissionLevel = Literal["view", "edit", "export"]
# ---- 团队成员角色（owner 不可通过邀请接口设置） ----
InviteRole = Literal["member", "guest", "admin"]


class ProjectCreateRequest(BaseModel):
    """创建项目请求体。

    Attributes:
        title: 项目标题（必填，1~255 字符）。
        synopsis: 一句话简介/创意。
        genre: 题材（开放枚举）。
        style_preset_key: 画风预设键（4 值，默认 live_action）。
        aspect_ratio: 画面比例（3 值，默认 9:16）。
        language: 主语言（BCP-47，默认 zh-CN）。
        target_episode_count: 目标集数（1~30，超限返回 40005，默认 10）。
        budget_credits: 项目预算上限（0 表示不限，默认 0）。
        config: 项目级配置 JSONB。
        team_id: 归属团队 ID（可选）。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255, description="项目标题")
    synopsis: str | None = Field(default=None, description="一句话简介")
    genre: str | None = Field(default=None, max_length=32, description="题材")
    style_preset_key: StylePresetKey = Field(default="live_action", description="画风预设键")
    aspect_ratio: AspectRatio = Field(default="9:16", description="画面比例")
    language: str = Field(default="zh-CN", max_length=16, description="主语言")
    target_episode_count: int = Field(default=10, description="目标集数（1~30）")
    budget_credits: int = Field(default=0, ge=0, description="预算上限（0 不限）")
    config: dict[str, Any] | None = Field(default=None, description="项目级配置")
    team_id: int | None = Field(default=None, description="归属团队 ID")


class ProjectUpdateRequest(BaseModel):
    """更新项目请求体（PATCH 语义，仅显式传入的字段会被更新）。

    Attributes:
        title: 新标题。
        synopsis: 新简介（显式传 null 可清空）。
        genre: 新题材。
        budget_credits: 新预算上限。
        config: 新配置（整体覆盖）。
        lock_version: 乐观锁版本；携带且与服务端不一致时返回 10006。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255, description="标题")
    synopsis: str | None = Field(default=None, description="简介")
    genre: str | None = Field(default=None, max_length=32, description="题材")
    budget_credits: int | None = Field(default=None, ge=0, description="预算上限")
    config: dict[str, Any] | None = Field(default=None, description="项目配置")
    lock_version: int | None = Field(default=None, description="乐观锁版本")


class TransitionRequest(BaseModel):
    """状态流转请求体。

    Attributes:
        target_status: 目标状态（须在当前状态的白名单内，否则 40001）。
        reason: 流转原因（写入操作日志与 SSE 事件）。
    """

    model_config = ConfigDict(extra="forbid")

    target_status: str = Field(min_length=1, max_length=32, description="目标状态")
    reason: str | None = Field(default=None, max_length=255, description="流转原因")


class SnapshotCreateRequest(BaseModel):
    """手动快照创建请求体。

    Attributes:
        name: 快照名称（携带名称的快照视为手动快照，不受 30 上限清理影响）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128, description="快照名称")


class TeamCreateRequest(BaseModel):
    """创建团队请求体。

    Attributes:
        name: 团队名称（1~128 字符）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="团队名称")


class TeamMemberAddRequest(BaseModel):
    """团队成员邀请请求体（按手机号定位被邀请用户）。

    Attributes:
        phone: 被邀请用户手机号。
        role: 团队角色 member/guest/admin（owner 不可通过本接口设置）。
    """

    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=1, max_length=20, description="被邀请人手机号")
    role: InviteRole = Field(default="member", description="团队角色")


class ProjectPermissionSetRequest(BaseModel):
    """项目成员权限设置请求体。

    Attributes:
        permission: 权限级别 view/edit/export。
    """

    model_config = ConfigDict(extra="forbid")

    permission: PermissionLevel = Field(description="权限级别")


# ---- ORM 序列化函数（输出统一为 JSON 兼容 dict） ----


def project_out(project: Project) -> dict[str, Any]:
    """将项目 ORM 对象序列化为响应字典。

    Args:
        project: 项目 ORM 实例。

    Returns:
        含全部业务字段与时间戳（ISO8601 字符串）的字典。
    """
    return {
        "id": project.id,
        "owner_user_id": project.owner_user_id,
        "team_id": project.team_id,
        "title": project.title,
        "synopsis": project.synopsis,
        "genre": project.genre,
        "style_preset_key": project.style_preset_key,
        "aspect_ratio": project.aspect_ratio,
        "language": project.language,
        "status": project.status,
        "target_episode_count": project.target_episode_count,
        "budget_credits": project.budget_credits,
        "spent_credits": project.spent_credits,
        "cover_media_id": project.cover_media_id,
        "current_script_id": project.current_script_id,
        "current_storyboard_id": project.current_storyboard_id,
        "config": project.config,
        "lock_version": project.lock_version,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def snapshot_out(snapshot: ProjectSnapshot, *, include_state: bool = True) -> dict[str, Any]:
    """将快照 ORM 对象序列化为响应字典。

    Args:
        snapshot: 快照 ORM 实例。
        include_state: 是否携带完整 state；False 时仅输出 state 摘要
            （status/trigger 等关键字段）。

    Returns:
        快照响应字典（时间戳为 ISO8601 字符串）。
    """
    state = snapshot.state or {}
    data: dict[str, Any] = {
        "id": snapshot.id,
        "project_id": snapshot.project_id,
        "sequence_no": snapshot.sequence_no,
        "label": snapshot.label,
        "trigger": state.get("_trigger", "manual"),
        "checkpoint_id": snapshot.checkpoint_id,
        "created_by": snapshot.created_by,
        "size_bytes": snapshot.size_bytes,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }
    if include_state:
        data["state"] = state
    else:
        # 摘要模式：仅状态关键字段
        data["status"] = state.get("status")
    return data


def team_out(team: Team, role: str | None = None) -> dict[str, Any]:
    """将团队 ORM 对象序列化为响应字典。

    Args:
        team: 团队 ORM 实例。
        role: 当前用户在该团队中的角色（可选携带）。

    Returns:
        团队响应字典。
    """
    return {
        "id": team.id,
        "name": team.name,
        "owner_user_id": team.owner_user_id,
        "seat_limit": team.seat_limit,
        "status": team.status,
        "my_role": role,
        "created_at": team.created_at.isoformat() if team.created_at else None,
    }


def project_member_out(member: ProjectMember, user: User | None = None) -> dict[str, Any]:
    """将项目成员授权记录序列化为响应字典。

    Args:
        member: ProjectMember ORM 实例。
        user: 关联用户实例（可选，用于携带昵称）。

    Returns:
        成员响应字典（昵称缺失时为 None）。
    """
    return {
        "user_id": member.user_id,
        "permission": member.permission,
        "granted_by": member.granted_by,
        "nickname": user.nickname if user is not None else None,
        "created_at": member.created_at.isoformat() if member.created_at else None,
    }


def team_member_out(member: TeamMember, user: User | None = None) -> dict[str, Any]:
    """将团队成员记录序列化为响应字典。

    Args:
        member: TeamMember ORM 实例。
        user: 关联用户实例（可选）。

    Returns:
        团队成员响应字典。
    """
    return {
        "team_id": member.team_id,
        "user_id": member.user_id,
        "role": member.role,
        "invited_by": member.invited_by,
        "nickname": user.nickname if user is not None else None,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
    }
