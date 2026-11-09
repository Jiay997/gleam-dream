"""渲染任务执行器模块（Task 8 · 单进程 asyncio，模拟 Celery 语义）。

:class:`RenderExecutor` 提供进程内任务队列与 worker 池：

- 队列：``asyncio.PriorityQueue``，元素 ``(priority, seq, task_id)``
  （priority 小者优先，seq 为入队序号保证同优先级 FIFO）；
- worker：默认 6 个常驻协程，逐个取任务执行
  :meth:`RenderExecutor._run_one`；
- 并发控制：per-user ``asyncio.Semaphore``（上限查用户订阅套餐
  concurrency_limit：free 2 / pro 5 / studio 10，默认 2）；
- 崩溃恢复：:meth:`start` 时把 status ∈ (pending, queued, running,
  retrying) 的任务行全部重置 queued 重新入队（任务行先持久化再执行的
  补充保障）；
- 幂等复用：执行前按 ``input_hash`` 查同镜头已成功任务，命中直接复用
  产物（不重复调用模型、不重复扣费——原冻结全额退款）。

任务执行流水线（每次状态变更：先事务落库，commit 后发 SSE 事件）：

1. ``queued → running``（shot: ``render_queued → rendering``）；
2. 调用 :func:`model_gateway.generate`（video / image_to_video 或
   text_to_video，prompt=final_prompt，params.duration_ms）；
3. 成功 → QC（伪向量人脸一致性 + 时长对齐规则）：QC failed 进入修正
   循环（≤3 轮，每轮新产物、消耗计入实际成本）；超限 →
   ``manual_required`` + 全额退款；
4. QC passed/warning → 登记 media_files / render_outputs / shot_versions
   （保留 3 版，超删最旧）/ qc_reports → shot ``rendered`` → 结算
   （actual = Σ ai_call_logs.cost_credits）；
5. 网关异常/失败结果 → ``failure_type=model_unavailable``，指数退避
   重试 ≤ max_retries（1s, 2s, ...）→ 超限 ``failed`` + 全额退款；
6. 批次内全部终结 → 发布 ``render.batch.completed``。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.core.db import async_session_factory
from app.core.ids import new_request_id
from app.infra.event_bus import event_bus
from app.infra.model_gateway import GenRequest, model_gateway
from app.infra.storage import storage
from app.models import (
    MediaFile,
    Project,
    RenderOutput,
    RenderTask,
    ScriptLine,
    Shot,
    ShotVersion,
)
from app.models.asset import Asset, AssetVersion
from app.repositories.render_repo import RenderRepository, int_param, str_param
from app.services import (
    anchor_visual_qc,
    billing_service,
    motion_service,
    reference_guard,
)
from app.services.render_service import (
    QC_MAX_ROUNDS,
    publish_batch_completed_if_done,
    publish_task_event,
)

logger = logging.getLogger("gleamdream.tasks.executor")


def _compose_anchor_prompt(shot: Any, params: dict[str, Any]) -> str:
    """从镜头剧本锚点构造渲染 prompt 的剧情约束片段（缺陷三）。

    优先级：``shot.script_anchor.script_text`` → ``params["script_anchor"]``
    → 镜头 action_desc。渲染层只使用已注册锚点组装，不再自由扩写；
    锚点缺失时以 action_desc 兜底并记 warning（不阻塞历史镜头）。

    Args:
        shot: 镜头 ORM 实例（可读取 script_anchor / action_desc）。
        params: 任务参数字典（可能携带 script_anchor 覆盖值）。

    Returns:
        剧情锚点文本（≤180 字）；完全缺失时返回空字符串。
    """
    anchor: dict[str, Any] | None = None
    if getattr(shot, "script_anchor", None) and isinstance(shot.script_anchor, dict):
        anchor = shot.script_anchor
    elif isinstance(params.get("script_anchor"), dict):
        anchor = params["script_anchor"]
    if anchor:
        text = str(anchor.get("script_text") or "").strip()
        if text:
            return text[:180]
    action_desc = str(getattr(shot, "action_desc", None) or "").strip()
    if action_desc:
        logger.warning("镜头缺剧本锚点，按 action_desc 兜底 shot_id=%s", getattr(shot, "id", "?"))
        return action_desc[:180]
    return ""

# worker 协程数量
WORKER_COUNT: int = 6
# 队列容量上限（满时 submit 抛 60006）
QUEUE_MAX_SIZE: int = 1000
# 网关异常重试的基础退避秒数（指数：1s, 2s, 4s...）
RETRY_BACKOFF_BASE_SECONDS: float = 1.0
# 每镜头保留的生成版本数上限（超删最旧）
SHOT_VERSION_KEEP: int = 3
# 用户并发信号量缓存上限（防长期运行内存增长）
SEMAPHORE_CACHE_MAX: int = 1000
# QC 时长对齐容差（毫秒）：产物时长与镜头时长偏差超过该值记入 detail
DURATION_ALIGN_TOLERANCE_MS: int = 500


def _utcnow() -> datetime:
    """获取当前 UTC 时间。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


