"""分镜域数据访问层（Repository）模块（Task 7b）。

封装分镜板、镜头、镜头版本、画布布局、审批单等 ORM 查询，供服务层
调用；统一使用 SQLAlchemy 2.0 ``select()`` 风格 + 参数化条件，禁止
拼接 SQL。

包含：
- :class:`StoryboardRepository`：分镜板/镜头/版本/布局/审批查询与维护。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collab import ApprovalRequest
from app.models.script import Episode
from app.models.storyboard import Shot, ShotVersion, Storyboard, StoryboardLayout


class StoryboardRepository:
    """分镜域查询集合（静态方法，按 session 显式传参）。"""

    # ---- 分镜板 ----

    @staticmethod
    async def get_by_id(session: AsyncSession, storyboard_id: int) -> Storyboard | None:
        """按 ID 查询分镜板。

        Args:
            session: 异步数据库会话。
            storyboard_id: 分镜板 ID。

        Returns:
            分镜板实例；无则 None。
        """
        return await session.get(Storyboard, storyboard_id)

    @staticmethod
    async def get_by_episode(session: AsyncSession, episode_id: int) -> Storyboard | None:
        """按集 ID 查询分镜板（generate 幂等判断）。

        Args:
            session: 异步数据库会话。
            episode_id: 集 ID。

        Returns:
            分镜板实例；无则 None。
        """
        stmt = (
            select(Storyboard)
            .where(Storyboard.episode_id == episode_id)
            .order_by(Storyboard.version_no.desc(), Storyboard.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_by_project(session: AsyncSession, project_id: int) -> list[tuple[Storyboard, int]]:
        """查询项目全部分镜板（联分集表取集号，按集号升序）。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。

        Returns:
            (Storyboard, episode_no) 元组列表。
        """
        stmt = (
            select(Storyboard, Episode.episode_no)
            .join(Episode, Episode.id == Storyboard.episode_id)
            .where(Storyboard.project_id == project_id)
            .order_by(Episode.episode_no.asc(), Storyboard.version_no.desc())
        )
        return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]

    # ---- 镜头 ----

    @staticmethod
    async def list_shots(session: AsyncSession, storyboard_id: int) -> list[Shot]:
        """按展示序查询分镜板全部镜头。

        Args:
            session: 异步数据库会话。
            storyboard_id: 分镜板 ID。

        Returns:
            镜头实例列表（order_index 升序）。
        """
        stmt = (
            select(Shot)
            .where(Shot.storyboard_id == storyboard_id)
            .order_by(Shot.order_index.asc(), Shot.id.asc())
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def get_shot(session: AsyncSession, shot_id: int) -> Shot | None:
        """按 ID 查询镜头。

        Args:
            session: 异步数据库会话。
            shot_id: 镜头 ID。

        Returns:
            镜头实例；无则 None。
        """
        return await session.get(Shot, shot_id)

    @staticmethod
    async def max_shot_no(session: AsyncSession, storyboard_id: int) -> int:
        """查询分镜板当前最大镜号（无镜头时为 0）。

        Args:
            session: 异步数据库会话。
            storyboard_id: 分镜板 ID。

        Returns:
            最大镜号（≥0）。
        """
        stmt = select(func.max(Shot.shot_no)).where(Shot.storyboard_id == storyboard_id)
        return int((await session.execute(stmt)).scalar_one_or_none() or 0)

    @staticmethod
    async def count_shots(session: AsyncSession, storyboard_id: int) -> int:
        """统计分镜板镜头总数。

        Args:
            session: 异步数据库会话。
            storyboard_id: 分镜板 ID。

        Returns:
            镜头数量。
        """
        stmt = select(func.count()).select_from(Shot).where(Shot.storyboard_id == storyboard_id)
        return int((await session.execute(stmt)).scalar_one())

    # ---- 镜头版本（首帧图） ----

    @staticmethod
    async def current_shot_version(session: AsyncSession, shot_id: int) -> ShotVersion | None:
        """查询镜头当前生成版本（is_current，取最新一条）。

        Args:
            session: 异步数据库会话。
            shot_id: 镜头 ID。

        Returns:
            当前版本实例；无则 None。
        """
        stmt = (
            select(ShotVersion)
            .where(ShotVersion.shot_id == shot_id, ShotVersion.is_current.is_(True))
            .order_by(ShotVersion.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # ---- 画布布局 ----

    @staticmethod
    async def get_layout(
        session: AsyncSession, storyboard_id: int, user_id: int
    ) -> StoryboardLayout | None:
        """查询用户在分镜板上的画布布局（upsert 前置查询）。

        Args:
            session: 异步数据库会话。
            storyboard_id: 分镜板 ID。
            user_id: 用户 ID。

        Returns:
            布局实例；无则 None。
        """
        stmt = select(StoryboardLayout).where(
            StoryboardLayout.storyboard_id == storyboard_id,
            StoryboardLayout.user_id == user_id,
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # ---- 审批单 ----

    @staticmethod
    async def get_pending_approval(
        session: AsyncSession, storyboard_id: int
    ) -> ApprovalRequest | None:
        """查询分镜板最新的 pending 审批单（分镜 decide 用）。

        Args:
            session: 异步数据库会话。
            storyboard_id: 分镜板 ID（approval_requests.target_id）。

        Returns:
            pending 审批单；无则 None。
        """
        stmt = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.approval_type == "storyboard",
                ApprovalRequest.target_id == storyboard_id,
                ApprovalRequest.status == "pending",
            )
            .order_by(ApprovalRequest.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()
