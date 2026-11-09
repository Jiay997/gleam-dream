"""资产域数据访问层（Repository）模块（Task 7b）。

封装资产、资产版本、人脸向量、质检报告、音色库等 ORM 查询，供
服务层调用；统一使用 SQLAlchemy 2.0 ``select()`` 风格 + 参数化条件，
禁止拼接 SQL。

包含：
- :class:`AssetRepository`：资产主表/版本/QC/音色查询与维护。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import (
    Asset,
    AssetVersion,
    FaceAuthorization,
    FaceEmbedding,
    MediaFile,
    TtsVoice,
)
from app.models.quality import QcReport
from app.models.storyboard import Shot


class AssetRepository:
    """资产域查询集合（静态方法，按 session 显式传参）。"""

    # ---- 资产主表 ----

    @staticmethod
    async def get_by_id(session: AsyncSession, asset_id: int) -> Asset | None:
        """按 ID 查询存活资产（排除软删除）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。

        Returns:
            资产 ORM 实例；不存在或已软删返回 None。
        """
        stmt = select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None))
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_by_project(
        session: AsyncSession,
        project_id: int,
        *,
        asset_type: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        labels: list[str] | None = None,
    ) -> tuple[list[Asset], int]:
        """分页查询项目资产列表（类型/状态/关键词/标签多条件筛选）。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。
            asset_type: 资产类型筛选（None 不过滤）。
            status: 状态筛选（None 不过滤）。
            page: 页码（从 1 起）。
            page_size: 每页条数。
            search: 名称前缀/包含匹配关键词（AC-F3-11-1，大小写不敏感）。
            labels: 标签多选 AND 过滤（AC-F3-11-1）。

        Returns:
            (当前页资产列表, 满足条件总数)。
        """
        stmt: Select = select(Asset).where(
            Asset.project_id == project_id, Asset.deleted_at.is_(None)
        )
        if asset_type:
            stmt = stmt.where(Asset.asset_type == asset_type)
        if status:
            stmt = stmt.where(Asset.status == status)
        if search:
            keyword = search.strip()
            if keyword:
                # name 模糊 + labels 数组包含（同一关键词命中任一即可）
                stmt = stmt.where(
                    or_(
                        Asset.name.ilike(f"%{keyword}%"),
                        Asset.labels.op("@>")(func.jsonb_build_array(keyword)),
                    )
                )
        for label in labels or []:
            if label.strip():
                stmt = stmt.where(
                    Asset.labels.op("@>")(func.jsonb_build_array(label.strip()))
                )
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        stmt = stmt.order_by(Asset.id.asc()).offset((page - 1) * page_size).limit(page_size)
        items = list((await session.execute(stmt)).scalars().all())
        return items, int(total)

    @staticmethod
    async def find_same_name(
        session: AsyncSession, project_id: int, asset_type: str, name: str
    ) -> Asset | None:
        """查询同项目下同类型同名资产（plan 幂等复用判断）。

        Args:
            session: 异步数据库会话。
            project_id: 项目 ID。
            asset_type: 资产类型。
            name: 资产名称。

        Returns:
            命中的资产实例；无则 None。
        """
        stmt = select(Asset).where(
            Asset.project_id == project_id,
            Asset.asset_type == asset_type,
            Asset.name == name,
            Asset.deleted_at.is_(None),
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_by_ids(session: AsyncSession, asset_ids: list[int]) -> list[Asset]:
        """按 ID 批量查询资产（保持传入顺序）。

        Args:
            session: 异步数据库会话。
            asset_ids: 资产 ID 列表。

        Returns:
            资产实例列表（顺序与传入一致；缺失的 ID 不出现）。
        """
        if not asset_ids:
            return []
        stmt = select(Asset).where(Asset.id.in_(asset_ids), Asset.deleted_at.is_(None))
        found = {int(a.id): a for a in (await session.execute(stmt)).scalars().all()}
        return [found[i] for i in asset_ids if i in found]

    # ---- 资产版本 ----

    @staticmethod
    async def list_versions(session: AsyncSession, asset_id: int) -> list[AssetVersion]:
        """查询资产全部版本（版本号倒序）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。

        Returns:
            版本实例列表（最新在前）。
        """
        stmt = (
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id)
            .order_by(AssetVersion.version_no.desc(), AssetVersion.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def get_version(session: AsyncSession, version_id: int) -> AssetVersion | None:
        """按 ID 查询资产版本。

        Args:
            session: 异步数据库会话。
            version_id: 版本 ID。

        Returns:
            版本实例；无则 None。
        """
        return await session.get(AssetVersion, version_id)

    @staticmethod
    async def latest_version(
        session: AsyncSession, asset_id: int, version_type: str
    ) -> AssetVersion | None:
        """查询资产指定类型的最新版本（版本号最大）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。
            version_type: 版本类型。

        Returns:
            最新版本实例；无则 None。
        """
        stmt = (
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id, AssetVersion.version_type == version_type)
            .order_by(AssetVersion.version_no.desc(), AssetVersion.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def first_version(
        session: AsyncSession, asset_id: int, version_type: str
    ) -> AssetVersion | None:
        """查询资产指定类型的首个版本（版本号最小，portrait 基准用）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。
            version_type: 版本类型。

        Returns:
            首个版本实例；无则 None。
        """
        stmt = (
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id, AssetVersion.version_type == version_type)
            .order_by(AssetVersion.version_no.asc(), AssetVersion.id.asc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def next_version_no(
        session: AsyncSession, asset_id: int, version_type: str
    ) -> int:
        """计算指定资产指定类型的下一个版本号（当前最大 + 1，无则 1）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。
            version_type: 版本类型。

        Returns:
            下一个版本号（≥1）。
        """
        stmt = select(func.max(AssetVersion.version_no)).where(
            AssetVersion.asset_id == asset_id, AssetVersion.version_type == version_type
        )
        current = (await session.execute(stmt)).scalar_one_or_none()
        return int(current or 0) + 1

    @staticmethod
    async def clear_current(
        session: AsyncSession, asset_id: int, version_type: str
    ) -> int:
        """将资产指定类型的全部当前版本置为非当前（新版本就位前调用）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。
            version_type: 版本类型。

        Returns:
            被更新的行数。
        """
        stmt = select(AssetVersion).where(
            AssetVersion.asset_id == asset_id,
            AssetVersion.version_type == version_type,
            AssetVersion.is_current.is_(True),
        )
        rows = list((await session.execute(stmt)).scalars().all())
        for row in rows:
            row.is_current = False
        return len(rows)

    # ---- 人脸向量 ----

    @staticmethod
    async def get_embedding_by_version(
        session: AsyncSession, asset_version_id: int
    ) -> FaceEmbedding | None:
        """按资产版本 ID 查询其人脸向量（QC 基准/比对用）。

        Args:
            session: 异步数据库会话。
            asset_version_id: 资产版本 ID。

        Returns:
            人脸向量实例；无则 None。
        """
        stmt = (
            select(FaceEmbedding)
            .where(FaceEmbedding.asset_version_id == asset_version_id)
            .order_by(FaceEmbedding.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # ---- 媒体文件 ----

    @staticmethod
    async def get_media_file(session: AsyncSession, media_file_id: int | None) -> MediaFile | None:
        """按 ID 查询媒体文件。

        Args:
            session: 异步数据库会话。
            media_file_id: 媒体文件 ID（可空）。

        Returns:
            媒体文件实例；ID 为空或不存在返回 None。
        """
        if media_file_id is None:
            return None
        return await session.get(MediaFile, media_file_id)

    # ---- QC 报告 ----

    @staticmethod
    async def list_qc_reports(
        session: AsyncSession, target_type: str, target_id: int
    ) -> list[QcReport]:
        """按多态目标查询质检报告（ID 倒序，最新在前）。

        Args:
            session: 异步数据库会话。
            target_type: 目标类型（shot/asset/script/timeline/output）。
            target_id: 目标 ID。

        Returns:
            QC 报告实例列表。
        """
        stmt = (
            select(QcReport)
            .where(QcReport.target_type == target_type, QcReport.target_id == target_id)
            .order_by(QcReport.id.desc())
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def get_qc_report(session: AsyncSession, qc_report_id: int | None) -> QcReport | None:
        """按 ID 查询质检报告。

        Args:
            session: 异步数据库会话。
            qc_report_id: 报告 ID（可空）。

        Returns:
            QC 报告实例；ID 为空或不存在返回 None。
        """
        if qc_report_id is None:
            return None
        return await session.get(QcReport, qc_report_id)

    # ---- 锚点影响面 ----

    @staticmethod
    async def count_anchor_shots(session: AsyncSession, asset_id: int) -> int:
        """统计资产作为锚点出现的镜头数（character_asset_ids JSONB 包含匹配）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。

        Returns:
            引用该资产的镜头数量。
        """
        condition: ColumnElement[bool] = Shot.character_asset_ids.contains([asset_id])  # type: ignore[attr-defined]
        stmt = select(func.count()).select_from(Shot).where(condition)
        return int((await session.execute(stmt)).scalar_one())

    # ---- 音色库 ----

    @staticmethod
    async def get_voice(session: AsyncSession, voice_id: int) -> TtsVoice | None:
        """按 ID 查询音色。

        Args:
            session: 异步数据库会话。
            voice_id: 音色 ID。

        Returns:
            音色实例；无则 None。
        """
        return await session.get(TtsVoice, voice_id)

    @staticmethod
    async def list_voices(
        session: AsyncSession,
        *,
        status: str | None = "available",
        gender: str | None = None,
        exclude_test: bool = True,
        voice_keys: list[str] | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[TtsVoice], int]:
        """分页查询音色库（默认仅可用音色，ID 升序）。

        Args:
            session: 异步数据库会话。
            status: 状态筛选（None 查全部）。
            gender: 性别筛选（male/female/neutral，None 不筛选）。
            exclude_test: 是否排除自动化测试写入的音色（voice_key 以
                test-voice / v6-voice 开头或名称以「测试音色」开头）。
                修复 E2E 报告问题 9：测试数据曾污染下拉至 101 项。
            voice_keys: 仅返回这些业务音色键（用于过滤未映射到真实引擎
                音色的条目，避免用户选中后音色漂移）。
            page: 页码（从 1 起）。
            page_size: 每页条数。

        Returns:
            (当前页音色列表, 总数)。
        """
        stmt: Select = select(TtsVoice)
        if status:
            stmt = stmt.where(TtsVoice.status == status)
        if gender:
            stmt = stmt.where(TtsVoice.gender == gender)
        if exclude_test:
            stmt = stmt.where(
                TtsVoice.voice_key.not_like("test-voice%"),
                TtsVoice.voice_key.not_like("v6-voice%"),
                TtsVoice.name.not_like("测试音色%"),
            )
        if voice_keys:
            stmt = stmt.where(TtsVoice.voice_key.in_(list(voice_keys)))
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        stmt = stmt.order_by(TtsVoice.id.asc()).offset((page - 1) * page_size).limit(page_size)
        items = list((await session.execute(stmt)).scalars().all())
        return items, int(total)

    # ---- 真人授权记录（V4.0 US-F3-12）----

    @staticmethod
    async def list_authorizations(
        session: AsyncSession,
        *,
        user_id: int,
        project_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FaceAuthorization], int]:
        """分页查询真人授权记录（按提交人，可选项目/状态筛选）。

        Args:
            session: 异步数据库会话。
            user_id: 授权提交人用户 ID。
            project_id: 项目 ID 筛选（None 不过滤）。
            status: 状态筛选 active/revoked（None 不过滤）。
            page: 页码（从 1 起）。
            page_size: 每页条数。

        Returns:
            (当前页授权记录列表, 总数)，按授权时间倒序。
        """
        stmt: Select = select(FaceAuthorization).where(
            FaceAuthorization.user_id == user_id
        )
        if project_id is not None:
            stmt = stmt.where(FaceAuthorization.project_id == project_id)
        if status:
            stmt = stmt.where(FaceAuthorization.status == status)
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        stmt = (
            stmt.order_by(FaceAuthorization.authorized_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await session.execute(stmt)).scalars().all())
        return items, int(total)

    @staticmethod
    async def get_authorization(
        session: AsyncSession, authorization_id: int
    ) -> FaceAuthorization | None:
        """按 ID 查询真人授权记录。

        Args:
            session: 异步数据库会话。
            authorization_id: 授权记录 ID。

        Returns:
            授权记录实例；无则 None。
        """
        return await session.get(FaceAuthorization, authorization_id)

    @staticmethod
    async def active_authorization(
        session: AsyncSession, asset_id: int
    ) -> FaceAuthorization | None:
        """查询资产当前生效的真人授权记录（status=active）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。

        Returns:
            生效授权记录；无则 None。
        """
        stmt = (
            select(FaceAuthorization)
            .where(
                FaceAuthorization.asset_id == asset_id,
                FaceAuthorization.status == "active",
            )
            .order_by(FaceAuthorization.id.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def list_asset_versions_by_variant(
        session: AsyncSession, asset_id: int, variant_tag: str
    ) -> list[AssetVersion]:
        """按变体标签查询资产版本（场景变体列表，AC-F3-08-1）。

        Args:
            session: 异步数据库会话。
            asset_id: 资产 ID。
            variant_tag: 变体标签。

        Returns:
            版本列表（版本号升序）。
        """
        stmt = (
            select(AssetVersion)
            .where(
                AssetVersion.asset_id == asset_id,
                AssetVersion.variant_tag == variant_tag,
            )
            .order_by(AssetVersion.version_no.asc())
        )
        return list((await session.execute(stmt)).scalars().all())
