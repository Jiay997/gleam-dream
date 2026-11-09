"""一致性服务模块（资产人脸一致性 QC 的确定性伪向量与相似度计算，Task 7b）。

职责：
- :func:`make_deterministic_embedding`：对文本做 SHA-256 分段派生，产出
  确定性 128 维单位向量（同输入恒同输出，无随机性，沙箱阶段替代真实
  人脸 embedding 模型）；
- :func:`cosine_similarity`：两个向量的余弦相似度（结果裁剪到 [-1, 1]）；
- :func:`qc_face_check`：按系统阈值（face_pass_threshold=0.80 /
  face_warn_low=0.68）判定 passed/warning/failed。

伪向量语义（沙箱约定）：资产各版本 embedding 由「资产基向量 + 版本扰动」
在 :mod:`app.services.asset_service` 组装——非 portrait 版本按
``sha256(asset_id + version_type)`` 派生的固定偏移构造，使其与 portrait
基准的余弦相似度确定性落在 0.70~0.95 区间，模拟真实人脸一致性的波动
分布；阈值判定逻辑与真实模型完全一致，后续接入真实 embedding 仅需
替换向量来源。

渲染 QC 契约（Task 17 联调修复）：渲染产物与锚点 portrait 的一致性
模拟遵循同一分布——:func:`make_render_candidate_embedding` 围绕
portrait 基准按种子派生正交偏移构造候选向量，相似度同样确定性落在
0.70~0.95。禁止用自由文本（如镜头 prompt）直接派生候选向量：文本
哈希向量与资产向量体系无关联，相似度必然趋近 0 导致 QC 系统性失败。
"""

from __future__ import annotations

import hashlib
import math

#: 伪 embedding 维度（与 face_embeddings 表 DOUBLE PRECISION[] 对齐）
EMBEDDING_DIM: int = 128

#: 维度分段数（每段独立哈希派生，长文本前缀共享产生温和相关性）
_SEGMENT_COUNT: int = 16

#: 默认阈值（system_configs 缺失时的兜底，对齐种子数据）
DEFAULT_PASS_THRESHOLD: float = 0.80
DEFAULT_WARN_LOW: float = 0.68


def make_deterministic_embedding(text: str) -> list[float]:
    """对文本做 SHA-256 分段派生，产出确定性 128 维单位向量。

    将文本均分为 16 段，每段独立哈希派生 8 维 ``[-1, 1]`` 分量后拼接
    归一化。共享前缀的文本（如相同画风前缀 + 角色外观）会产生部分
    相关维度；同输入恒同输出，保证伪向量可复现。

    Args:
        text: 输入文本（prompt 或资产描述）；空串按固定占位文本处理。

    Returns:
        128 维浮点列表（L2 归一化，范数为 1）。

    Raises:
        无（任何输入均可派生，异常输入按占位文本兜底）。
    """
    clean = (text or "").strip() or "gleam-dream-empty-embedding"
    vec = [0.0] * EMBEDDING_DIM
    per_segment = EMBEDDING_DIM // _SEGMENT_COUNT
    length = len(clean)
    for i in range(_SEGMENT_COUNT):
        start = length * i // _SEGMENT_COUNT
        end = length * (i + 1) // _SEGMENT_COUNT
        # 短文本时段可能为空，用段序号扰动兜底，保证段间独立
        chunk = clean[start:end] if end > start else f"{clean}#{i}"
        digest = hashlib.sha256(f"{i}:{chunk}".encode("utf-8")).digest()
        for j in range(per_segment):
            byte_pair = digest[(j * 2) % len(digest):(j * 2) % len(digest) + 2]
            value = int.from_bytes(byte_pair, "big") / 65535.0 * 2.0 - 1.0
            vec[i * per_segment + j] = value
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度（结果裁剪到 [-1, 1]）。

    Args:
        a: 向量 A。
        b: 向量 B。

    Returns:
        余弦相似度；维度不一致或存在零向量时返回 0.0。

    Raises:
        无（非法输入按 0.0 兜底，不向调用方抛异常）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def qc_face_check(
    portrait_emb: list[float],
    candidate_emb: list[float],
    thresholds: dict[str, float] | None = None,
) -> tuple[float, str]:
    """按阈值执行人脸一致性判定（真实模型与伪向量共用本判定逻辑）。

    判定规则（对齐 00 契约 §6.8）：
    - ``score >= face_pass_threshold``（默认 0.80）→ ``passed``；
    - ``face_warn_low <= score < face_pass_threshold`` → ``warning``；
    - ``score < face_warn_low``（默认 0.68）→ ``failed``。

    Args:
        portrait_emb: portrait 基准向量。
        candidate_emb: 候选版本向量。
        thresholds: 阈值字典（键 ``face_pass_threshold`` /
            ``face_warn_low``；缺省用默认值）。

    Returns:
        ``(score, result)`` 二元组——score 为保留 4 位小数的相似度，
        result 为 passed/warning/failed。

    Raises:
        无（向量非法时相似度为 0.0，判定为 failed）。
    """
    th = thresholds or {}
    pass_at = float(th.get("face_pass_threshold", DEFAULT_PASS_THRESHOLD))
    warn_low = float(th.get("face_warn_low", DEFAULT_WARN_LOW))
    score = round(cosine_similarity(portrait_emb, candidate_emb), 4)
    if score >= pass_at:
        return score, "passed"
    if score >= warn_low:
        return score, "warning"
    return score, "failed"


