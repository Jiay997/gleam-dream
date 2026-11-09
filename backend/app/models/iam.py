"""域 A · 身份与权限（IAM）模型。

包含 7 张表（对齐《02 数据库设计文档》§5.1）：
- ``users`` 用户主表；
- ``user_identities`` 三方登录身份；
- ``teams`` 团队/工作空间（P1）；
- ``team_members`` 团队成员与角色（P1）；
- ``project_members`` 项目级授权（P1）；
- ``audit_logs`` 审计日志（基础设施表，只追加）；
- ``operation_logs`` 操作日志（基础设施表，只追加）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, SoftDeleteMixin, TimestampMixin


class User(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """用户主表：承载登录标识、昵称、状态与风控标记（表 users）。

    Attributes:
        phone: 手机号（软删部分唯一）。
        email: 邮箱（软删部分唯一）。
        nickname: 昵称。
        avatar_url: 头像 URL。
        password_hash: 密码哈希（Argon2id，三方登录可空）。
        status: 账号状态 active/suspended/banned。
        risk_level: 风控等级 0~3。
        register_source: 注册来源 web/wechat/apple/admin。
        preferences: 用户偏好设置（JSONB，新手引导 P02，浅合并更新）。
        last_login_at: 末次登录时间。
        deleted_at: 软删除时间（继承自 SoftDeleteMixin）。
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            text("status IN ('active', 'suspended', 'banned')"),
            name="ck_users_status",
        ),
        CheckConstraint(text("risk_level BETWEEN 0 AND 3"), name="ck_users_risk_level"),
        CheckConstraint(
            text("register_source IN ('web', 'wechat', 'apple', 'admin')"),
            name="ck_users_register_source",
        ),
        # 软删部分唯一：仅对存活行保证唯一（对齐 02 文档 §2.3）
        Index("uq_users_phone", "phone", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("uq_users_email", "email", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("idx_users_status", "status"),
        Index("idx_users_created_at", "created_at"),
        Index("idx_users_last_login_at", "last_login_at"),
    )

    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="手机号")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="邮箱")
    nickname: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="", comment="昵称"
    )
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="头像 URL")
    password_hash: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="密码哈希（Argon2id）"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active", comment="账号状态"
    )
    risk_level: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="风控等级 0~3"
    )
    register_source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="web", comment="注册来源"
    )
    preferences: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="用户偏好设置（新手引导 P02，JSONB 浅合并更新）"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="末次登录时间"
    )


class UserIdentity(IdMixin, TimestampMixin, Base):
    """三方登录身份：一个用户可绑定多个三方身份（表 user_identities）。

    Attributes:
        user_id: 归属用户 ID（FK users，级联删除）。
        provider: 身份提供方 wechat/apple/qq/phone。
        provider_user_id: 提供方用户标识（openid/sub）。
        union_id: 微信 unionid 等归并标识。
        raw_profile: 原始资料快照。
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        CheckConstraint(
            text("provider IN ('wechat', 'apple', 'qq', 'phone')"),
            name="ck_user_identities_provider",
        ),
        UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_user_identities_provider_provider_user_id",
        ),
        Index("idx_user_identities_user_id", "user_id"),
        Index("idx_user_identities_union_id", "union_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="归属用户 ID"
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, comment="身份提供方")
    provider_user_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="提供方用户标识"
    )
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="微信 unionid")
    raw_profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="原始资料快照")


class Team(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """团队/工作空间：P1 团队容器，团队计费与成员归属主体（表 teams）。

    Attributes:
        name: 团队名称。
        owner_user_id: 拥有者用户 ID（FK users，RESTRICT）。
        plan_id: 团队套餐 ID（FK plans，SET NULL）。
        seat_limit: 席位数上限（≥1）。
        status: 状态 active/suspended/archived。
        deleted_at: 软删除时间。
    """

    __tablename__ = "teams"
    __table_args__ = (
        CheckConstraint(text("seat_limit >= 1"), name="ck_teams_seat_limit"),
        CheckConstraint(
            text("status IN ('active', 'suspended', 'archived')"),
            name="ck_teams_status",
        ),
        Index("idx_teams_owner_user_id", "owner_user_id"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="团队名称")
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="拥有者用户 ID"
    )
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, comment="团队套餐 ID"
    )
    seat_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5"), comment="席位数上限"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active", comment="团队状态"
    )


class TeamMember(IdMixin, TimestampMixin, Base):
    """团队成员与角色：团队与用户的多对多关联（表 team_members）。

    Attributes:
        team_id: 团队 ID（FK teams，级联删除）。
        user_id: 成员用户 ID（FK users，级联删除）。
        role: 角色 owner/admin/member/guest。
        invited_by: 邀请人用户 ID（FK users，SET NULL）。
        joined_at: 加入时间。
    """

    __tablename__ = "team_members"
    __table_args__ = (
        CheckConstraint(
            text("role IN ('owner', 'admin', 'member', 'guest')"),
            name="ck_team_members_role",
        ),
        UniqueConstraint("team_id", "user_id", name="uq_team_members_team_id_user_id"),
        Index("idx_team_members_user_id", "user_id"),
    )

    team_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, comment="团队 ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="成员用户 ID"
    )
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="member", comment="团队角色"
    )
    invited_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="邀请人用户 ID"
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="加入时间"
    )


class ProjectMember(IdMixin, TimestampMixin, Base):
    """项目级授权：项目三级权限（view/edit/export）显式授权表（表 project_members）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        user_id: 被授权用户 ID（FK users，级联删除）。
        permission: 权限级别 view/edit/export。
        granted_by: 授权人用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint(
            text("permission IN ('view', 'edit', 'export')"),
            name="ck_project_members_permission",
        ),
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_id_user_id"),
        Index("idx_project_members_user_id", "user_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="被授权用户 ID"
    )
    permission: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="view", comment="权限级别"
    )
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="授权人用户 ID"
    )


