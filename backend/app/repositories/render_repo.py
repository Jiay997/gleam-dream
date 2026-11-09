"""生成执行域仓储模块（Task 8 · Render）。

聚合渲染任务（render_tasks / render_outputs）、镜头（shots / shot_versions）、
资产锚点向量（face_embeddings）、质检报告（qc_reports）、媒体文件
（media_files）、冻结记录（credit_freezes）与 AI 调用日志（ai_call_logs）
的数据访问。仅封装查询/写入语句，事务与提交由服务层/执行器控制。

约定：
- 仓储方法不做业务校验、不抛 BizError（查不到返回 None），由服务层翻译；
- 所有列表查询的排序保持确定性（id 升序兜底）。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiCallLog,
    Asset,
    AssetVersion,
    CreditFreeze,
    FaceEmbedding,
    MediaFile,
    Plan,
    QcReport,
    RenderOutput,
    RenderTask,
    Shot,
    ShotVersion,
    Storyboard,
    Subscription,
)


class RenderRepository:
    """渲染任务仓储：渲染任务/产物/镜头版本/锚点向量/QC 报告的数据访问。

    Attributes:
        session: 异步数据库会话（事务由调用方控制）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存会话引用。

        Args:
            session: 异步数据库会话。
        """
        self.session = session

    # ---- 渲染任务 ----

    async def get_task(self, task_id: int) -> RenderTask | None:
        """按主键查询渲染任务。

        Args:
            task_id: 渲染任务 ID。

        Returns:
            RenderTask 实例；不存在返回 None。
        """
        return await self.session.get(RenderTask, task_id)

    async def list_tasks_by_idempotency_prefix(
        self, project_id: int, key_prefix: str
    ) -> list[RenderTask]:
        """按幂等键前缀查询同批渲染任务（批次幂等回放用）。

        Args:
            project_id: 项目 ID。
            key_prefix: 幂等键前缀（如 ``batch-xxx:``）。

        Returns:
            命中的渲染任务列表（id 升序）。
        """
        stmt = (
            select(RenderTask)
            .where(
                RenderTask.project_id == project_id,
                RenderTask.idempotency_key.like(f"{key_prefix}%"),
            )
            .order_by(RenderTask.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_reusable_success(
        self, project_id: int, shot_id: int, input_hash: str
    ) -> RenderTask | None:
        """查询同镜头同入参哈希且已成功的任务（二次幂等复用产物）。

        Args:
            project_id: 项目 ID。
            shot_id: 镜头 ID。
            input_hash: 入参哈希（SHA-256）。

        Returns:
            最近一次成功的 RenderTask；无则 None。
        """
        stmt = (
            select(RenderTask)
            .where(
                RenderTask.project_id == project_id,
                RenderTask.shot_id == shot_id,
                RenderTask.input_hash == input_hash,
                RenderTask.status == "succeeded",
            )
            .order_by(RenderTask.finished_at.desc().nullslast(), RenderTask.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_tasks(
        self,
        project_id: int,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> tuple[list[RenderTask], int]:
        """分页查询项目渲染任务列表。

        Args:
            project_id: 项目 ID。
            status: 状态筛选（None 全部）。
            page: 页码（≥1）。
            page_size: 每页条数（1~100）。
            sort: 排序，仅支持 ``-created_at`` / ``created_at``。

        Returns:
            ``(任务列表, 总数)`` 二元组。
        """
        conditions: tuple = (RenderTask.project_id == project_id,)
        if status:
            conditions = (*conditions, RenderTask.status == status)
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(RenderTask).where(*conditions)
                )
            ).scalar_one()
        )
        order_col = (
            RenderTask.created_at.asc() if sort == "created_at" else RenderTask.created_at.desc()
        )
        stmt = (
            select(RenderTask)
            .where(*conditions)
            .order_by(order_col, RenderTask.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list((await self.session.execute(stmt)).scalars().all()), total

    async def count_active_for_project(self, project_id: int) -> int:
        """统计项目内仍在执行中的任务数（pending/queued/running/retrying/qc_review）。

        Args:
            project_id: 项目 ID。

        Returns:
            活跃任务数。
        """
        stmt = (
            select(func.count())
            .select_from(RenderTask)
            .where(
                RenderTask.project_id == project_id,
                RenderTask.status.in_(("pending", "queued", "running", "retrying", "qc_review")),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_tasks_by_batch_key(self, project_id: int, batch_key: str) -> list[RenderTask]:
        """按批次键（input_params.batch_key）查询同批全部任务。

        Args:
            project_id: 项目 ID。
            batch_key: 批次键（Idempotency-Key 或 regenerate 生成键）。

        Returns:
            同批渲染任务列表（id 升序）。
        """
        stmt = (
            select(RenderTask)
            .where(
                RenderTask.project_id == project_id,
                RenderTask.input_params["batch_key"].astext == batch_key,
            )
            .order_by(RenderTask.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_recoverable_tasks(self) -> list[RenderTask]:
        """查询崩溃恢复扫描的目标任务（queued/running/retrying/pending）。

        Returns:
            待重新入队的渲染任务列表（priority, id 升序）。
        """
        stmt = (
            select(RenderTask)
            .where(RenderTask.status.in_(("pending", "queued", "running", "retrying")))
            .order_by(RenderTask.priority.asc(), RenderTask.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ---- 镜头与分镜板 ----

    async def get_shot(self, shot_id: int) -> Shot | None:
        """按主键查询镜头。

        Args:
            shot_id: 镜头 ID。

        Returns:
            Shot 实例；不存在返回 None。
        """
        return await self.session.get(Shot, shot_id)

    async def get_storyboard(self, storyboard_id: int) -> Storyboard | None:
        """按主键查询分镜板。

        Args:
            storyboard_id: 分镜板 ID。

        Returns:
            Storyboard 实例；不存在返回 None。
        """
        return await self.session.get(Storyboard, storyboard_id)

    async def list_shots_by_ids(self, shot_ids: list[int]) -> list[Shot]:
        """按 ID 集合查询镜头（保持入参顺序，缺失的 ID 不返回）。

        Args:
            shot_ids: 镜头 ID 列表。

        Returns:
            与入参同序的 Shot 列表。
        """
        if not shot_ids:
            return []
        stmt = select(Shot).where(Shot.id.in_(shot_ids))
        by_id = {int(row.id): row for row in (await self.session.execute(stmt)).scalars().all()}
        return [by_id[sid] for sid in shot_ids if sid in by_id]

    async def list_renderable_shots(self, project_id: int) -> list[Shot]:
        """查询项目内可提交渲染的镜头（batch 空 shot_ids 时使用）。

        修复：旧实现只取 ``status=locked``，而渲染成功后镜头会变为
        ``rendered``；一旦重新合成首帧（画面修复），这些镜头无法再次批量
        提交，只能逐镜 regenerate。现允许 locked/rendered/render_failed。

        Args:
            project_id: 项目 ID。

        Returns:
            按 (storyboard_id, order_index, id) 排序的可渲染镜头列表。
        """
        stmt = (
            select(Shot)
            .where(
                Shot.project_id == project_id,
                Shot.status.in_(("locked", "rendered", "render_failed")),
            )
            .order_by(Shot.storyboard_id.asc(), Shot.order_index.asc(), Shot.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ---- 镜头版本 / 产物 / QC ----

    async def list_shot_versions(self, shot_id: int) -> list[ShotVersion]:
        """查询镜头全部生成版本（version_no 升序）。

        Args:
            shot_id: 镜头 ID。

        Returns:
            ShotVersion 列表。
        """
        stmt = (
            select(ShotVersion)
            .where(ShotVersion.shot_id == shot_id)
            .order_by(ShotVersion.version_no.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_shot_version(self, version_id: int) -> None:
        """按主键删除镜头版本（保留 3 版策略删除最旧版本）。

        Args:
            version_id: 版本 ID。
        """
        await self.session.execute(delete(ShotVersion).where(ShotVersion.id == version_id))

    async def list_outputs(self, render_task_id: int) -> list[RenderOutput]:
        """查询渲染任务的产物列表（output_index 升序）。

        Args:
            render_task_id: 渲染任务 ID。

        Returns:
            RenderOutput 列表。
        """
        stmt = (
            select(RenderOutput)
            .where(RenderOutput.render_task_id == render_task_id)
            .order_by(RenderOutput.output_index.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_qc_reports_for_shot(self, shot_id: int) -> list[QcReport]:
        """查询镜头的质检报告（round_no 升序）。

        Args:
            shot_id: 镜头 ID。

        Returns:
            QcReport 列表。
        """
        stmt = (
            select(QcReport)
            .where(QcReport.target_type == "shot", QcReport.target_id == shot_id)
            .order_by(QcReport.round_no.asc(), QcReport.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ---- 资产锚点向量 ----

    async def get_portrait_embedding(
        self, asset_version_id: int
    ) -> FaceEmbedding | None:
        """查询资产版本的人脸向量（任取一条，伪向量场景每版本一条）。

        Args:
            asset_version_id: 资产版本 ID。

        Returns:
            FaceEmbedding 实例；不存在返回 None。
        """
        stmt = (
            select(FaceEmbedding)
            .where(FaceEmbedding.asset_version_id == asset_version_id)
            .order_by(FaceEmbedding.id.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_asset_version_asset(self, asset_version_id: int) -> Asset | None:
        """查询资产版本所属资产（判断是否角色类型）。

        Args:
            asset_version_id: 资产版本 ID。

        Returns:
            Asset 实例；版本不存在返回 None。
        """
        stmt = (
            select(Asset)
            .join(AssetVersion, AssetVersion.asset_id == Asset.id)
            .where(AssetVersion.id == asset_version_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- 媒体文件 ----

    async def get_media_file(self, media_file_id: int) -> MediaFile | None:
        """按主键查询媒体文件。

        Args:
            media_file_id: 媒体文件 ID。

        Returns:
            MediaFile 实例；不存在返回 None。
        """
        return await self.session.get(MediaFile, media_file_id)

    # ---- 计费 ----

    async def get_freeze(self, freeze_id: int) -> CreditFreeze | None:
        """按主键查询冻结记录。

        Args:
            freeze_id: 冻结记录 ID。

        Returns:
            CreditFreeze 实例；不存在返回 None。
        """
        return await self.session.get(CreditFreeze, freeze_id)

    async def get_user_plan_concurrency(self, user_id: int) -> int:
        """查询用户当前生效套餐的并发生成上限（默认 2）。

        规则：取该用户最近一条 active 订阅 → 套餐 concurrency_limit；
        订阅过期/缺失按 free 档兜底（2）。

        Args:
            user_id: 用户 ID。

        Returns:
            并发上限（≥1）。
        """
        stmt = (
            select(Plan)
            .join(Subscription, Subscription.plan_id == Plan.id)
            .where(Subscription.user_id == user_id, Subscription.status == "active")
            .order_by(Subscription.started_at.desc())
            .limit(1)
        )
        plan = (await self.session.execute(stmt)).scalar_one_or_none()
        if plan is None:
            return 2
        return max(1, int(plan.concurrency_limit or 2))

    # ---- AI 调用日志 ----

    async def sum_call_cost(self, render_task_id: int) -> int:
        """汇总渲染任务的 AI 调用总成本（ai_call_logs.cost_credits 求和）。

        Args:
            render_task_id: 渲染任务 ID。

        Returns:
            累计成本（积分，无日志时为 0）。
        """
        stmt = (
            select(func.coalesce(func.sum(AiCallLog.cost_credits), 0))
            .where(AiCallLog.render_task_id == render_task_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())


def sanitize_idempotency_key(raw: str | None, max_len: int = 64) -> str | None:
    """清洗幂等键：去空白、限长（截断），空串返回 None。

    Args:
        raw: 原始幂等键（请求头 Idempotency-Key）。
        max_len: 最大长度（默认 64，保证拼接 shot_id 后仍在 128 以内）。

    Returns:
        清洗后的幂等键；无效输入返回 None。
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def batch_task_key(idem_key: str, shot_id: int) -> str:
    """构造单任务幂等键：``{批次幂等键}:{shot_id}``。

    Args:
        idem_key: 批次幂等键（已清洗）。
        shot_id: 镜头 ID。

    Returns:
        渲染任务级幂等键（render_tasks.idempotency_key 唯一约束用）。
    """
    return f"{idem_key}:{shot_id}"


def batch_freeze_key(idem_key: str, shot_id: int) -> str:
    """构造单镜头冻结幂等键（与任务键共用前缀，billing 幂等回放用）。

    Args:
        idem_key: 批次幂等键（已清洗）。
        shot_id: 镜头 ID。

    Returns:
        credit_transactions.idempotency_key 使用的键。
    """
    return batch_task_key(idem_key, shot_id)


def str_param(value: object | None) -> str | None:
    """从 JSONB 反序列化值中取字符串（None 安全）。

    Args:
        value: JSONB 字段中取出的任意值。

    Returns:
        非空字符串；其余返回 None。
    """
    if isinstance(value, str) and value:
        return value
    return None


def int_param(value: object | None) -> int | None:
    """从 JSONB 反序列化值中取整数（None 安全）。

    Args:
        value: JSONB 字段中取出的任意值。

    Returns:
        整数值；非法输入返回 None。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
