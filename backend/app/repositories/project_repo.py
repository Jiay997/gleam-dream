"""项目域数据访问层（Repository）模块。

封装项目、快照、团队成员、团队等 ORM 查询，供服务层调用；统一使用
SQLAlchemy 2.0 ``select()`` 风格 + 参数化条件，禁止拼接 SQL。

包含：
- :class:`ProjectRepository`：项目主表/成员/快照查询与维护；
- :class:`TeamRepository`：团队与团队成员查询。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.iam import ProjectMember, Team, TeamMember, User
from app.models.project import Project, ProjectSnapshot

#: 列表排序允许的字段白名单（防注入）
_SORTABLE_FIELDS = ("created_at", "updated_at", "title")
#: 列表 scope 取值
_SCOPES = ("created", "joined", "all")


class ProjectRepository:
    """项目域查询集合（静态方法，按 session 显式传参）。"""

    @staticmethod
    async def get_by_id(session: AsyncSession, project_id: int) -> Project | None:
        """按 ID 查询存活项目（排除软删除）。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。

        Returns:
            项目 ORM 实例；不存在或已软删时返回 None。
        """
        stmt = select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def get_member(session: AsyncSession, project_id: int, user_id: int) -> ProjectMember | None:
        """查询用户在项目上的授权记录。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。
            user_id: 用户 ID。

        Returns:
            ProjectMember 实例；无授权时返回 None。
        """
        stmt = select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_members(session: AsyncSession, project_id: int) -> list[tuple[ProjectMember, User | None]]:
        """查询项目全部授权成员（联用户表取昵称，按授权时间升序）。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。

        Returns:
            (ProjectMember, User|None) 元组列表。
        """
        stmt = (
            select(ProjectMember, User)
            .outerjoin(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.id.asc())
        )
        rows = (await session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    @staticmethod
    def _base_list_stmt(owner_user_id: int, scope: str) -> Select:
        """构造项目列表基础查询（存活项目 + 可见范围条件）。

        可见范围：
        - ``created``：仅本人创建；
        - ``joined`` / ``all``：本人创建或 project_members 包含本人。

        Args:
            owner_user_id: 当前用户 ID。
            scope: 范围 created/joined/all。

        Returns:
            未加排序/分页/筛选的 Select 语句。
        """
        visible: ColumnElement[bool] = Project.owner_user_id == owner_user_id
        if scope in ("joined", "all"):
            joined_ids = select(ProjectMember.project_id).where(ProjectMember.user_id == owner_user_id)
            visible = or_(visible, Project.id.in_(joined_ids))
        return select(Project).where(Project.deleted_at.is_(None), visible)

    @staticmethod
    async def list_page(
        session: AsyncSession,
        *,
        owner_user_id: int,
        scope: str = "created",
        status: str | None = None,
        genre: str | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> tuple[list[Project], int]:
        """分页查询项目列表（scope 可见范围 + 状态/题材筛选 + 排序）。

        Args:
            session: 异步数据库会话。
            owner_user_id: 当前用户 ID。
            scope: 范围 created/joined/all（非法取值按 created 处理）。
            status: 状态筛选（None 不过滤）。
            genre: 题材筛选（None 不过滤）。
            page: 页码（从 1 起）。
            page_size: 每页条数。
            sort: 排序字段，``-`` 前缀表示倒序（仅允许 created_at/updated_at/title）。

        Returns:
            (当前页项目列表, 满足条件的总数)。
        """
        if scope not in _SCOPES:
            scope = "created"
        # 排序解析：字段白名单 + 方向前缀
        field, direction = (sort[1:], "desc") if sort.startswith("-") else (sort, "asc")
        if field not in _SORTABLE_FIELDS:
            field, direction = "created_at", "desc"
        order_col = getattr(Project, field)

        stmt = ProjectRepository._base_list_stmt(owner_user_id, scope)
        if status:
            stmt = stmt.where(Project.status == status)
        if genre:
            stmt = stmt.where(Project.genre == genre)

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        stmt = stmt.order_by(order_col.desc() if direction == "desc" else order_col.asc())
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list((await session.execute(stmt)).scalars().all())
        return items, int(total)

    @staticmethod
    async def next_sequence_no(session: AsyncSession, project_id: int) -> int:
        """计算项目下一个快照序号（当前最大值 + 1，无快照时为 1）。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。

        Returns:
            下一个快照序号（≥1）。
        """
        stmt = select(func.max(ProjectSnapshot.sequence_no)).where(ProjectSnapshot.project_id == project_id)
        current = (await session.execute(stmt)).scalar_one_or_none()
        return int(current or 0) + 1

    @staticmethod
    async def count_snapshots(session: AsyncSession, project_id: int) -> int:
        """统计项目快照总数。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。

        Returns:
            快照总数。
        """
        stmt = select(func.count()).select_from(ProjectSnapshot).where(ProjectSnapshot.project_id == project_id)
        return int((await session.execute(stmt)).scalar_one())

    @staticmethod
    async def list_snapshots(session: AsyncSession, project_id: int) -> list[ProjectSnapshot]:
        """按创建时间倒序列出项目全部快照。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。

        Returns:
            快照 ORM 列表（最新在前）。
        """
        stmt = (
            select(ProjectSnapshot)
            .where(ProjectSnapshot.project_id == project_id)
            .order_by(ProjectSnapshot.created_at.desc(), ProjectSnapshot.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def get_snapshot(session: AsyncSession, project_id: int, snapshot_id: int) -> ProjectSnapshot | None:
        """按 ID 查询项目下的快照。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID（限定归属，防跨项目取快照）。
            snapshot_id: 快照 ID。

        Returns:
            快照 ORM 实例；不存在时返回 None。
        """
        stmt = select(ProjectSnapshot).where(
            ProjectSnapshot.project_id == project_id,
            ProjectSnapshot.id == snapshot_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def prune_snapshots(session: AsyncSession, project_id: int, limit: int = 30) -> int:
        """快照数量超限时删除最旧的非手动快照（label 为空，即 auto/rollback）。

        手动快照（label 非空）不受清理影响；无匿名快照可删时不做任何删除。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。
            limit: 数量上限。

        Returns:
            实际删除的快照条数。
        """
        total = await ProjectRepository.count_snapshots(session, project_id)
        if total <= limit:
            return 0
        extra = total - limit
        stmt = (
            select(ProjectSnapshot)
            .where(ProjectSnapshot.project_id == project_id, ProjectSnapshot.label.is_(None))
            .order_by(ProjectSnapshot.created_at.asc(), ProjectSnapshot.id.asc())
            .limit(extra)
        )
        victims = list((await session.execute(stmt)).scalars().all())
        for snap in victims:
            await session.delete(snap)
        await session.flush()
        return len(victims)


class TeamRepository:
    """团队域查询集合（静态方法）。"""

    @staticmethod
    async def get_team_by_id(session: AsyncSession, team_id: int) -> Team | None:
        """按 ID 查询存活团队。

        Args:
            session: 异步数据库会话。
            team_id: 团队 ID。

        Returns:
            团队 ORM 实例；不存在或已软删返回 None。
        """
        stmt = select(Team).where(Team.id == team_id, Team.deleted_at.is_(None))
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def get_member_role(session: AsyncSession, team_id: int, user_id: int) -> str | None:
        """查询用户在团队中的角色。

        Args:
            session: 异步数据库会话。
            team_id: 团队 ID。
            user_id: 用户 ID。

        Returns:
            角色（owner/admin/member/guest）；非成员返回 None。
        """
        stmt = select(TeamMember.role).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_my_teams(session: AsyncSession, user_id: int) -> list[tuple[Team, str]]:
        """查询用户参与的全部团队（含角色，按团队 ID 倒序）。

        Args:
            session: 异步数据库会话。
            user_id: 用户 ID。

        Returns:
            (Team, role) 元组列表。
        """
        stmt = (
            select(Team, TeamMember.role)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == user_id, Team.deleted_at.is_(None))
            .order_by(Team.id.desc())
        )
        return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]

    @staticmethod
    async def get_team_member(session: AsyncSession, team_id: int, user_id: int) -> TeamMember | None:
        """查询团队成员记录。

        Args:
            session: 异步数据库会话。
            team_id: 团队 ID。
            user_id: 用户 ID。

        Returns:
            TeamMember 实例；非成员返回 None。
        """
        stmt = select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_team_members(session: AsyncSession, team_id: int) -> list[tuple[TeamMember, User | None]]:
        """列出团队全部成员（联用户表，按加入时间升序）。

        Args:
            session: 异步数据库会话。
            team_id: 团队 ID。

        Returns:
            (TeamMember, User|None) 元组列表。
        """
        stmt = (
            select(TeamMember, User)
            .outerjoin(User, User.id == TeamMember.user_id)
            .where(TeamMember.team_id == team_id)
            .order_by(TeamMember.id.asc())
        )
        rows = (await session.execute(stmt)).all()
        return [(row[0], row[1]) for row in rows]

    @staticmethod
    async def remove_members_project_grants(
        session: AsyncSession,
        team_id: int,
        user_id: int,
    ) -> int:
        """删除用户在「归属该团队的项目」上的全部项目级授权。

        用于团队成员被移除时级联清理 project_members。

        Args:
            session: 异步数据库会话。
            team_id: 团队 ID。
            user_id: 被移除的用户 ID。

        Returns:
            删除的授权记录条数。
        """
        project_ids = select(Project.id).where(Project.team_id == team_id)
        stmt = (
            delete(ProjectMember)
            .where(ProjectMember.user_id == user_id, ProjectMember.project_id.in_(project_ids))
            .execution_options(synchronize_session=False)
        )
        result = await session.execute(stmt)
        return int(result.rowcount or 0)
