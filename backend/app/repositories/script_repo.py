"""剧本域数据仓储模块（Task 7a）。

封装剧本域（documents/scripts/episodes/scenes/script_lines/
parse_confidence_items）的常用查询与写入，全部使用 SQLAlchemy 参数化
查询。供 :mod:`app.services.script_service` 与路由层复用。

设计约定：
- 仓储不提交事务（commit 由服务层/调用方控制），保证多表写入的原子性；
- 列表查询统一按 order_index / version_no 等稳定序输出。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.iam import OperationLog
from app.models.script import (
    Document,
    Episode,
    ParseConfidenceItem,
    Scene,
    Script,
    ScriptLine,
)


class ScriptRepository:
    """剧本域仓储：集中承载剧本域表的查询与小粒度写入。

    Attributes:
        session: 请求级/任务级异步数据库会话。
    """

    def __init__(self, session: AsyncSession) -> None:
        """构造仓储。

        Args:
            session: 异步数据库会话。
        """
        self.session = session

    # ---- documents ----

    async def get_document(self, document_id: int) -> Document | None:
        """按 ID 查询上传文档。

        Args:
            document_id: 文档 ID。

        Returns:
            文档实例；不存在返回 None。
        """
        return await self.session.get(Document, document_id)

    # ---- scripts ----

    async def get_script(self, script_id: int) -> Script | None:
        """按 ID 查询剧本版本。

        Args:
            script_id: 剧本版本 ID。

        Returns:
            剧本版本实例；不存在返回 None。
        """
        return await self.session.get(Script, script_id)

    async def latest_script(self, project_id: int) -> Script | None:
        """查询项目当前（version_no 最大）的剧本版本。

        Args:
            project_id: 项目 ID。

        Returns:
            最新剧本版本实例；项目尚无剧本时返回 None。
        """
        stmt = (
            select(Script)
            .where(Script.project_id == project_id)
            .order_by(Script.version_no.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def script_by_version(self, project_id: int, version_no: int) -> Script | None:
        """按版本号查询项目的剧本版本。

        Args:
            project_id: 项目 ID。
            version_no: 版本号。

        Returns:
            对应版本实例；不存在返回 None。
        """
        stmt = select(Script).where(
            Script.project_id == project_id,
            Script.version_no == version_no,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def next_version_no(self, project_id: int) -> int:
        """计算项目下一个剧本版本号（当前最大值 + 1，无剧本时为 1）。

        Args:
            project_id: 项目 ID。

        Returns:
            下一个版本号（≥1）。
        """
        current_max = (
            await self.session.execute(
                select(func.max(Script.version_no)).where(Script.project_id == project_id)
            )
        ).scalar_one()
        return int(current_max or 0) + 1

    async def list_versions(self, project_id: int, limit: int = 50) -> list[Script]:
        """查询项目的剧本版本列表（按版本号倒序，最新在前）。

        Args:
            project_id: 项目 ID。
            limit: 返回条数上限（默认 50）。

        Returns:
            剧本版本实例列表。
        """
        stmt = (
            select(Script)
            .where(Script.project_id == project_id)
            .order_by(Script.version_no.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_versions(self, project_id: int) -> int:
        """统计项目的剧本版本总数（版本上限淘汰判定用，AC-F2-11-3）。

        Args:
            project_id: 项目 ID。

        Returns:
            版本数量。
        """
        total = (
            await self.session.execute(
                select(func.count(Script.id)).where(Script.project_id == project_id)
            )
        ).scalar_one()
        return int(total or 0)

    async def oldest_version(self, project_id: int) -> Script | None:
        """取项目最旧的剧本版本（版本超限时按时间淘汰）。

        Args:
            project_id: 项目 ID。

        Returns:
            最旧版本实例；无版本返回 None。
        """
        stmt = (
            select(Script)
            .where(Script.project_id == project_id)
            .order_by(Script.version_no.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- episodes / scenes / lines ----

    async def episodes_of(self, script_id: int) -> list[Episode]:
        """查询剧本版本下的全部分集（按 episode_no 升序）。

        Args:
            script_id: 剧本版本 ID。

        Returns:
            分集实例列表。
        """
        stmt = (
            select(Episode)
            .where(Episode.script_id == script_id)
            .order_by(Episode.episode_no.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def scenes_of(self, episode_ids: list[int]) -> list[Scene]:
        """查询一批分集下的全部场次（按集序/场序稳定排序）。

        Args:
            episode_ids: 分集 ID 列表。

        Returns:
            场次实例列表（空列表时返回空）。
        """
        if not episode_ids:
            return []
        stmt = (
            select(Scene)
            .where(Scene.episode_id.in_(episode_ids))
            .order_by(Scene.episode_id.asc(), Scene.scene_no.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lines_of(self, scene_ids: list[int]) -> list[ScriptLine]:
        """查询一批场次下的全部行（按场序/行序稳定排序）。

        Args:
            scene_ids: 场次 ID 列表。

        Returns:
            剧本行实例列表（空列表时返回空）。
        """
        if not scene_ids:
            return []
        stmt = (
            select(ScriptLine)
            .where(ScriptLine.scene_id.in_(scene_ids))
            .order_by(ScriptLine.scene_id.asc(), ScriptLine.line_no.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_line(self, line_id: int) -> ScriptLine | None:
        """按 ID 查询剧本行。

        Args:
            line_id: 剧本行 ID。

        Returns:
            剧本行实例；不存在返回 None。
        """
        return await self.session.get(ScriptLine, line_id)

    async def delete_lines_of_scene(self, scene_id: int) -> int:
        """删除场次下的全部行（改写替换用），返回删除数量。

        Args:
            scene_id: 场次 ID。

        Returns:
            被删除的行数。
        """
        rows = list(
            (
                await self.session.execute(
                    select(ScriptLine).where(ScriptLine.scene_id == scene_id)
                )
            ).scalars()
        )
        for row in rows:
            await self.session.delete(row)
        return len(rows)

    # ---- parse_confidence_items ----

    async def confidence_page(
        self,
        document_id: int,
        *,
        page: int,
        page_size: int,
        needs_manual: bool | None = None,
    ) -> tuple[list[ParseConfidenceItem], int]:
        """分页查询文档的字段级置信度项。

        Args:
            document_id: 文档 ID。
            page: 页码（≥1）。
            page_size: 页大小（≥1）。
            needs_manual: 可选过滤（True 仅待人工项 / False 仅已处理项）。

        Returns:
            (当前页置信度项列表, 总数)。
        """
        base = select(ParseConfidenceItem).where(ParseConfidenceItem.document_id == document_id)
        count_stmt = (
            select(func.count(ParseConfidenceItem.id))
            .where(ParseConfidenceItem.document_id == document_id)
        )
        if needs_manual is not None:
            base = base.where(ParseConfidenceItem.needs_manual.is_(needs_manual))
            count_stmt = count_stmt.where(ParseConfidenceItem.needs_manual.is_(needs_manual))
        total = int((await self.session.execute(count_stmt)).scalar_one())
        rows = (
            await self.session.execute(
                base.order_by(ParseConfidenceItem.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return list(rows), total

    async def get_confidence_item(self, item_id: int) -> ParseConfidenceItem | None:
        """按 ID 查询置信度项。

        Args:
            item_id: 置信度项 ID。

        Returns:
            置信度项实例；不存在返回 None。
        """
        return await self.session.get(ParseConfidenceItem, item_id)

    async def overall_confidence(self, document_id: int) -> Decimal | None:
        """计算文档全部置信度项的均值（无项时返回 None）。

        Args:
            document_id: 文档 ID。

        Returns:
            置信度均值（0~1）或 None。
        """
        stmt = select(func.avg(ParseConfidenceItem.confidence)).where(
            ParseConfidenceItem.document_id == document_id
        )
        value = (await self.session.execute(stmt)).scalar_one()
        return Decimal(str(value)) if value is not None else None

    async def manual_pending_count(self, document_id: int) -> int:
        """统计文档中待人工校正（needs_manual=true）的置信项数量。

        Args:
            document_id: 文档 ID。

        Returns:
            待人工校正数量。
        """
        stmt = (
            select(func.count(ParseConfidenceItem.id))
            .where(
                ParseConfidenceItem.document_id == document_id,
                ParseConfidenceItem.needs_manual.is_(True),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def latest_script_by_document(self, document_id: int) -> Script | None:
        """查询某文档解析产出的最新剧本版本。

        Args:
            document_id: 文档 ID。

        Returns:
            剧本版本实例；无则 None。
        """
        stmt = (
            select(Script)
            .where(Script.document_id == document_id)
            .order_by(Script.version_no.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- operation_logs ----

    async def add_operation_log(
        self,
        *,
        actor_user_id: int | None,
        project_id: int | None,
        entity_type: str,
        entity_id: int | None,
        diff: dict[str, Any],
        request_id: str | None = None,
    ) -> OperationLog:
        """追加一条操作日志（diff 摘要留痕，只写不改）。

        Args:
            actor_user_id: 操作者用户 ID。
            project_id: 项目 ID。
            entity_type: 实体类型（如 script_line/script/document）。
            entity_id: 实体 ID。
            diff: 变更差异摘要（JSONB）。
            request_id: 请求 ID（ULID，可空）。

        Returns:
            已加入会话的 OperationLog 实例（未提交）。
        """
        log = OperationLog(
            actor_user_id=actor_user_id,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action="update",
            diff=diff,
            request_id=request_id,
        )
        self.session.add(log)
        return log