#: 渲染 QC 候选向量与 portrait 基准的相似度区间（对齐资产版本模拟分布）
RENDER_SIM_MIN: float = 0.70
RENDER_SIM_SPAN: float = 0.25


def make_render_candidate_embedding(portrait_emb: list[float], seed: str) -> list[float]:
    """围绕 portrait 基准构造相似度受控的渲染 QC 候选向量（确定性）。

    规则（对齐资产版本伪向量契约，相似度确定性落在
    ``RENDER_SIM_MIN`` ~ ``RENDER_SIM_MIN + RENDER_SIM_SPAN``）：

    1. ``sim = RENDER_SIM_MIN + RENDER_SIM_SPAN * u``，其中
       ``u = sha256(seed)`` 派生的 [0, 1) 均匀值（同 seed 恒同结果）；
    2. 由种子文本派生偏移向量并相对 portrait 正交化；
    3. ``candidate = normalize(portrait + k * offset)``，
       ``k = sqrt(1/sim^2 - 1)``（正交偏移下余弦相似度恰为 sim）。

    Args:
        portrait_emb: 锚点 portrait 基准向量（128 维单位向量）。
        seed: 确定性种子文本（如 ``"{task_id}:{qc_round}"``）；
            空串按固定占位处理。

    Returns:
        128 维单位向量（L2 归一化），与 portrait 的余弦相似度落在
        受控区间。

    Raises:
        无（portrait 非法/零向量时退化为纯种子派生向量）。
    """
    if not portrait_emb or len(portrait_emb) != EMBEDDING_DIM:
        return make_deterministic_embedding(seed)
    norm = math.sqrt(sum(v * v for v in portrait_emb))
    if norm == 0.0:
        return make_deterministic_embedding(seed)
    unit = [v / norm for v in portrait_emb]

    # 1. 种子 → 目标相似度
    digest = hashlib.sha256(f"render-qc:{seed}".encode("utf-8")).digest()
    u = int.from_bytes(digest[:8], "big") / 2 ** 64
    sim = RENDER_SIM_MIN + RENDER_SIM_SPAN * u
    k = math.sqrt(max(1.0 / (sim * sim) - 1.0, 0.0))

    # 2. 种子偏移向量相对 portrait 正交化（退化时用段序扰动兜底）
    offset = make_deterministic_embedding(f"render-qc-offset:{seed}")
    dot = sum(a * b for a, b in zip(offset, unit))
    residual = [o - dot * p for o, p in zip(offset, unit)]
    rnorm = math.sqrt(sum(v * v for v in residual))
    if rnorm > 1e-6:
        offset = [v / rnorm for v in residual]
    else:  # 极端退化（offset ∥ portrait）：换扰动种子重派生
        offset = make_deterministic_embedding(f"render-qc-ortho-fallback:{seed}")

    # 3. 混合后归一化（k 与 sim 的关系由正交性保证）
    return _render_normalize([p + k * o for p, o in zip(unit, offset)])


def _render_normalize(vec: list[float]) -> list[float]:
    """将向量 L2 归一化（零向量原样返回）。

    Args:
        vec: 原始向量。

    Returns:
        归一化后的向量。

    Raises:
        无。
    """
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return list(vec)
    return [v / norm for v in vec]
