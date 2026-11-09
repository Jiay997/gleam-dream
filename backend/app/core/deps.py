"""通用 FastAPI 依赖模块。

集中提供路由层常用依赖：
- ``get_db``：数据库会话（自 :mod:`app.core.db` 重导出）；
- ``get_current_user``：解析 Bearer JWT，无效 -> 20001 / 过期 -> 20002；
- ``get_request_id``：获取当前请求的 ULID 请求 ID；
- ``require_project_acl`` / ``project_guard``：项目级 ACL 鉴权
  （owner 或 project_members 三级权限 view/edit/export）。
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id
from app.core.security import decode_token
from app.models.iam import AuditLog, ProjectMember
from app.models.project import Project

__all__ = [
    "get_db",
    "get_current_user",
    "get_request_id",
    "require_admin_token",
    "require_project_acl",
    "project_guard",
]

# Bearer 凭证提取器：缺失/格式错误时返回 None（不自动抛 403，交给业务码）
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer JWT")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    access_token: Annotated[str | None, Query()] = None,
) -> str:
    """认证依赖：解析 JWT 并返回当前用户 ID。

    令牌来源优先级：
    1. ``Authorization: Bearer <jwt>`` 请求头（常规 AJAX 请求）；
    2. ``?access_token=<jwt>`` 查询参数（兜底：浏览器 ``EventSource``
       无法自定义请求头，SSE 端点依赖此通道完成鉴权）。

    Args:
        credentials: 从 Authorization 头提取的 Bearer 凭证（可能为 None）。
        access_token: 查询参数中的访问令牌（可选，供 SSE 使用）。

    Returns:
        用户 ID（JWT payload 中的 sub）。

    Raises:
        BizError: 20001 未登录/token 无效/token 类型不匹配；20002 token 过期。
    """
    token = credentials.credentials if (credentials and credentials.credentials) else access_token
    if not token:
        raise BizError(ErrorCode.UNAUTHORIZED)
    payload = decode_token(token, expected_type="access")
    subject = str(payload.get("sub", "")).strip()
    if not subject.isdigit():
        # 防御非法/缺失 sub（避免业务层 int() 抛 ValueError 变成 500）
        raise BizError(ErrorCode.UNAUTHORIZED)
    return subject


async def require_admin_token(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """管理端鉴权依赖：校验 ``X-Admin-Token`` 与配置口令一致。

    用于平台级敏感操作（模型配置写操作 / 埋点漏斗等）。服务端未配置
    ``ADMIN_TOKEN`` 时端点恒禁用（20003），不提供匿名管理入口。

    Args:
        x_admin_token: 请求头中的管理端口令。

    Returns:
        None（校验通过）。

    Raises:
        BizError: 20003 口令缺失/不匹配或管理端未启用（消息内附可执行的修复指引，
            前端会直接展示后端 message，避免只剩“无权限执行该操作”的泛化提示）。
    """
    expected = settings.ADMIN_TOKEN.strip()
    if not expected or not x_admin_token or not hmac.compare_digest(x_admin_token.strip(), expected):
        raise BizError(
            ErrorCode.FORBIDDEN,
            "管理端口令缺失或不匹配（X-Admin-Token）：请在「模型配置」页填写"
            " backend/.env 中的 ADMIN_TOKEN",
        )


def get_request_id(request: Request) -> str:
    """获取当前请求的请求 ID。

    正常情况下由 main.py 的 RequestIDMiddleware 写入 request.state；
    状态缺失时（如单元测试直调）兜底生成新的 ULID。

    Args:
        request: 当前请求对象。

    Returns:
        请求 ID 字符串。
    """
    rid = getattr(request.state, "request_id", None)
    return rid if rid else new_request_id()


# ---- 项目级 ACL 鉴权 ----

#: 权限等级量化：export > edit > view（高位包含低位）
_PERMISSION_RANK: dict[str, int] = {"view": 0, "edit": 1, "export": 2}

#: 允许的权限需求取值
PermissionNeed = Literal["view", "edit", "export"]


async def require_project_acl(
    project_id: int,
    user_id: str | int,
    session: AsyncSession,
    need: PermissionNeed,
) -> Project:
    """项目级访问控制：校验用户对项目的权限，不满足则记审计并拒绝。

    判定顺序：
    1. 项目不存在或已软删 -> 10002；
    2. **归档只读守卫**（D13/D15 修复）：``status == "archived"`` 且需求为
       edit/export -> 40004（归档=纯只读，view 读取放行）；
    3. ``owner_user_id == user`` -> 通过（视为最高权限）；
    4. ``project_members.permission`` 等级 >= 所需等级 -> 通过；
    5. 否则写 AuditLog（action="acl_denied"，detail 携带 need）后抛 20003。

    Args:
        project_id: 项目 ID。
        user_id: 当前用户 ID（JWT sub，字符串数字或 int）。
        session: 异步数据库会话（用于查项目/成员并落审计日志）。
        need: 所需权限等级 view/edit/export。

    Returns:
        校验通过的项目 ORM 实例。

    Raises:
        BizError: 10002 项目不存在；40004 归档项目拒绝写操作；
            20003 无权限（已先落审计日志）。
    """
    uid = int(user_id)
    stmt = select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
    project = (await session.execute(stmt)).scalar_one_or_none()
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

    # 归档只读守卫：edit/export 一律拒绝（含 Owner），读取（view）不受限。
    # 归档恢复走专用 POST /projects/{id}/unarchive（Owner 专属，view 档 + Owner 校验）。
    if project.status == "archived" and need in ("edit", "export"):
        raise BizError(ErrorCode.PROJECT_ARCHIVED)

    required = _PERMISSION_RANK[need]
    if project.owner_user_id == uid:
        return project

    member_stmt = select(ProjectMember).where(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == uid,
    )
    member = (await session.execute(member_stmt)).scalar_one_or_none()
    if member is not None and _PERMISSION_RANK.get(member.permission, -1) >= required:
        return project

    # 拒绝前先落审计日志（独立提交，避免异常回滚导致留痕丢失）
    session.add(
        AuditLog(
            actor_type="user",
            actor_user_id=uid,
            project_id=project_id,
            action="acl_denied",
            target_type="project",
            target_id=project_id,
            detail={"need": need},
        )
    )
    await session.commit()
    raise BizError(ErrorCode.FORBIDDEN)


def project_guard(need: PermissionNeed):
    """项目 ACL 依赖工厂：生成从路径参数 project_id 取值的鉴权依赖。

    供路由声明使用::

        @router.get("/{project_id}")
        async def detail(project: Annotated[Project, Depends(project_guard("edit"))]): ...

    Args:
        need: 所需权限等级 view/edit/export。

    Returns:
        FastAPI 依赖函数，解析成功返回项目 ORM 实例，失败抛 BizError。
    """

    async def dep(
        project_id: int,
        user_id: Annotated[str, Depends(get_current_user)],
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> Project:
        """执行 ACL 校验的依赖闭包。

        Args:
            project_id: 路径参数项目 ID。
            user_id: 当前登录用户 ID（get_current_user 注入）。
            session: 请求级数据库会话（get_db 注入）。

        Returns:
            校验通过的项目 ORM 实例。

        Raises:
            BizError: 10002 项目不存在；20003 无权限；20001 未登录。
        """
        return await require_project_acl(project_id, user_id, session, need)

    return dep