class RenderExecutor:
    """渲染任务执行器：优先级队列 + worker 池 + per-user 并发闸门。

    Attributes:
        _queue: 任务优先级队列（元素 ``(priority, seq, task_id)``）。
        _workers: worker 协程任务列表。
        _seq: 入队序号计数器（同优先级 FIFO 保证）。
        _semaphores: user_id → 并发信号量缓存（按套餐上限）。
        _semaphores_lock: 信号量缓存构造锁（防并发重复建）。
        _running: 是否已启动（防重复 start）。
    """

    def __init__(self, worker_count: int = WORKER_COUNT, queue_max_size: int = QUEUE_MAX_SIZE) -> None:
        """构造执行器（不启动；调用 :meth:`start` 启动）。

        Args:
            worker_count: worker 协程数量。
            queue_max_size: 队列容量上限。
        """
        self._worker_count = worker_count
        self._queue: asyncio.Queue[tuple[int, int, int]] = asyncio.Queue(maxsize=queue_max_size)
        self._workers: list[asyncio.Task[None]] = []
        self._seq = 0
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._semaphores_lock = asyncio.Lock()
        self._running = False
        #: 延迟重试登记表（V4.0 AC-F5-08-2）：{job_id: (asyncio.Task, 元数据)}
        self._delayed_retries: dict[str, dict[str, object]] = {}
        #: 过期冻结周期清理任务（防止冻结仅依赖计费查询时惰性触发）
        self._expiry_task: asyncio.Task[None] | None = None

    # ---- 延迟批量重试（V4.0 AC-F5-08-2） ----

    def schedule_delayed_retry(
        self,
        *,
        project_id: int,
        task_ids: list[int],
        delay_minutes: int,
        user_id: int,
    ) -> str:
        """登记一次延迟批量重试（到时自动批量重试，倒计时可取消）。

        使用 ``asyncio`` 定时任务实现（单实例约束，见 03 文档 ADR-09）；
        重启后未到期的延迟重试不恢复（失败任务仍可手动重试，无数据损失）。

        Args:
            project_id: 项目 ID。
            task_ids: 待重试任务 ID 列表。
            delay_minutes: 延迟分钟数（>0）。
            user_id: 操作人用户 ID（审计）。

        Returns:
            延迟任务标识（用于取消）。
        """
        job_id = f"delay-{project_id}-{new_request_id().lower()[:10]}"

        async def _runner() -> None:
            """延迟到时后执行批量重试。"""
            try:
                await asyncio.sleep(max(0, delay_minutes) * 60)
            except asyncio.CancelledError:
                logger.info("延迟批量重试已取消 job_id=%s", job_id)
                raise
            try:
                from app.services import render_service

                async with async_session_factory() as session:
                    project = await session.get(Project, project_id)
                    if project is None:
                        return
                    result = await render_service.batch_retry(
                        session,
                        project,
                        task_ids=task_ids,
                        delay_minutes=0,
                        user_id=user_id,
                    )
                logger.info(
                    "延迟批量重试已执行 job_id=%s accepted=%s skipped=%s",
                    job_id, result.get("accepted"), result.get("skipped"),
                )
                await event_bus.publish(
                    project_id,
                    "render.batch.retried",
                    {"job_id": job_id, **{k: v for k, v in result.items()}},
                )
            except Exception:  # noqa: BLE001
                logger.error("延迟批量重试执行失败 job_id=%s", job_id, exc_info=True)
            finally:
                self._delayed_retries.pop(job_id, None)

        task = asyncio.create_task(_runner(), name=job_id)
        self._delayed_retries[job_id] = {
            "task": task,
            "project_id": project_id,
            "task_ids": list(task_ids),
            "delay_minutes": delay_minutes,
            "user_id": user_id,
        }
        logger.info(
            "已登记延迟批量重试 job_id=%s delay_minutes=%s tasks=%s",
            job_id, delay_minutes, len(task_ids),
        )
        return job_id

    def cancel_delayed_retry(self, job_id: str) -> bool:
        """取消一次已登记的延迟批量重试。

        Args:
            job_id: :meth:`schedule_delayed_retry` 返回的标识。

        Returns:
            取消成功返回 True；不存在返回 False。
        """
        entry = self._delayed_retries.pop(job_id, None)
        if entry is None:
            return False
        task = entry.get("task")
        if isinstance(task, asyncio.Task):
            task.cancel()
        return True

    def list_delayed_retries(self, project_id: int | None = None) -> list[dict[str, object]]:
        """列出进行中的延迟批量重试（倒计时展示用）。

        Args:
            project_id: 项目筛选（None 全部）。

        Returns:
            延迟任务元数据列表（不含内部 Task 对象）。
        """
        items: list[dict[str, object]] = []
        for job_id, entry in self._delayed_retries.items():
            if project_id is not None and int(entry.get("project_id") or 0) != project_id:
                continue
            items.append(
                {
                    "job_id": job_id,
                    "project_id": entry.get("project_id"),
                    "task_ids": entry.get("task_ids"),
                    "delay_minutes": entry.get("delay_minutes"),
                }
            )
        return items

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动执行器：崩溃恢复扫描 + 启动 worker 池。

        重复调用幂等（已启动直接返回）。

        Raises:
            无（恢复扫描中单个任务入队失败仅记日志，不影响启动）。
        """
        if self._running:
            return
        self._running = True
        await self._recover()
        for i in range(self._worker_count):
            worker = asyncio.create_task(self._worker_loop(i), name=f"render-worker-{i}")
            self._workers.append(worker)
        self._expiry_task = asyncio.create_task(self._expiry_loop(), name="freeze-expiry")
        logger.info("渲染执行器已启动 workers=%s queue=%s", self._worker_count, self._queue.maxsize)

    async def _expiry_loop(self) -> None:
        """周期释放过期冻结（每 5 分钟一次，失败不终止循环）。

        Returns:
            None。

        Raises:
            asyncio.CancelledError: 执行器停止时向上传播。
        """
        while True:
            try:
                await asyncio.sleep(300)
                async with async_session_factory() as session:
                    released = await billing_service.expire_stale_freezes(session)
                if released:
                    logger.info("周期清理过期冻结 released=%s", released)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 单次清理失败不影响后续周期
                logger.exception("周期清理过期冻结失败")

    async def stop(self) -> None:
        """停止执行器：取消 worker 并等待在跑任务收尾。

        取消语义：worker 协程在 await 点被取消，数据库事务由 SQLAlchemy
        会话上下文管理器回滚，不会留下半提交状态；已提交的事务（如
        running 状态）会在下次 :meth:`start` 恢复扫描中重新入队。

        Raises:
            无（worker 取消异常一律吞掉并记日志）。
        """
        if not self._running:
            return
        self._running = False
        tasks: list[asyncio.Task[Any]] = [*self._workers]
        if self._expiry_task is not None:
            tasks.append(self._expiry_task)
            self._expiry_task = None
        for worker in tasks:
            worker.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        logger.info("渲染执行器已停止")

    async def _recover(self) -> None:
        """崩溃恢复：把 queued/running/retrying/pending 任务全部重置 queued 入队。

        每个 worker 节点重启后执行一次；任务行先重置再入队，保证
        「任务行先持久化再执行」。

        Raises:
            无（数据库异常仅记日志，不阻塞启动）。
        """
        try:
            async with async_session_factory() as session:
                repo = RenderRepository(session)
                tasks = await repo.list_recoverable_tasks()
                for task in tasks:
                    if task.status != "queued":
                        task.status = "queued"
                        task.started_at = None
                await session.commit()
                for task in tasks:
                    await self.submit(int(task.id), int(task.priority), 0, skip_limit=True)
                if tasks:
                    logger.warning("崩溃恢复：重新入队 %s 个渲染任务", len(tasks))
        except Exception:  # noqa: BLE001 恢复失败不阻塞应用启动
            logger.exception("渲染任务恢复扫描失败")

    # ---- 入队 ----

    async def submit(self, task_id: int, priority: int, user_id: int, *, skip_limit: bool = False) -> None:
        """将渲染任务入队（队列满抛 60006）。

        Args:
            task_id: 渲染任务 ID（须已持久化）。
            priority: 优先级（1~9，小者优先）。
            user_id: 所属用户 ID（用于 per-user 并发闸门；恢复扫描时传 0，
                执行时再按项目 owner 补查）。
            skip_limit: True 跳过队列容量检查（恢复扫描内部使用）。

        Raises:
            BizError: 60006 并发任务超限（队列满）。
        """
        if not skip_limit and self._queue.qsize() >= self._queue.maxsize:
            raise BizError(
                ErrorCode.CONCURRENCY_LIMIT_EXCEEDED,
                data={"queue_size": self._queue.qsize(), "queue_max": self._queue.maxsize},
            )
        self._seq += 1
        try:
            self._queue.put_nowait((max(1, min(9, int(priority))), self._seq, int(task_id)))
        except asyncio.QueueFull:  # 并发竞争兜底
            raise BizError(
                ErrorCode.CONCURRENCY_LIMIT_EXCEEDED,
                data={"queue_size": self._queue.maxsize, "queue_max": self._queue.maxsize},
            ) from None
        logger.debug("任务入队 task_id=%s priority=%s seq=%s", task_id, priority, self._seq)

    async def _get_semaphore(self, user_id: int) -> asyncio.Semaphore:
        """获取（或构造）指定用户的并发信号量。

        上限按该用户最近一条 active 订阅的套餐 concurrency_limit 决定
        （free 2 / pro 5 / studio 10，默认 2）；信号量按 user_id 缓存。

        Args:
            user_id: 用户 ID。

        Returns:
            该用户的 asyncio.Semaphore。
        """
        async with self._semaphores_lock:
            semaphore = self._semaphores.get(user_id)
            if semaphore is not None:
                return semaphore
            async with async_session_factory() as session:
                limit = await RenderRepository(session).get_user_plan_concurrency(user_id)
            semaphore = asyncio.Semaphore(limit)
            # 缓存上限：用户规模过大时淘汰最旧的空闲信号量，防止内存无界增长
            if len(self._semaphores) >= SEMAPHORE_CACHE_MAX:
                for stale_id, stale in list(self._semaphores.items()):
                    if stale._value >= limit and stale_id != user_id:
                        self._semaphores.pop(stale_id, None)
                        break
                if len(self._semaphores) >= SEMAPHORE_CACHE_MAX:
                    self._semaphores.pop(next(iter(self._semaphores)), None)
            self._semaphores[user_id] = semaphore
            return semaphore

    # ---- worker ----

    async def _worker_loop(self, index: int) -> None:
        """worker 主循环：逐个取任务执行，异常不中断循环。

        Args:
            index: worker 序号（仅日志用）。

        Raises:
            无（任务执行异常全部捕获记日志；CancelledError 向上传播）。
        """
        while True:
            priority, seq, task_id = await self._queue.get()
            try:
                await self._run_one(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 单任务异常不影响 worker 存活
                logger.exception("渲染任务执行异常 worker=%s task_id=%s", index, task_id)
            finally:
                self._queue.task_done()

    # ---- 单任务执行 ----

    async def _run_one(self, task_id: int) -> None:
        """执行单个渲染任务（完整流水线，见模块 docstring）。

        Args:
            task_id: 渲染任务 ID。

        Raises:
            无（全部业务失败路径内部消化为任务终态；意外异常向上抛给
            worker 循环记录）。
        """
        async with async_session_factory() as session:
            repo = RenderRepository(session)
            task = await repo.get_task(task_id)
            if task is None:
                logger.warning("执行器取到不存在的任务 task_id=%s", task_id)
                return
            if task.status not in ("queued", "pending", "retrying"):
                logger.debug("跳过非待执行任务 task_id=%s status=%s", task_id, task.status)
                return

            project_id = int(task.project_id)
            shot_id = int(task.shot_id) if task.shot_id else None
            shot = await repo.get_shot(shot_id) if shot_id else None
            project_owner = int(task.input_params.get("owner_user_id") or 0) if task.input_params else 0
            if not project_owner:
                project_row = (
                    await session.execute(select(Project).where(Project.id == project_id))
                ).scalar_one_or_none()
                project_owner = int(project_row.owner_user_id) if project_row else 0

            # ---- 二次幂等校验：同镜头同入参已成功 → 复用产物 ----
            # 复用前校验历史产物是否为可播放媒体：历史 stub 视频是
            # application/json 占位，若继续复用，修复后的 MP4 播放能力会被旧
            # 缓存永久遮蔽。仅当 MIME 为 video/* 时才复用，否则正常重新生成。
            input_hash = str_param(task.input_hash)
            if shot is not None and input_hash:
                reusable = await repo.find_reusable_success(project_id, shot_id, input_hash)
                if reusable is not None and int(reusable.id) != int(task.id):
                    prior_outputs = await repo.list_outputs(int(reusable.id))
                    prior_media_id = (
                        int(prior_outputs[0].media_file_id)
                        if prior_outputs and prior_outputs[0].media_file_id else None
                    )
                    prior_media = (
                        (await session.execute(
                            select(MediaFile).where(MediaFile.id == prior_media_id)
                        )).scalar_one_or_none()
                        if prior_media_id else None
                    )
                    if prior_media is not None and str_param(prior_media.mime_type).startswith("video/"):
                        await self._reuse_prior_output(session, repo, task, shot, reusable)
                        return

            # ---- running + 并发闸门 ----
            semaphore = await self._get_semaphore(project_owner) if project_owner else None
            if semaphore is None:
                await self._execute(session, repo, task, shot)
                return
            async with semaphore:
                await self._execute(session, repo, task, shot)

    async def _execute(self, session: AsyncSession, repo: RenderRepository, task: RenderTask, shot: Shot | None) -> None:
        """任务主体：running → 生成 → QC 循环 → 落库结算 / 失败重试。

        Args:
            session: 数据库会话（执行器自建短会话）。
            repo: 渲染仓储。
            task: 渲染任务 ORM 实例（status 已校验为 queued/pending/retrying）。
            shot: 关联镜头（可空）。

        Raises:
            无（业务失败路径内部消化；意外异常向上传播）。
        """
        project_id = int(task.project_id)
        shot_id = int(task.shot_id) if task.shot_id else None
        params = dict(task.input_params or {})
        attempt_base = int(task.retry_count)

        # 1. running（先持久化再执行）
        task.status = "running"
        task.started_at = task.started_at or _utcnow()
        if shot is not None and shot.status in ("render_queued", "locked"):
            shot.status = "rendering"
        await session.commit()
        await publish_task_event(
            project_id, int(task.id), shot_id, "running", attempt=attempt_base + 1, progress=0,
        )

        # 2. 生成 + QC 修正循环
        # base_prompt 保持视觉描述（外貌/场景/风格）不变；每轮在其上重新拼装
        # 三要素（动作/运镜/时序），静态判定后按轮次强化，避免重复叠加。
        base_prompt = (
            str_param(params.get("base_prompt"))
            or str_param(params.get("final_prompt"))
            or ""
        )
        params["base_prompt"] = base_prompt
        duration_ms = int_param(params.get("duration_ms")) or int(shot.duration_ms if shot else 3000)
        # 生成端时长收紧到 3–5 秒（成片时间线会按镜头实际时长裁剪）。
        generation_ms = max(
            motion_service.SHOT_DURATION_MIN_MS,
            min(motion_service.SHOT_DURATION_MAX_MS, duration_ms),
        )
        # 首帧选择顺序（2026-11 修复：换场错配）：
        # 1. 本镜已合成的整幅要素首帧（含正确场景+角色+道具布局）；
        # 2. 同场次的上一镜尾帧（仅用于同场动作连贯，见 _resolve_continuity_image_key
        #    的场次判断）；
        # 3. 单张资产锚点图兜底。
        # 历史实现把「上一镜尾帧」放在第一位且不判断场次，换场镜头会被
        # 强行从上一场尾帧开始扩散，导致场景在镜头间渐变而不是硬切。
        image_key: str | None = None
        image_source: str | None = None
        if shot is not None and shot.first_frame_media_id:
            image_key = await self._resolve_shot_image_key(session, shot)
            image_source = "composed_first_frame"
        if image_key is None:
            image_key, image_source = await self._resolve_continuity_image_key(session, shot)
        if image_key is None:
            image_key = await self._resolve_shot_image_key(session, shot)
            image_source = "asset_anchor"
        image_uri = self._image_key_to_data_uri(image_key)
        # 参考帧预检（宫格准入闸门）：命中规则网格布局时自动提取左上单格
        # 替代整图，避免表情集/三视图拼版整图进入 I2V 造成多宫格画面。
        reference_precheck: list[str] = []
        if image_key:
            try:
                raw_path = storage.object_path(
                    image_key[len("/files/"):] if image_key.startswith("/files/") else image_key
                )
                safe_path, precheck_warnings = reference_guard.ensure_single_panel(raw_path)
                reference_precheck.extend(precheck_warnings)
                if safe_path != str(raw_path):
                    # 命中宫格并已提取单格：以单格路径重编 data URI，
                    # image_key 保留原图键供审计（meta 中记录 precheck 结果）
                    image_uri = motion_service.image_to_data_uri(safe_path)
                    reference_precheck.append("I2V 首帧已替换为提取的单格图")
            except Exception as exc:  # noqa: BLE001 预检失败不阻断渲染
                logger.warning("参考帧预检失败 shot_id=%s: %s", shot_id, exc)
                reference_precheck.append(f"precheck_error:{type(exc).__name__}")
        # P1 修复（竖屏构图）：把项目画幅传给视频网关，避免降级视频默认按
        # 横屏 1280x720 生成、合成时上下补黑边。项目画幅读取失败按 9:16。
        project = await session.get(Project, int(project_id))
        aspect_ratio = str(
            params.get("aspect_ratio")
            or (project.aspect_ratio if project is not None else "9:16")
        )
        gateway_params: dict[str, Any] = {
            "duration_ms": generation_ms,
            "aspect_ratio": aspect_ratio,
            # 运镜语义透传到视频网关：stub 按 dolly_in/tracking/handheld 等
            # 生成不同运动，避免所有镜头都是同一种慢推。
            "camera_move": str(getattr(shot, "camera_move", None) or "static"),
            # DashScope wan 系列按 720P 输出，竖屏由首帧比例决定
            "resolution": "720P",
            "fps": 30,
            "image_source": image_source,
            # 视频模型易在画面里幻觉中文字幕/店招文字/服装名牌，显式反向提示；
            # 镜头 7/9 实测出现过围裙上被写成「陈姨」「DeeFart」的穿帮。
            # 2026-10-29 追加宫格类负面词（缺陷一）：抑制 split screen /
            # grid / collage 等多面板画面（即便参考帧或 prompt 残留诱发因素）。
            "negative_prompt": (
                "文字，字幕，水印，logo，文字叠加，中文字幕，服装上的文字，"
                "名牌，姓名贴，店招文字，英文logo，低质量，画面畸形，"
                # 2026-11 QA 追加（文字乱码）：验收实测转账记录、合同条款、
                # 门外招牌出现乱码文字；小字号文本一律反向抑制。
                "乱码文字，乱码，错别字招牌，伪造文字，无法辨认的文字，"
                "屏幕上的文字，合同上的文字，纸张上的印刷文字，"
                # 2026-11 QA 修复（镜头内/跨镜风格漂移）：显式抑制画风切换、
                # 插画/二次元/3D 与实拍混合，避免 I2V 重绘后风格突变。
                "风格突变，画风切换，多种画风混搭，插画风格，二次元风格，"
                "动漫脸，3D渲染感，写实与卡通混合，"
                # 2026-11 修复（景别跳变）：I2V 常在 4 秒内推成特写、跟着人物
                # 换背景（shot 6/7 实测 wide→close_up），显式反向抑制变景别、
                # 换地点与镜头内转场。
                "景别跳变，突然拉近，突然推成特写，镜头内切换场景，"
                "背景突变，场景变换，镜头切换，转场，多镜头拼接，"
                "人物尺寸突变，人物突然变大，"
                # 2026-11 QA 修复（镜内场景/时间突变）：验收实测首帧昏黄傍晚
                # → 尾帧白天明亮、凭空多出路人顾客；显式锁死时段与背景人群。
                "时段变化，白天变夜晚，夜晚变白天，光线突然变亮，光线突变，"
                "晴天变阴天，室内变室外，背景人群增加，路人走动，围观人群，"
                "空场变满场，场景扩建，家具位置改变，陈设变化，"
                + reference_guard.GRID_NEGATIVE_PROMPT
            ),
            "reference_precheck": reference_precheck,
        }
        if image_key:
            gateway_params["image_key"] = image_key
        if image_uri:
            gateway_params["img_url"] = image_uri
        qc_round = max(0, int_param(params.get("qc_round")) or 0)
        gateway_error: str | None = None

        while True:
            # 每轮重新构造 prompt：首轮为三要素基线，重试轮逐级强化动作幅度
            # 2026-10-29 缺陷三修复：生成 prompt 由剧本锚点自动组装——把镜头
            # script_anchor.script_text（对应剧本原文）并入基础 prompt，禁止
            # 渲染层自由扩写剧本外内容；锚点缺失时按 dialogue_refs/action_desc
            # 确定性兜底（不阻塞历史镜头渲染），并记 warning。
            anchor_prompt = _compose_anchor_prompt(shot, params)
            prompt = motion_service.build_motion_prompt(
                shot, base_prompt, round_no=qc_round,
            )
            if anchor_prompt:
                prompt = f"{prompt}（剧情锚点：{anchor_prompt}）"
            params["effective_prompt"] = prompt
            params["motion_round"] = qc_round
            gateway_params["prompt_round"] = qc_round
            try:
                result = await model_gateway.generate(
                    "video",
                    task.task_type,
                    GenRequest(
                        capability="video",
                        prompt=prompt,
                        params=gateway_params,
                    ),
                    project_id=project_id,
                    task_id=int(task.id),
                )
            except Exception as exc:  # noqa: BLE001 网关异常（路由失败/网络等）统一按模型不可用重试
                gateway_error = f"{type(exc).__name__}: {exc}"
                break
            if not result.ok:
                gateway_error = result.error or "模型生成失败"
                break

            # QC：伪向量人脸一致性 + 画面运动验收 + 时长覆盖对齐
            score, qc_result, qc_detail = await self._qc_check(
                repo, shot, int(task.id), prompt, result, qc_round + 1,
            )
            qc_detail["image_source"] = image_source
            qc_detail["image_key"] = image_key
            qc_report_id = await self._persist_qc_report(
                session, repo, task, shot, score, qc_result, qc_detail, qc_round + 1,
            )
            if qc_result in ("passed", "warning"):
                # 成功前回写 params：effective_prompt / motion_round 须随任务落库，
                # 否则 _finish_success 读到旧参数，版本快照与媒体 meta 丢失实际提示词。
                task.input_params = params
                await self._finish_success(
                    session, repo, task, shot, result, qc_report_id, qc_round + 1,
                    qc_detail,
                )
                return
            # QC failed：修正循环（静态镜头 → 强化 prompt 后重新生成）
            history = list(params.get("motion_retry_history") or [])
            history.append(
                {
                    "round": qc_round + 1,
                    "prompt": prompt,
                    "verdict": qc_result,
                    "motion": qc_detail.get("motion"),
                    "reasons": qc_detail.get("motion_reasons"),
                }
            )
            params["motion_retry_history"] = history
            qc_round += 1
            params["qc_round"] = qc_round
            task.input_params = params
            if qc_round >= QC_MAX_ROUNDS:
                await self._finish_manual_required(session, repo, task, shot, qc_detail)
                return
            task.retry_count = int(task.retry_count) + 1  # QC 每轮失败计数（CHECK 上限 max_retries+3）
            task.input_params = params
            await session.commit()
            await publish_task_event(
                project_id, int(task.id), shot_id, "running",
                attempt=attempt_base + qc_round + 1, progress=qc_round * 30,
            )

        # 3. 网关异常路径：指数退避重试 ≤ max_retries，超限 failed + 退款
        if int(task.retry_count) < int(task.max_retries):
            task.retry_count = int(task.retry_count) + 1
            task.status = "retrying"
            task.failure_type = "model_unavailable"
            task.failure_reason = gateway_error
            task.input_params = params
            await session.commit()
            backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (int(task.retry_count) - 1))
            await publish_task_event(
                project_id, int(task.id), shot_id, "retrying",
                attempt=int(task.retry_count),
            )
            await asyncio.sleep(backoff)
            # 重新入队（priority 不变；直接投递队尾保证退避语义）
            await self.submit(int(task.id), int(task.priority), 0, skip_limit=True)
            return

        await self._finish_failed(session, repo, task, shot, gateway_error or "模型生成失败")


    # ---- QC ----

    async def _asset_image_key(self, session: AsyncSession, asset_id: int) -> str | None:
        """读取资产参考图的图片对象键（参考帧准入过滤后）。

        缺陷修复（2026-10-29 多宫格画面）：历史实现直接取
        ``asset.current_version_id``，当最新生成版本为表情集（九宫格拼图）
        时，宫格图会被当作视频参考帧 / 合成图层进入视频链路。

        缺陷修复（2026-11-03 背面视图当锚点）：``is_current`` 仅指向最新
        审批版本，补生成三视图后背面视图会顶掉正脸定妆照，视频参考帧变成
        背影（实测首帧无脸、成片形象漂移）。现查询该资产**全部带图版本**，
        经 :func:`reference_guard.pick_safe_reference` 按"portrait →
        turnaround_front → side → back"优先级选择，排除宫格类版本。

        Args:
            session: 数据库会话。
            asset_id: 资产 ID。

        Returns:
            图片对象键；资产/版本/媒体缺失、非图片或全部版本均为宫格类
            素材时返回 None。

        Raises:
            无：查询异常由调用方统一兜底。
        """
        asset = await session.get(Asset, int(asset_id))
        if asset is None:
            return None
        versions = (
            (
                await session.execute(
                    select(AssetVersion)
                    .where(AssetVersion.asset_id == int(asset_id))
                    .order_by(AssetVersion.version_no.asc())
                )
            )
            .scalars()
            .all()
        )
        if not versions:
            return None
        selected, warnings = reference_guard.pick_safe_reference(
            list(versions), asset_type=str(getattr(asset, "asset_type", "") or "character")
        )
        for message in warnings:
            logger.warning("镜头参考图准入告警 asset_id=%s: %s", asset_id, message)
        if selected is None or selected.media_file_id is None:
            return None
        media = await session.get(MediaFile, int(selected.media_file_id))
        if media is None or not media.object_key:
            return None
        mime = str(media.mime_type or "").lower()
        key = str(media.object_key).lower()
        if mime and not mime.startswith("image/") and not key.endswith(
            (".png", ".jpg", ".jpeg", ".webp")
        ):
            return None
        return str(media.object_key)

    async def _resolve_shot_image_key(
        self, session: AsyncSession, shot: Shot | None,
    ) -> str | None:
        """解析镜头视频的视觉参考图（本地存储 key）。

        P1 修复（画面穿帮）：历史实现按「角色 ID 列表 → 场景」的固定顺序
        取图，导致无对白的空镜/场景镜也套用角色定妆照，场景资产完全未参与，
        十个镜头只出现两张白墙棚拍图，与分镜描述严重不符。现按镜头语义选图：

        1. 首帧媒体（已人工指定时优先）；
        2. 画面描述/对白命中道具名 → 道具图（特写道具镜头）；
        3. 单角色对白 → 该角色图（近景/特写人像）；
        4. 多角色同框或无对白 → 场景图（避免只出现一个人或人物穿帮）；
        5. 逐级回退：场景 → 角色 → 道具 → 锚点版本。

        Args:
            session: 当前渲染任务的数据库会话。
            shot: 镜头实例（可空）。

        Returns:
            存储对象 key（可直接用于 /files 访问），解析失败返回 None。

        Raises:
            无：内部异常会记录日志并返回 None，避免阻断渲染主流程。
        """
        if shot is None:
            return None
        try:
            if shot.first_frame_media_id:
                media = await session.get(MediaFile, int(shot.first_frame_media_id))
                if media is not None and media.object_key:
                    return str(media.object_key)

            char_ids = [
                int(a) for a in (shot.character_asset_ids or []) if str(a).isdigit()
            ]
            prop_ids = [
                int(a) for a in (shot.prop_asset_ids or []) if str(a).isdigit()
            ]
            scene_id = int(shot.scene_asset_id) if shot.scene_asset_id else None

            # 角色名 → 资产 ID（用于按说话人精确取图）
            char_key_by_name: dict[str, str] = {}
            char_keys: list[str] = []
            for asset_id in char_ids:
                asset = await session.get(Asset, int(asset_id))
                key = await self._asset_image_key(session, int(asset_id))
                if key is None:
                    continue
                char_keys.append(key)
                if asset is not None and asset.name:
                    char_key_by_name[str(asset.name).strip()] = key

            scene_key = await self._asset_image_key(session, scene_id) if scene_id else None

            # 道具名命中（动作/对白/最终 prompt 中出现道具名）
            text_blob = " ".join(
                str(x or "")
                for x in (shot.action_desc, shot.dialogue, shot.final_prompt)
            )
            prop_ids = list(prop_ids)
            if not prop_ids:
                # P1 修复：历史分镜未绑定道具，这里按项目道具名兜底匹配，
                # 避免手写纸条/关东煮等特写镜头永远取不到道具图。
                project_props = (
                    await session.execute(
                        select(Asset).where(
                            Asset.project_id == int(shot.project_id),
                            Asset.asset_type == "prop",
                        )
                    )
                ).scalars().all()
                prop_ids = [int(asset.id) for asset in project_props]
            prop_keys: list[str] = []
            prop_hit: str | None = None
            for prop_id in prop_ids:
                asset = await session.get(Asset, int(prop_id))
                key = await self._asset_image_key(session, int(prop_id))
                if key is None:
                    continue
                prop_keys.append(key)
                name = str((asset.name if asset else "") or "").strip()
                # 支持「手写纸条 → 纸条」这类核心词命中，避免名称过长漏匹配
                if (
                    len(name) >= 2
                    and (name in text_blob or (len(name) >= 4 and name[-2:] in text_blob))
                    and prop_hit is None
                ):
                    prop_hit = key

            # 解析说话人：分镜 dialogue 只存台词正文（无「林晚：」前缀），
            # 因此先解析显式前缀，再按剧本行内容反查 speaker_name。
            speakers: list[str] = []
            line_types: list[str] = []
            for raw_line in str(shot.dialogue or "").splitlines():
                if "：" in raw_line:
                    name = raw_line.split("：", 1)[0].strip()
                elif ":" in raw_line:
                    name = raw_line.split(":", 1)[0].strip()
                else:
                    continue
                if name and name not in speakers:
                    speakers.append(name)
            if not speakers and str(shot.dialogue or "").strip():
                dialogue_text = str(shot.dialogue or "")
                rows = (
                    await session.execute(
                        select(ScriptLine)
                        .where(
                            ScriptLine.project_id == int(shot.project_id),
                            ScriptLine.is_deleted.is_(False),
                            ScriptLine.line_type.in_(("dialogue", "narration")),
                        )
                        .order_by(ScriptLine.id.asc())
                    )
                ).scalars().all()
                for line in rows:
                    content = str(line.content or "").strip()
                    if not content or content not in dialogue_text:
                        continue
                    line_types.append(str(line.line_type))
                    name = str(line.speaker_name or "").strip()
                    if name and name not in speakers:
                        speakers.append(name)

            # 仅当镜头明确是「特写道具」时才用道具图，避免关东煮锅等
            # 场景常见道具把对白镜头抢成静物特写。
            prop_subject = prop_hit is not None and "特写" in str(shot.action_desc or "")
            if prop_subject:
                return prop_hit
            if len(speakers) == 1:
                speaker_key = char_key_by_name.get(speakers[0])
                if speaker_key is not None:
                    return speaker_key
            # 旁白（全部为 narration）或多角色同框 → 场景更安全
            if scene_key is not None:
                return scene_key

            # 兜底：任意角色 → 任意道具 → 锚点版本
            if char_keys:
                return char_keys[0]
            if prop_keys:
                return prop_keys[0]
            for raw in shot.asset_anchor_ids or []:
                if not str(raw).isdigit():
                    continue
                version = await session.get(AssetVersion, int(raw))
                if version is None or version.media_file_id is None:
                    continue
                media = await session.get(MediaFile, int(version.media_file_id))
                if media is not None and media.object_key:
                    return str(media.object_key)
        except (ValueError, TypeError) as exc:
            logger.warning("镜头参考图解析失败 shot_id=%s: %s", getattr(shot, "id", None), exc)
        return None

    async def _resolve_continuity_image_key(
        self, session: AsyncSession, shot: Shot | None,
    ) -> tuple[str | None, str | None]:
        """解析「上一镜尾帧」作为本镜 I2V 首帧，保证镜头间动作连贯。

        规则：**同分镜板且同场次**内 order_index 紧邻的前一镜，若已有成功
        版本且媒体为真实视频，抽取最后一帧落 ``continuity/`` 缓存（同版本
        幂等复用）。首镜、换场（场景资产不一致）、前一镜失败或产物为占位
        JSON 时返回 ``(None, None)``，由调用方回退到本镜合成首帧/资产锚点图。

        2026-11 修复：历史实现只比较 storyboard_id + order_index，换场镜头
        仍从上一场尾帧扩散，观众看到的是场景渐变而非硬切，跨场剧情错乱。

        Args:
            session: 当前渲染任务的数据库会话。
            shot: 镜头实例（可空）。

        Returns:
            ``(尾帧对象键, "prev_tail")`` 或 ``(None, None)``。
        """
        if shot is None or shot.storyboard_id is None or shot.order_index is None:
            return None, None
        try:
            prev_shot = (
                await session.execute(
                    select(Shot)
                    .where(
                        Shot.storyboard_id == int(shot.storyboard_id),
                        Shot.order_index < int(shot.order_index),
                    )
                    .order_by(Shot.order_index.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if prev_shot is None:
                return None, None
            # 换场禁止尾帧续接：场景不一致时继续扩散会把上一场的空间、
            # 光线、道具带进本场，表现为"场景在镜头间突变/渐变"。
            # 场景资产与场次 ID 任一不一致都视为换场（历史数据可能两列皆空，
            # 此时按同场处理，保持旧行为）。
            if (
                getattr(prev_shot, "scene_asset_id", None)
                != getattr(shot, "scene_asset_id", None)
                and (prev_shot.scene_asset_id or shot.scene_asset_id)
            ):
                return None, None
            if (
                getattr(prev_shot, "scene_id", None) != getattr(shot, "scene_id", None)
                and (prev_shot.scene_id or shot.scene_id)
            ):
                return None, None
            versions = await RenderRepository(session).list_shot_versions(int(prev_shot.id))
            if not versions:
                return None, None
            current = next((v for v in reversed(versions) if v.is_current), versions[-1])
            if current.media_file_id is None:
                return None, None
            media = await session.get(MediaFile, int(current.media_file_id))
            if media is None or not media.object_key:
                return None, None
            if not str_param(media.mime_type).startswith("video/"):
                return None, None
            out_key = f"continuity/shot_{int(prev_shot.id)}_v{int(current.version_no)}.jpg"
            out_path = storage.object_path(out_key)
            if not out_path.exists():
                source_path = storage.object_path(str(media.object_key))
                if not motion_service.extract_last_frame(source_path, out_path):
                    return None, None
            return out_key, "prev_tail"
        except Exception as exc:  # noqa: BLE001 尾帧续接失败不阻断渲染
            logger.warning(
                "上一镜尾帧解析失败 shot_id=%s: %s", getattr(shot, "id", None), exc,
            )
            return None, None

    @staticmethod
    def _image_key_to_data_uri(image_key: str | None) -> str | None:
        """把存储对象键转换为 I2V 网关可用的 JPEG data URI。

        Args:
            image_key: 存储对象键（可带 ``/files/`` 前缀）。

        Returns:
            data URI；键为空、文件缺失或不可解码时返回 None。
        """
        if not image_key:
            return None
        key = image_key[len("/files/"):] if image_key.startswith("/files/") else image_key
        try:
            return motion_service.image_to_data_uri(storage.object_path(key))
        except Exception as exc:  # noqa: BLE001 图片编码失败回退纯文本生成
            logger.warning("首帧编码 data URI 失败 key=%s: %s", key, exc)
            return None

    async def _qc_check(
        self,
        repo: RenderRepository,
        shot: Shot | None,
        task_id: int,
        prompt: str,
        result: Any,
        round_no: int,
    ) -> tuple[float | None, str, dict[str, Any]]:
        """执行渲染产物 QC：真实帧伪影 + 画面运动 + 时长覆盖 + 锚点软校验。

        规则：
        - 人脸：当前无真实视觉/人脸模型，显式标记 ``face_check=unverified``，
          不再用伪向量伪造 passed；
        - 伪影（Q-011）：对运动存证帧做黑块/白底贴图检测，命中 → failed，
          触发强化 prompt 重绘；
        - 运动（P0 动态性）：对真实视频抽帧计算灰度帧差，平均差异、首中尾
          关键帧差异或最长静止窗口任一不达标 → 直接 failed，触发强化
          prompt 重绘；占位/降级产物跳过（避免 stub 回退陷入无限重试）；
        - 时长：成片时间线会按镜头时长裁剪，产物只要「不短于镜头时长减
          容差」即视为覆盖合格；超出部分由合成层裁剪，不算缺陷。

        Args:
            repo: 渲染仓储。
            shot: 镜头实例（可空）。
            task_id: 渲染任务 ID（候选向量种子的一部分）。
            prompt: 本轮实际使用的提示词（记入 detail 供排查）。
            result: 网关生成结果（GenResult）。
            round_no: 本轮 QC 轮次（1 起）。

        Returns:
            ``(score, verdict, detail)`` 三元组。
        """
        detail: dict[str, Any] = {"round_no": round_no}
        detail["prompt"] = str(prompt or "")[:800]
        score: float | None = None
        verdict = "passed"

        # Q-011：此前用伪向量候选与同一 portrait 比对，分数恒落在
        # 0.70~0.95（必然 passed），无法发现换脸/换装。当前环境没有真实
        # 人脸/视觉模型，因此不再伪造一致性分数，显式标记 unverified，
        # 由下方真实帧伪影检测与人工复核兜底。
        if shot is not None:
            detail["face_check"] = "unverified"
            detail["consistency_score"] = None
            detail["anchor_ids"] = [
                int(a) for a in (shot.asset_anchor_ids or []) if str(a).isdigit()
            ]

        # 画面运动验收（P0 动态性）：真实视频才做抽帧检测，占位/降级跳过
        simulated = bool(result.meta.get("simulated")) or bool(result.meta.get("degraded"))
        detail["simulated"] = simulated
        mime_type = str_param(result.meta.get("mime_type"))
        detail["motion"] = None
        if simulated:
            detail["motion_check"] = "skipped_simulated"
        elif mime_type.startswith("video/") and result.object_key:
            try:
                produced_path = storage.object_path(str(result.object_key))
            except Exception:  # noqa: BLE001 非法对象键按不可检测处理
                produced_path = None
            if produced_path is not None and produced_path.exists():
                # 三帧存证：把首/中/尾帧按原分辨率落盘 backend/.run/review，
                # 供逐镜头动态性验收留痕与人工复核。
                motion = motion_service.analyze_video_motion(
                    produced_path,
                    evidence_prefix=(
                        f"shot{int(shot.id):04d}_task{int(task_id)}_r{round_no}"
                        if shot is not None
                        else f"task{int(task_id)}_r{round_no}"
                    ),
                )
                detail["motion"] = motion
                if motion.get("passed"):
                    detail["motion_check"] = "passed"
                else:
                    detail["motion_check"] = "failed"
                    detail["motion_reasons"] = motion.get("reasons") or []
                    verdict = "failed"
            else:
                detail["motion_check"] = "unavailable"
        else:
            detail["motion_check"] = "not_video"

        # Q-011：真实帧伪影检测（黑块遮脸/白底贴图）。命中即判定 failed，
        # 走现有“强化提示词重绘”循环；不伪造人脸相似度。
        evidence_frames = list((detail.get("motion") or {}).get("evidence_frames") or [])
        if evidence_frames and not simulated:
            try:
                from PIL import Image

                from app.services.frame_image_ops import detect_panel_artifact

                artifacts: list[dict[str, Any]] = []
                for frame_path in evidence_frames[:2]:
                    with Image.open(frame_path) as frame_image:
                        issues = detect_panel_artifact(
                            frame_image, ignore_transparent=False
                        )
                    if issues:
                        artifacts.append({"frame": str(frame_path), "issues": issues})
                detail["frame_artifacts"] = artifacts
                if artifacts:
                    verdict = "failed"
                    detail.setdefault("motion_reasons", []).append("frame_artifact")
            except Exception:  # noqa: BLE001 - 检测失败不阻断 QC 主流程
                detail["frame_artifacts"] = []

        # 时长覆盖规则：产物 >= 镜头时长（容差内）即可，超出部分由合成层裁剪
        produced_ms = int_param(result.meta.get("duration_ms")) if result.meta else None
        if shot is not None and produced_ms is not None:
            shot_ms = int(shot.duration_ms)
            detail["duration_ms"] = produced_ms
            detail["duration_drift_ms"] = produced_ms - shot_ms
            if produced_ms + DURATION_ALIGN_TOLERANCE_MS < shot_ms:
                detail["duration_aligned"] = False
                verdict = "warning" if verdict == "passed" else "failed"
            else:
                detail["duration_aligned"] = True

        # ---- 剧本锚点 ↔ 画面软校验（2026-11 新增）----
        # 目标：把「剧本写了动作/道具，但提示词或画面里没有」这类信息丢失
        # 变成可观测的 QC 结论。规则校验 0 成本、必跑；视觉比对默认关闭
        # （当前 LLM 路由为纯文本模型），开启后失败也不阻断。
        # 结果只降级为 warning（照常落成片，不自动重生成，避免成本失控）。
        if shot is not None:
            anchor_check = anchor_visual_qc.check_anchor_coverage(shot)
            detail["anchor_check"] = anchor_check
            if not anchor_check.get("passed", True):
                detail["anchor_mismatch"] = True
                if verdict == "passed":
                    verdict = "warning"
            motion_detail = detail.get("motion") if isinstance(detail.get("motion"), dict) else {}
            vision = await anchor_visual_qc.check_frames_with_vision(
                shot,
                list(motion_detail.get("evidence_frames") or []),
                project_id=int(shot.project_id),
            )
            if vision:
                detail["anchor_vision"] = vision
                if not vision.get("passed", True):
                    detail["anchor_mismatch"] = True
                    if verdict == "passed":
                        verdict = "warning"
        return score, verdict, detail

    async def _persist_qc_report(
        self,
        session: AsyncSession,
        repo: RenderRepository,
        task: RenderTask,
        shot: Shot | None,
        score: float | None,
        verdict: str,
        detail: dict[str, Any],
        round_no: int,
    ) -> int | None:
        """落库一条 shot 维度的质检报告（qc_reports.target_type="shot"）。

        Args:
            session: 数据库会话。
            repo: 渲染仓储。
            task: 渲染任务实例。
            shot: 镜头实例（可空；无镜头不落报告）。
            score: 一致性分（可空）。
            verdict: QC 结论（passed/warning/failed）。
            detail: 报告详情。
            round_no: 轮次。

        Returns:
            新落的 QcReport ID；无镜头时返回 None。

        Raises:
            无（依赖调用方会话上下文管理提交）。
        """
        from app.models import QcReport

        if shot is None:
            return None
        report = QcReport(
            project_id=int(task.project_id),
            target_type="shot",
            target_id=int(shot.id),
            target_version_id=None,
            result=verdict,
            consistency_score=score,
            quality_defects=None,
            detail=detail,
            round_no=round_no,
            model_key="qc-motion-v1",
        )
        session.add(report)
        await session.flush()
        return int(report.id)

    # ---- 成功落库 ----

    async def _finish_success(
        self,
        session: AsyncSession,
        repo: RenderRepository,
        task: RenderTask,
        shot: Shot | None,
        result: Any,
        qc_report_id: int | None,
        qc_round: int,
        qc_detail: dict[str, Any] | None = None,
    ) -> None:
        """成功收尾：登记媒体/产物/版本/QC → shot rendered → 结算冻结。

        Args:
            session: 数据库会话。
            repo: 渲染仓储。
            task: 渲染任务实例。
            shot: 镜头实例（可空）。
            result: 最终通过的网关生成结果。
            qc_report_id: 通过轮次的质检报告 ID（可空）。
            qc_round: QC 轮次。
            qc_detail: 通过轮次的质检详情（运动指标/首帧来源，写入媒体 meta）。

        Raises:
            无（结算失败等意外异常向上传播，由 worker 记录）。
        """
        project_id = int(task.project_id)
        shot_id = int(task.shot_id) if task.shot_id else None
        params = dict(task.input_params or {})

        # 1. 媒体文件登记（视频元数据 JSON + 海报）
        media = MediaFile(
            owner_user_id=None,
            project_id=project_id,
            storage_provider="cos",
            bucket="gleamdream-local",
            object_key=str(result.object_key),
            url=result.media_url,
            media_type="video",
            mime_type=str_param(result.meta.get("mime_type")),
            # 写入真实文件体积（stub 生成的 MP4 会在 meta 中回传字节数）
            file_size_bytes=int_param(result.meta.get("file_size_bytes")) or 0,
            width=int_param(result.meta.get("width")),
            height=int_param(result.meta.get("height")),
            duration_ms=int_param(result.meta.get("duration_ms")),
            checksum_sha256=None,
            meta={
                "poster_key": str_param(result.meta.get("poster_key")),
                "simulated": bool(result.meta.get("simulated")),
                "placeholder": str_param(result.meta.get("placeholder")),
                "render_task_id": int(task.id),
                "provider": str_param(result.meta.get("provider")),
                "model_key": str_param(result.meta.get("model_key")),
                "qc_round": qc_round,
                "motion": (qc_detail or {}).get("motion"),
                "image_source": (qc_detail or {}).get("image_source"),
                "effective_prompt": params.get("effective_prompt"),
            },
        )
        session.add(media)
        await session.flush()

        # 2. 产物登记（关联本轮 QC 报告）
        output = RenderOutput(
            render_task_id=int(task.id),
            project_id=project_id,
            shot_id=shot_id,
            media_file_id=int(media.id),
            output_index=0,
            output_type="video",
            qc_report_id=qc_report_id,
            is_selected=True,
        )
        session.add(output)
        await session.flush()

        # 3. 镜头版本（保留 3 版，超删最旧；幂等复用路径可能无 shot）
        if shot is not None:
            versions = await repo.list_shot_versions(int(shot.id))
            next_no = (int(versions[-1].version_no) + 1) if versions else 1
            version = ShotVersion(
                shot_id=int(shot.id),
                version_no=next_no,
                media_file_id=int(media.id),
                render_task_id=int(task.id),
                qc_report_id=qc_report_id,
                prompt_snapshot=str_param(
                    params.get("effective_prompt") or params.get("final_prompt")
                ),
                seed=None,
                is_current=True,
                is_locked=False,
            )
            session.add(version)
            for old in versions:
                old.is_current = False
            await session.flush()
            # 保留最近 SHOT_VERSION_KEEP 版：versions 为升序旧列表，删除最旧的前缀
            remove_count = max(0, len(versions) + 1 - SHOT_VERSION_KEEP)
            for old in versions[:remove_count]:
                await repo.delete_shot_version(int(old.id))

        # 4. 任务 + 镜头终态（主数据；结算解耦到主数据提交之后）
        actual_cost = await repo.sum_call_cost(int(task.id))
        # ---- P0-3 修复：降级占位产物不得按真实模型计费 ----
        # 现象：Agnes 503 / 百炼 401 后网关熔断回退 stub，产物是占位视频，
        # 但 ai_call_logs 仍按路由单价记账，用户被正常扣费。
        # 口径：网关结果 meta.simulated=True（或显式 degraded）即视为降级，
        # 本次任务实际成本归零（结算走退款），并在 input_params 留痕供前端提示。
        degraded = bool(result.meta.get("simulated")) or bool(result.meta.get("degraded"))
        if degraded:
            logger.warning(
                "渲染产物降级（占位媒体），不计费 task_id=%s provider=%s",
                task.id, result.meta.get("provider"),
            )
            actual_cost = 0
            params["degraded"] = True
            params["degraded_reason"] = "媒体提供商不可用，已回退占位产物"
            params["degraded_provider"] = str_param(result.meta.get("provider"))
            task.input_params = params
        # 恢复重跑场景：历史轮次调用成本可能累计超过本任务冻结额，
        # 结算前封顶到冻结额，避免 settle 参数校验失败导致任务卡死
        frozen_credits = int(task.frozen_credits or 0)
        if frozen_credits > 0 and actual_cost > frozen_credits:
            logger.warning(
                "实际成本超冻结额，按冻结额封顶结算 task_id=%s actual=%s frozen=%s",
                task.id, actual_cost, frozen_credits,
            )
            actual_cost = frozen_credits
        task.status = "succeeded"
        task.actual_cost_credits = actual_cost
        task.failure_type = None
        task.failure_reason = None
        task.finished_at = _utcnow()
        if shot is not None:
            shot.status = "rendered"
        task_id_val = int(task.id)
        retry_count_val = int(task.retry_count)

        await session.commit()

        # 5. 结算（独立短会话；P0-3：降级占位产物 actual_cost=0，走全额退款；
        #    正常任务 actual_cost>0 结算。失败仅记日志，不影响任务终态）
        freeze_id = int_param(params.get("freeze_id"))
        if freeze_id is not None:
            try:
                async with async_session_factory() as pay_session:
                    if actual_cost > 0:
                        await billing_service.settle(
                            pay_session, freeze_id=freeze_id, actual_credits=actual_cost,
                        )
                    else:
                        await billing_service.refund(
                            pay_session, freeze_id=freeze_id, reason="零成本任务退款",
                        )
            except BizError as exc:
                logger.warning(
                    "结算/退款失败（冻结可能已过期）task_id=%s freeze_id=%s: %s",
                    task_id_val, freeze_id, exc.message,
                )
                # 结算失败不再静默：落站内通知（落库失败仅记日志），供人工对账补扣
                try:
                    async with async_session_factory() as alert_session:
                        project_row = await alert_session.get(Project, project_id)
                        owner_id = int(project_row.owner_user_id) if project_row else 0
                    if owner_id:
                        await event_bus.publish(
                            project_id,
                            "notification.system",
                            {
                                "user_id": owner_id,
                                "title": "计费结算异常",
                                "content": (
                                    f"任务 {task_id_val} 已产出，但积分结算失败：{exc.message}"
                                    "（请到计费中心核对并人工补扣/退款）"
                                ),
                                "payload": {"task_id": task_id_val, "freeze_id": freeze_id},
                            },
                        )
                except Exception:  # noqa: BLE001 告警失败不得覆盖任务终态
                    logger.exception("结算异常告警发布失败 task_id=%s", task_id_val)

        # 6. 事件（DB 提交后）
        await publish_task_event(
            project_id, task_id_val, shot_id, "succeeded",
            attempt=retry_count_val + 1, progress=100,
        )
        batch_key = str_param(params.get("batch_key"))
        if batch_key:
            await publish_batch_completed_if_done(session, project_id, batch_key)

    async def _reuse_prior_output(
        self,
        session: AsyncSession,
        repo: RenderRepository,
        task: RenderTask,
        shot: Shot,
        prior: RenderTask,
    ) -> None:
        """幂等复用：同 input_hash 已成功 → 复用产物并全额退款。

        复用规则：render_output 指向原任务媒体文件；镜头当前版本已由
        原任务建立则不重复建版本；actual_cost=0、不结算、原冻结全额
        退还（无新调用无新消耗）。

        Args:
            session: 数据库会话。
            repo: 渲染仓储。
            task: 当前任务实例。
            shot: 镜头实例。
            prior: 已成功的同入参任务。

        Raises:
            无。
        """
        params = dict(task.input_params or {})
        prior_outputs = await repo.list_outputs(int(prior.id))
        prior_shot_versions = await repo.list_shot_versions(int(shot.id))
        from_prior = [v for v in prior_shot_versions if v.render_task_id == int(prior.id)]

        session.add(
            RenderOutput(
                render_task_id=int(task.id),
                project_id=int(task.project_id),
                shot_id=int(shot.id),
                media_file_id=int(prior_outputs[0].media_file_id) if prior_outputs else None,
                output_index=0,
                output_type="video",
                qc_report_id=int(prior_outputs[0].qc_report_id) if prior_outputs and prior_outputs[0].qc_report_id else None,
                is_selected=False,
            )
        )
        if not from_prior:
            media_file_id = int(prior_outputs[0].media_file_id) if prior_outputs else None
            next_no = (int(prior_shot_versions[-1].version_no) + 1) if prior_shot_versions else 1
            session.add(
                ShotVersion(
                    shot_id=int(shot.id),
                    version_no=next_no,
                    media_file_id=media_file_id,
                    render_task_id=int(task.id),
                    qc_report_id=(
                        int(prior_outputs[0].qc_report_id)
                        if prior_outputs and prior_outputs[0].qc_report_id else None
                    ),
                    prompt_snapshot=str_param((task.input_params or {}).get("final_prompt")),
                    seed=None,
                    is_current=True,
                    is_locked=False,
                )
            )
            for old in prior_shot_versions:
                old.is_current = False

        task.status = "succeeded"
        task.actual_cost_credits = 0
        task.model_key = prior.model_key
        task.finished_at = _utcnow()
        task.failure_reason = f"幂等复用任务 #{prior.id} 产物"
        shot.status = "rendered"
        task_id_val = int(task.id)
        project_id_val = int(task.project_id)
        retry_count_val = int(task.retry_count)

        await session.commit()

        # 退款走独立短会话（失败仅记日志，不影响任务终态）
        freeze_id = int_param(params.get("freeze_id"))
        if freeze_id is not None:
            try:
                async with async_session_factory() as pay_session:
                    await billing_service.refund(
                        pay_session, freeze_id=freeze_id, reason="幂等复用产物退款",
                    )
            except BizError as exc:
                logger.warning(
                    "幂等复用退款失败 task_id=%s freeze_id=%s: %s",
                    task_id_val, freeze_id, exc.message,
                )

        await publish_task_event(
            project_id_val, task_id_val, int(shot.id), "succeeded",
            attempt=retry_count_val + 1, progress=100,
        )
        batch_key = str_param(params.get("batch_key"))
        if batch_key:
            await publish_batch_completed_if_done(session, project_id_val, batch_key)

    # ---- 失败收尾 ----

    async def _finish_manual_required(
        self,
        session: AsyncSession,
        repo: RenderRepository,
        task: RenderTask,
        shot: Shot | None,
        qc_detail: dict[str, Any],
    ) -> None:
        """QC 修正循环超限收尾：manual_required + 全额退款。

        Args:
            session: 数据库会话。
            repo: 渲染仓储。
            task: 渲染任务实例。
            shot: 镜头实例（可空）。
            qc_detail: 最后一轮 QC 详情。

        Raises:
            无。
        """
        params = dict(task.input_params or {})
        task.status = "manual_required"
        task.failure_type = "unknown"
        task.failure_reason = f"QC 修正循环超限（{QC_MAX_ROUNDS} 轮未通过）"
        task.finished_at = _utcnow()
        task.actual_cost_credits = await repo.sum_call_cost(int(task.id))
        if shot is not None:
            shot.status = "needs_manual"
        task_id_val = int(task.id)
        project_id_val = int(task.project_id)
        shot_id_val = int(task.shot_id) if task.shot_id else None
        retry_count_val = int(task.retry_count)

        await session.commit()

        # 退款走独立短会话（失败仅记日志，不影响任务终态）
        freeze_id = int_param(params.get("freeze_id"))
        if freeze_id is not None:
            try:
                async with async_session_factory() as pay_session:
                    await billing_service.refund(
                        pay_session, freeze_id=freeze_id, reason="QC 未通过退款",
                    )
            except BizError as exc:
                logger.warning(
                    "QC 退款失败 task_id=%s freeze_id=%s: %s", task_id_val, freeze_id, exc.message,
                )

        await publish_task_event(
            project_id_val, task_id_val, shot_id_val,
            "manual_required", attempt=retry_count_val + 1,
        )
        batch_key = str_param(params.get("batch_key"))
        if batch_key:
            await publish_batch_completed_if_done(session, project_id_val, batch_key)

    async def _finish_failed(
        self,
        session: AsyncSession,
        repo: RenderRepository,
        task: RenderTask,
        shot: Shot | None,
        error: str,
    ) -> None:
        """网关重试超限收尾：failed + 全额退款 + 事件。

        Args:
            session: 数据库会话。
            repo: 渲染仓储。
            task: 渲染任务实例。
            shot: 镜头实例（可空）。
            error: 失败原因。

        Raises:
            无。
        """
        params = dict(task.input_params or {})
        task.status = "failed"
        task.failure_type = "model_unavailable"
        task.failure_reason = error
        task.finished_at = _utcnow()
        task.actual_cost_credits = await repo.sum_call_cost(int(task.id))
        if shot is not None:
            shot.status = "render_failed"
        task_id_val = int(task.id)
        project_id_val = int(task.project_id)
        shot_id_val = int(task.shot_id) if task.shot_id else None
        retry_count_val = int(task.retry_count)

        await session.commit()

        # 退款走独立短会话（失败仅记日志，不影响任务终态）
        freeze_id = int_param(params.get("freeze_id"))
        if freeze_id is not None:
            try:
                async with async_session_factory() as pay_session:
                    await billing_service.refund(
                        pay_session, freeze_id=freeze_id, reason="任务失败退款",
                    )
            except BizError as exc:
                logger.warning(
                    "失败退款失败 task_id=%s freeze_id=%s: %s", task_id_val, freeze_id, exc.message,
                )

        await publish_task_event(
            project_id_val, task_id_val, shot_id_val,
            "failed", attempt=retry_count_val + 1,
        )
        batch_key = str_param(params.get("batch_key"))
        if batch_key:
            await publish_batch_completed_if_done(session, project_id_val, batch_key)


# 进程级执行器单例（API 服务层与 lifespan 共用）
render_executor = RenderExecutor()
