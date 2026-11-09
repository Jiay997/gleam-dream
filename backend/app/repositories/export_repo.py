"""导出分发域仓储模块（Task 9 · Export & Distribution）。

聚合导出任务（export_jobs）、平台授权（platform_accounts）、分发记录
（distribution_records）的数据访问。仅封装查询/写入语句，事务与提交由
服务层/后台管线控制。

约定：
- 仓储方法不做业务校验、不抛 BizError（查不到返回 None），由服务层翻译；
- 幂等键按「export:{project_id}:{key}」拼接（项目内唯一）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DistributionRecord, ExportJob, PlatformAccount


class ExportRepository:
    """导出分发域仓储：导出任务/平台授权/分发记录的数据访问。

    Attributes:
        session: 异步数据库会话（事务由调用方控制）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存会话引用。

        Args:
            session: 异步数据库会话。
        """
        self.session = session

    # ---- 导出任务 ----

    async def get_export_job(self, job_id: int) -> ExportJob | None:
        """按主键查询导出任务。

        Args:
            job_id: 导出任务 ID。

        Returns:
            ExportJob 实例；不存在返回 None。
        """
        return await self.session.get(ExportJob, job_id)

    async def find_by_idempotency_key(self, project_id: int, key: str) -> ExportJob | None:
        """按项目 + 幂等键查询历史导出任务（幂等回放用）。

        Args:
            project_id: 项目 ID。
            key: 拼接后的完整幂等键。

        Returns:
            ExportJob 实例；不存在返回 None。
        """
        stmt = (
            select(ExportJob)
            .where(ExportJob.project_id == project_id, ExportJob.idempotency_key == key)
            .order_by(ExportJob.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_export_job(self, job: ExportJob) -> ExportJob:
        """追加导出任务（flush 取 ID）。

        Args:
            job: 已构造好的 ExportJob 实例。

        Returns:
            带 ID 的 ExportJob 实例。
        """
        self.session.add(job)
        await self.session.flush()
        return job

    async def find_by_cache_key(self, cache_key: str) -> ExportJob | None:
        """按转码缓存键查询历史导出任务（AC-F7-06-2 缓存命中复用）。

        Args:
            cache_key: 缓存键（成片版本 + 参数 hash）。

        Returns:
            命中的 ExportJob；无则 None。
        """
        stmt = (
            select(ExportJob)
            .where(ExportJob.cache_key == cache_key, ExportJob.status == "succeeded")
            .order_by(ExportJob.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_export_jobs(self, project_id: int) -> list[ExportJob]:
        """查询项目全部导出任务（创建时间降序）。

        Args:
            project_id: 项目 ID。

        Returns:
            ExportJob 列表。
        """
        stmt = (
            select(ExportJob)
            .where(ExportJob.project_id == project_id)
            .order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_export_jobs(self, project_id: int) -> int:
        """统计项目导出任务总数。

        Args:
            project_id: 项目 ID。

        Returns:
            任务总数。
        """
        stmt = select(func.count()).select_from(ExportJob).where(ExportJob.project_id == project_id)
        return int((await self.session.execute(stmt)).scalar_one())

    # ---- 平台授权 ----

    async def list_platform_accounts(self, user_id: int) -> list[PlatformAccount]:
        """查询用户全部平台授权（创建时间降序）。

        Args:
            user_id: 用户 ID。

        Returns:
            PlatformAccount 列表。
        """
        stmt = (
            select(PlatformAccount)
            .where(PlatformAccount.user_id == user_id)
            .order_by(PlatformAccount.created_at.desc(), PlatformAccount.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_platform_account(self, account_id: int) -> PlatformAccount | None:
        """按主键查询平台授权。

        Args:
            account_id: 授权 ID。

        Returns:
            PlatformAccount 实例；不存在返回 None。
        """
        return await self.session.get(PlatformAccount, account_id)

    async def get_latest_active_platform_account(
        self, user_id: int, platform: str
    ) -> PlatformAccount | None:
        """查询用户在指定平台最新的 active 授权（分发用）。

        Args:
            user_id: 用户 ID。
            platform: 平台键（douyin/kuaishou/wechat_channels/jianying）。

        Returns:
            PlatformAccount 实例；不存在返回 None。
        """
        stmt = (
            select(PlatformAccount)
            .where(
                PlatformAccount.user_id == user_id,
                PlatformAccount.platform == platform,
                PlatformAccount.status == "active",
            )
            .order_by(PlatformAccount.authorized_at.desc(), PlatformAccount.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_platform_account(self, account: PlatformAccount) -> PlatformAccount:
        """追加平台授权（flush 取 ID）。

        Args:
            account: 已构造好的 PlatformAccount 实例。

        Returns:
            带 ID 的 PlatformAccount 实例。
        """
        self.session.add(account)
        await self.session.flush()
        return account

    async def delete_platform_account(self, account_id: int) -> None:
        """按主键删除平台授权。

        Args:
            account_id: 授权 ID。
        """
        await self.session.execute(delete(PlatformAccount).where(PlatformAccount.id == account_id))

    # ---- 分发记录 ----

    async def list_distribution_records(self, project_id: int) -> list[DistributionRecord]:
        """查询项目全部分发记录（创建时间降序）。

        Args:
            project_id: 项目 ID。

        Returns:
            DistributionRecord 列表。
        """
        stmt = (
            select(DistributionRecord)
            .where(DistributionRecord.project_id == project_id)
            .order_by(DistributionRecord.created_at.desc(), DistributionRecord.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_distribution_record(self, record: DistributionRecord) -> DistributionRecord:
        """追加分发记录（flush 取 ID）。

        Args:
            record: 已构造好的 DistributionRecord 实例。

        Returns:
            带 ID 的 DistributionRecord 实例。
        """
        self.session.add(record)
        await self.session.flush()
        return record

    async def expire_platform_account(self, account: PlatformAccount, now: datetime) -> None:
        """把过期平台授权标记为 expired（分发前校验发现过期时调用）。

        Args:
            account: 平台授权实例。
            now: 当前 UTC 时间。
        """
        account.status = "expired"
        await self.session.flush()
