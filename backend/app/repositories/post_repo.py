"""后期域仓储模块（Task 9 · Post）。

聚合时间线（timelines / timeline_clips）、字幕样式（subtitle_styles）、
BGM 曲库（bgm_tracks）、镜头（shots / shot_versions）、媒体文件
（media_files）与审批单（approval_requests）的数据访问。

约定：
- 仓储方法不做业务校验、不抛 BizError（查不到返回 None），由服务层翻译；
- 时间线按「项目级（episode_id IS NULL）+ cut_type + version_no=1」定位；
- 镜头顺序统一为（集号升序、order_index 升序、id 升序）。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApprovalRequest,
    BgmTrack,
    Episode,
    MediaFile,
    Shot,
    ShotVersion,
    Storyboard,
    SubtitleStyle,
    Timeline,
    TimelineClip,
)


def parse_clip_content(raw: str | None) -> dict | str | None:
    """解析时间线片段的 content 字段（DB Text 列，约定存 JSON）。

    Args:
        raw: 数据库中的原始字符串。

    Returns:
        JSON 解析成功返回字典；解析失败返回原始字符串；空值返回 None。
    """
    if raw is None:
        return None
    import json

    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    return loaded if isinstance(loaded, dict) else raw


class PostRepository:
    """后期域仓储：时间线/片段/字幕样式/BGM 曲库/镜头/审批单的数据访问。

    Attributes:
        session: 异步数据库会话（事务由调用方控制）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存会话引用。

        Args:
            session: 异步数据库会话。
        """
        self.session = session

    # ---- 时间线 ----

    async def get_timeline(self, project_id: int, cut_type: str = "preview") -> Timeline | None:
        """查询项目级时间线（episode_id 为 NULL、version_no=1）。

        Args:
            project_id: 项目 ID。
            cut_type: 版本类型 preview/master。

        Returns:
            Timeline 实例；不存在返回 None。
        """
        stmt = (
            select(Timeline)
            .where(
                Timeline.project_id == project_id,
                Timeline.cut_type == cut_type,
                Timeline.episode_id.is_(None),
                Timeline.version_no == 1,
            )
            .order_by(Timeline.id.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create_timeline(self, project_id: int, cut_type: str = "preview") -> Timeline:
        """创建项目级时间线（status=draft）。

        Args:
            project_id: 项目 ID。
            cut_type: 版本类型 preview/master。

        Returns:
            新建的 Timeline 实例（已 flush）。
        """
        timeline = Timeline(
            project_id=project_id,
            episode_id=None,
            cut_type=cut_type,
            version_no=1,
            status="draft",
        )
        self.session.add(timeline)
        await self.session.flush()
        return timeline

    async def list_clips(self, timeline_id: int) -> list[TimelineClip]:
        """查询时间线全部片段（start_ms 升序）。

        Args:
            timeline_id: 时间线 ID。

        Returns:
            TimelineClip 列表。
        """
        stmt = (
            select(TimelineClip)
            .where(TimelineClip.timeline_id == timeline_id)
            .order_by(TimelineClip.start_ms.asc(), TimelineClip.track_type.asc(), TimelineClip.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_clips(self, timeline_id: int, track_type: str | None = None) -> None:
        """删除时间线片段（可按轨道类型过滤）。

        Args:
            timeline_id: 时间线 ID。
            track_type: 轨道类型；None 表示删除全部轨道。
        """
        conditions: tuple = (TimelineClip.timeline_id == timeline_id,)
        if track_type:
            conditions = (*conditions, TimelineClip.track_type == track_type)
        await self.session.execute(delete(TimelineClip).where(*conditions))

    async def add_clip(self, clip: TimelineClip) -> TimelineClip:
        """追加一个时间线片段（flush 取 ID）。

        Args:
            clip: 已构造好的 TimelineClip 实例。

        Returns:
            带 ID 的 TimelineClip 实例。
        """
        self.session.add(clip)
        await self.session.flush()
        return clip

    # ---- 字幕样式 / BGM 曲库 ----

    async def get_subtitle_style(self, style_id: int) -> SubtitleStyle | None:
        """按主键查询字幕样式。

        Args:
            style_id: 样式 ID。

        Returns:
            SubtitleStyle 实例；不存在返回 None。
        """
        return await self.session.get(SubtitleStyle, style_id)

    async def list_subtitle_styles(self, project_id: int | None = None) -> list[SubtitleStyle]:
        """查询字幕样式列表（全局 + 项目级）。

        Args:
            project_id: 项目 ID（None 仅查全局）。

        Returns:
            SubtitleStyle 列表（id 升序）。
        """
        stmt = select(SubtitleStyle).order_by(SubtitleStyle.id.asc())
        if project_id is not None:
            from sqlalchemy import or_

            stmt = stmt.where(or_(SubtitleStyle.project_id.is_(None), SubtitleStyle.project_id == project_id))
        else:
            stmt = stmt.where(SubtitleStyle.project_id.is_(None))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_bgm_track(self, track_id: int) -> BgmTrack | None:
        """按主键查询 BGM 曲目。

        Args:
            track_id: 曲目 ID。

        Returns:
            BgmTrack 实例；不存在返回 None。
        """
        return await self.session.get(BgmTrack, track_id)

    async def list_bgm_by_emotion(self, emotion: str, limit: int = 5) -> list[BgmTrack]:
        """按情绪标签查询上架 BGM 曲目（mood_tags JSONB 数组包含匹配）。

        Args:
            emotion: 情绪标签（如 tension）。
            limit: 返回条数上限。

        Returns:
            BgmTrack 列表（id 升序）。
        """
        import json

        from sqlalchemy import text

        tag_cond = text("bgm_tracks.mood_tags @> CAST(:emotion_tag AS jsonb)").bindparams(
            emotion_tag=json.dumps([emotion])
        )
        stmt = (
            select(BgmTrack)
            .where(BgmTrack.is_active.is_(True), tag_cond)
            .order_by(BgmTrack.id.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ---- 镜头 / 镜头版本 / 媒体 ----

    async def list_rendered_shots(self, project_id: int, episode_no: int | None = None) -> list[Shot]:
        """查询项目内已渲染镜头（可选按集号过滤）。

        排序：集号升序（episodes.episode_no）、order_index 升序、id 升序。

        Args:
            project_id: 项目 ID。
            episode_no: 集号过滤；None 表示全部集。

        Returns:
            Shot 列表。
        """
        stmt = (
            select(Shot)
            .join(Episode, Episode.id == Shot.episode_id)
            .where(Shot.project_id == project_id, Shot.status == "rendered")
            .order_by(Episode.episode_no.asc(), Shot.order_index.asc(), Shot.id.asc())
        )
        if episode_no is not None:
            stmt = stmt.where(Episode.episode_no == episode_no)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_current_shot_version(self, shot_id: int) -> ShotVersion | None:
        """查询镜头的当前生成版本（is_current=True）。

        Args:
            shot_id: 镜头 ID。

        Returns:
            ShotVersion 实例；不存在返回 None。
        """
        stmt = (
            select(ShotVersion)
            .where(ShotVersion.shot_id == shot_id, ShotVersion.is_current.is_(True))
            .order_by(ShotVersion.version_no.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_media_file(self, media_file_id: int) -> MediaFile | None:
        """按主键查询媒体文件。

        Args:
            media_file_id: 媒体文件 ID。

        Returns:
            MediaFile 实例；不存在返回 None。
        """
        return await self.session.get(MediaFile, media_file_id)

    async def get_storyboard(self, storyboard_id: int) -> Storyboard | None:
        """按主键查询分镜板。

        Args:
            storyboard_id: 分镜板 ID。

        Returns:
            Storyboard 实例；不存在返回 None。
        """
        return await self.session.get(Storyboard, storyboard_id)

    # ---- 审批单 ----

    async def get_pending_final_cut_approval(
        self, project_id: int, timeline_id: int
    ) -> ApprovalRequest | None:
        """查询时间线当前待审批的成片审批单（最新一条）。

        Args:
            project_id: 项目 ID。
            timeline_id: 时间线 ID。

        Returns:
            ApprovalRequest 实例（status=pending）；不存在返回 None。
        """
        stmt = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.project_id == project_id,
                ApprovalRequest.approval_type == "final_cut",
                ApprovalRequest.target_type == "timeline",
                ApprovalRequest.target_id == timeline_id,
                ApprovalRequest.status == "pending",
            )
            .order_by(ApprovalRequest.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_approval(self, approval: ApprovalRequest) -> ApprovalRequest:
        """追加审批单（flush 取 ID）。

        Args:
            approval: 已构造好的 ApprovalRequest 实例。

        Returns:
            带 ID 的 ApprovalRequest 实例。
        """
        self.session.add(approval)
        await self.session.flush()
        return approval
