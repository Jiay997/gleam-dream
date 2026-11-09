"""确定性模型路由模块（ADR-07：路由决策不交给 LLM）。

从 ``model_routes``（任务类型 → 候选模型，priority 1 最高）读取候选，
叠加可用性 / 能力匹配 / 熔断器过滤后，按打分公式确定性选出唯一模型：

    score = 0.7 * quality_score - 0.3 * normalized_price

其中 ``normalized_price`` 为 ``unit_price_credits`` 在同能力候选集内的
min-max 归一化（0~1）。``quality_score`` 列设计域为 0.00~10.00，但种子
数据以 0-1 制存值（如 0.88），打分前统一折算为 10 分制（值 ≤ 1 时 ×10），
否则价格惩罚会系统性压过质量分，导致高质量模型永远落选。

熔断器为进程内存实现：同一模型连续失败 ≥5 次进入 open（60s 冷却），
冷却期满进入 half-open 放行一次探测——成功则清零恢复，失败则重新 open。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models import ModelRegistry, ModelRoute

logger = logging.getLogger("gleamdream.gateway")

# 熔断阈值：连续失败次数
BREAKER_FAIL_THRESHOLD: int = 5
# 熔断冷却时长（秒）：期满后 half-open 放行探测
BREAKER_COOLDOWN_SECONDS: float = 60.0

# 打分权重
SCORE_QUALITY_WEIGHT: float = 0.7
SCORE_PRICE_WEIGHT: float = 0.3


class CircuitBreaker:
    """进程内模型熔断器。

    以 ``model_key`` 为粒度记录连续失败次数：达到阈值进入 open 状态并
    记录开启时间；冷却期满自动放行（half-open），探测成功清零恢复，
    探测失败重新进入 open。

    Attributes:
        _fail_counts: model_key → 当前连续失败次数。
        _opened_at: model_key → 最近一次进入 open 的 monotonic 时间。
    """

    def __init__(
        self,
        fail_threshold: int = BREAKER_FAIL_THRESHOLD,
        cooldown_seconds: float = BREAKER_COOLDOWN_SECONDS,
    ) -> None:
        """构造熔断器。

        Args:
            fail_threshold: 连续失败阈值，默认 5。
            cooldown_seconds: open 状态冷却时长（秒），默认 60。
        """
        self._fail_threshold = fail_threshold
        self._cooldown_seconds = cooldown_seconds
        self._fail_counts: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, model_key: str) -> bool:
        """判断模型当前是否处于 open（拒绝路由）状态。

        冷却期满即视为 half-open：本方法返回 False 放行下一次调用，
        由 record_success / record_failure 决定恢复或重新熔断。

        Args:
            model_key: 模型键。

        Returns:
            处于 open 冷却期内返回 True；未熔断或已到 half-open 返回 False。
        """
        opened_at = self._opened_at.get(model_key)
        if opened_at is None:
            return False
        if time.monotonic() - opened_at >= self._cooldown_seconds:
            return False
        return True

    def record_success(self, model_key: str) -> None:
        """记录一次成功调用：清零失败计数并退出 open 状态。

        Args:
            model_key: 模型键。
        """
        self._fail_counts.pop(model_key, None)
        self._opened_at.pop(model_key, None)

    def record_failure(self, model_key: str) -> None:
        """记录一次失败调用：累计计数，达到阈值进入 open。

        half-open 探测失败时计数已 ≥ 阈值，将刷新 open 时间重新冷却。

        Args:
            model_key: 模型键。
        """
        count = self._fail_counts.get(model_key, 0) + 1
        self._fail_counts[model_key] = count
        if count >= self._fail_threshold:
            self._opened_at[model_key] = time.monotonic()
            logger.warning("模型连续失败 %s 次，熔断开启: %s", count, model_key)

    def reset(self, model_key: str | None = None) -> None:
        """手动重置熔断状态（测试/运维用）。

        Args:
            model_key: 指定模型键；为 None 时重置全部。
        """
        if model_key is None:
            self._fail_counts.clear()
            self._opened_at.clear()
        else:
            self._fail_counts.pop(model_key, None)
            self._opened_at.pop(model_key, None)


# 进程级熔断器单例（网关路由与记账共用）
circuit_breaker = CircuitBreaker()


def _score_model(registry: ModelRegistry, candidates: list[ModelRegistry]) -> float:
    """计算候选模型的路由得分。

    公式：``0.7 * quality_score - 0.3 * normalized_price``。质量分统一按
    10 分制解释（种子以 0-1 制存值时 ×10 折算）；单价在同能力候选集内
    min-max 归一化（最高价 1.0、最低价 0.0，全部同价时归零）。

    Args:
        registry: 待打分模型。
        candidates: 同能力候选集（用于价格归一化）。

    Returns:
        模型得分（越高越优先）。
    """
    quality = float(registry.quality_score or 0.0)
    if 0.0 < quality <= 1.0:
        quality *= 10.0
    price = float(registry.unit_price_credits or 0.0)
    # 防御：candidates 可能在某些调用路径为空（或全部缺价），此时价格
    # 无法归一化，按 0.0 处理（等价于只按质量分排序），避免 min([]) 崩溃。
    prices = [
        float(item.unit_price_credits or 0.0)
        for item in candidates
        if item is not None
    ]
    if prices:
        low, high = min(prices), max(prices)
        normalized_price = 0.0 if high == low else (price - low) / (high - low)
    else:
        normalized_price = 0.0
    return SCORE_QUALITY_WEIGHT * quality - SCORE_PRICE_WEIGHT * normalized_price


async def pick_models(
    session: AsyncSession,
    capability: str,
    task_type: str,
    user_locked_model: str | None = None,
    *,
    exclude_keys: set[str] | None = None,
    limit: int = 3,
) -> list[ModelRegistry]:
    """按确定性得分返回前 N 个候选模型（用于运行时跨提供商降级）。

    与 :func:`pick_model` 的差异：不只返回第一名，而是返回按
    ``score = 0.7 * quality - 0.3 * price`` 降序的候选列表，供网关在
    首个模型返回 5xx/超时/空响应时切换到下一候选。

    Args:
        session: 数据库会话（由调用方管理生命周期）。
        capability: 请求能力（llm/image/video/tts/bgm）。
        task_type: 任务类型（如 llm_text / text_to_image / tts）。
        user_locked_model: 用户锁定的模型键（非空时只返回该模型）。
        exclude_keys: 本次调用中已失败、需要排除的模型键集合。
        limit: 最多返回候选数量（默认 3）。

    Returns:
        候选模型列表（可能为空）。

    Raises:
        BizError: 锁定模型不可用/已熔断，或任务类型无路由。
    """
    excluded = set(exclude_keys or set())

    if user_locked_model:
        result = await session.execute(
            select(ModelRegistry).where(
                ModelRegistry.model_key == user_locked_model,
                ModelRegistry.is_available.is_(True),
            )
        )
        locked = result.scalar_one_or_none()
        if locked is None or locked.capability != capability:
            raise BizError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"锁定的模型不可用: {user_locked_model}",
            )
        if circuit_breaker.is_open(locked.model_key):
            raise BizError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"锁定的模型已熔断，请稍后重试: {user_locked_model}",
            )
        return [locked]

    route_result = await session.execute(
        select(ModelRoute)
        .where(ModelRoute.task_type == task_type, ModelRoute.is_active.is_(True))
        .order_by(ModelRoute.priority.asc())
    )
    routes: list[ModelRoute] = list(route_result.scalars().all())
    if not routes:
        raise BizError(ErrorCode.MODEL_UNAVAILABLE, f"任务类型无可用路由: {task_type}")

    ordered_keys = [route.model_key for route in routes]
    registry_result = await session.execute(
        select(ModelRegistry).where(
            ModelRegistry.model_key.in_(ordered_keys),
            ModelRegistry.is_available.is_(True),
            ModelRegistry.capability == capability,
        )
    )
    by_key: dict[str, ModelRegistry] = {
        item.model_key: item for item in registry_result.scalars().all()
    }
    # 保持 priority 先序，再过滤熔断与本次已失败候选
    candidates = [
        by_key[key]
        for key in ordered_keys
        if key in by_key
        and key not in excluded
        and not circuit_breaker.is_open(key)
    ]
    if not candidates:
        raise BizError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"任务类型 {task_type} 无可用候选模型（能力 {capability}）",
        )
    priority_index = {key: idx for idx, key in enumerate(ordered_keys)}
    # 同分时按 priority 先序，保证确定性
    # 关键修复：CPython 的 list.sort() 在排序期间会把列表临时清空
    # （元素先搬入临时数组，排序完成后回填）。若 key 函数在此期间引用
    # 被排序的列表本身，会读到空列表（_score_model 内 min(prices) 崩溃
    # → 10003）。因此必须先算好分数，再对 (分数, 模型) 二元组排序。
    scored = [
        (_score_model(item, candidates), item)
        for item in candidates
    ]
    scored.sort(
        key=lambda pair: (
            -pair[0],
            priority_index.get(pair[1].model_key, len(priority_index)),
        )
    )
    candidates = [item for _, item in scored]
    logger.info(
        "路由候选 task_type=%s capability=%s candidates=%s",
        task_type, capability, [item.model_key for item in candidates[:limit]],
    )
    return candidates[: max(1, limit)]


async def pick_model(
    session: AsyncSession,
    capability: str,
    task_type: str,
    user_locked_model: str | None = None,
) -> ModelRegistry:
    """确定性选择本次生成使用的唯一模型（兼容旧调用方）。

    流程：
    1. ``user_locked_model`` 非空 → 按 model_key 查询且须可用、能力匹配、
       未熔断，否则抛 80001；
    2. 查 ``model_routes``（task_type 匹配且 is_active，按 priority 升序）；
    3. 加载候选模型注册行，过滤 is_available / capability 匹配 / 熔断未开；
    4. 打分取最高（同分按 priority 先序，保证确定性）；
    5. 任一步无候选 → BizError(80001)。

    Args:
        session: 数据库会话（由调用方管理生命周期）。
        capability: 请求能力（llm/image/video/tts/bgm）。
        task_type: 任务类型（如 llm_text / text_to_image / tts）。
        user_locked_model: 用户锁定的模型键（可选）。

    Returns:
        选中的模型注册行。

    Raises:
        BizError: 80001 锁定模型不可用、无路由、或过滤后无可用候选。
    """
    models = await pick_models(
        session, capability, task_type, user_locked_model, limit=1
    )
    best = models[0]
    logger.info(
        "路由命中 task_type=%s capability=%s model=%s",
        task_type, capability, best.model_key,
    )
    return best