class AuditLog(IdMixin, TimestampMixin, Base):
    """审计日志：越权、敏感操作的不可篡改留痕，只追加（表 audit_logs）。

    无物理外键（日志独立性）；业务上只写不改，updated_at 恒等于 created_at。

    Attributes:
        actor_type: 主体类型 user/agent/system。
        actor_user_id: 操作者用户 ID（弱引用）。
        actor_agent_run_id: Agent 运行 ID（弱引用）。
        project_id: 关联项目 ID（弱引用）。
        action: 动作标识（如 acl.denied）。
        target_type: 目标类型。
        target_id: 目标 ID。
        ip: 来源 IP（INET）。
        user_agent: 客户端 UA。
        detail: 详情 JSONB。
        trace_id: 链路 ID（ULID）。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            text("actor_type IN ('user', 'agent', 'system')"),
            name="ck_audit_logs_actor_type",
        ),
        Index("idx_audit_logs_project_id_created_at", "project_id", "created_at"),
        Index("idx_audit_logs_actor_user_id_created_at", "actor_user_id", "created_at"),
        Index("idx_audit_logs_action_created_at", "action", "created_at"),
    )

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="主体类型")
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="操作者用户 ID")
    actor_agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Agent 运行 ID"
    )
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="关联项目 ID")
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="动作标识")
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="目标类型")
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="目标 ID")
    ip: Mapped[str | None] = mapped_column(INET, nullable=True, comment="来源 IP")
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="客户端 UA")
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="详情")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路 ID")


class OperationLog(IdMixin, TimestampMixin, Base):
    """操作日志：常规 who/when/what/diff 留痕，只追加（表 operation_logs）。

    无物理外键；业务上只写不改。

    Attributes:
        actor_user_id: 操作者用户 ID（弱引用）。
        project_id: 项目 ID（弱引用）。
        entity_type: 实体类型。
        entity_id: 实体 ID。
        action: 动作 create/update/delete。
        diff: 变更前后差异。
        request_id: 请求 ID（ULID）。
    """

    __tablename__ = "operation_logs"
    __table_args__ = (
        CheckConstraint(text("action IN ('create', 'update', 'delete')"), name="ck_operation_logs_action"),
        Index("idx_operation_logs_project_id_created_at", "project_id", "created_at"),
        Index("idx_operation_logs_entity_type_entity_id", "entity_type", "entity_id"),
    )

    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="操作者用户 ID")
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="项目 ID")
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="实体类型")
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="实体 ID")
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="动作")
    diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="变更前后差异")
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="请求 ID")
