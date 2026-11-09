"""导演视角的画面 QC 门禁（纯函数）。

与既有 ``_qc_check``（伪向量人脸一致性）互补：本模块检查"这一镜有没有
把人、景、物讲清楚"，直接对应审核标准 S1–S4：
- 要素齐备率（S2）：场景 + 应出现的道具；
- 相邻重复帧（S1）：相邻镜不得共用同一首帧；
- 景别多样性（S4）：整板景别去重数 ≥3；
- 台词主体匹配（S3）：说话人必须出现在画面角色中；
- 降级占比：合成降级过多说明素材或抠像不可用。

所有判定都基于合成器自报的 ground truth（spec + degraded），不做像素
猜测，避免再次出现"评分通过但画面不符"的假阳性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.shot_frame_spec import CompositionSpec

#: 要素齐备率合格线
MIN_ELEMENTS_RATIO = 0.90
#: 景别去重数合格线
MIN_SHOT_SIZE_VARIETY = 3
#: 台词主体匹配合格线
MIN_SPEAKER_MATCH_RATIO = 0.90
#: 降级占比告警线
MAX_DEGRADED_RATIO = 0.20


@dataclass
class FrameQcItem:
    """单镜 QC 输入。

    Attributes:
        shot_id: 镜头 ID。
        shot_no: 镜号。
        shot_size: 景别。
        spec: 构图规格（可空表示尚未合成）。
        frame_media_id: 首帧媒体 ID（可空）。
        degraded: 合成降级标记。
        speakers: 该镜台词的说话人姓名列表。
        source_signature: 画面来源签名（场景+角色+道具+景别），用于发现
            "不同首帧但视觉同源"的相邻重复。
    """

    shot_id: int
    shot_no: int
    shot_size: str
    spec: CompositionSpec | None
    frame_media_id: int | None
    degraded: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    source_signature: str = ""


def _has_prop_requirement(item: FrameQcItem) -> bool:
    """判断该镜是否"应有道具出现在画面"。

    Args:
        item: QC 条目。

    Returns:
        True 表示该镜的主题是道具特写或绑定了道具。
    """
    if item.spec is None:
        return False
    return item.spec.focus == "prop" or bool(item.spec.props)


def evaluate_frames(items: list[FrameQcItem]) -> dict[str, Any]:
    """执行四项门禁并汇总问题清单。

    Args:
        items: 整板镜头 QC 条目（按镜序排列）。

    Returns:
        ``{passed, checks, issues}``；issues 每项含 ``code``/``shot_no``/
        ``message``/``severity``。

    Raises:
        无。
    """
    total = len(items)
    issues: list[dict[str, Any]] = []

    complete = 0
    for item in items:
        missing: list[str] = []
        has_setting = bool(item.spec and item.spec.setting)
        has_prop = bool(item.spec and item.spec.props)
        if not has_setting:
            missing.append("场景")
            issues.append(
                {
                    "code": "missing_setting",
                    "shot_no": item.shot_no,
                    "message": f"镜 {item.shot_no} 画面缺少场景基底",
                    "severity": "blocking",
                }
            )
        if _has_prop_requirement(item) and not has_prop:
            missing.append("道具")
            issues.append(
                {
                    "code": "missing_prop",
                    "shot_no": item.shot_no,
                    "message": f"镜 {item.shot_no} 叙事道具未置入画面",
                    "severity": "blocking",
                }
            )
        if item.spec is None or item.frame_media_id is None:
            missing.append("首帧")
            issues.append(
                {
                    "code": "missing_frame",
                    "shot_no": item.shot_no,
                    "message": f"镜 {item.shot_no} 尚未合成首帧，将回退为单资产图驱动",
                    "severity": "warning",
                }
            )
        # 既无角色也无道具的镜头按"场景建立镜"计为齐备（不惩罚空镜）
        if not missing:
            complete += 1

    elements_ratio = 1.0 if total == 0 else round(complete / total, 4)

    duplicates: list[int] = []
    for previous, current in zip(items, items[1:]):
        if (
            previous.frame_media_id is not None
            and previous.frame_media_id == current.frame_media_id
        ):
            duplicates.append(current.shot_no)
            issues.append(
                {
                    "code": "duplicate_frame",
                    "shot_no": current.shot_no,
                    "message": (
                        f"镜 {previous.shot_no} 与镜 {current.shot_no} 复用同一首帧，"
                        "空间与叙事没有推进"
                    ),
                    "severity": "blocking",
                }
            )
        elif (
            previous.source_signature
            and previous.source_signature == current.source_signature
        ):
            # 首帧 ID 不同但来源组合相同（同一场景+同一角色+同景别）时，
            # 视觉上仍是"同一张画"，旧 QC 只看 media_id 会漏判。
            duplicates.append(current.shot_no)
            issues.append(
                {
                    "code": "duplicate_source",
                    "shot_no": current.shot_no,
                    "message": (
                        f"镜 {previous.shot_no} 与镜 {current.shot_no} 画面来源与景别相同，"
                        "换帧后视觉仍重复"
                    ),
                    "severity": "blocking",
                }
            )

    variety = len({item.shot_size for item in items})
    if variety < MIN_SHOT_SIZE_VARIETY:
        issues.append(
            {
                "code": "shot_size_variety",
                "shot_no": None,
                "message": f"整板景别仅 {variety} 种（合格线 {MIN_SHOT_SIZE_VARIETY}），镜头语言单调",
                "severity": "blocking",
            }
        )

    checked_speakers = 0
    matched_speakers = 0
    for item in items:
        names = {subject.name for subject in (item.spec.subjects if item.spec else [])}
        for speaker in item.speakers:
            checked_speakers += 1
            if speaker in names:
                matched_speakers += 1
            else:
                issues.append(
                    {
                        "code": "speaker_absent",
                        "shot_no": item.shot_no,
                        "message": f"镜 {item.shot_no} 台词归属「{speaker}」，但人物不在画面中",
                        "severity": "warning",
                    }
                )
    speaker_ratio = (
        1.0 if checked_speakers == 0 else round(matched_speakers / checked_speakers, 4)
    )

    degraded_count = sum(1 for item in items if item.degraded)
    degraded_ratio = 0.0 if total == 0 else round(degraded_count / total, 4)
    if degraded_ratio > MAX_DEGRADED_RATIO:
        issues.append(
            {
                "code": "degraded_high",
                "shot_no": None,
                "message": (
                    f"降级镜头占比 {degraded_ratio:.0%}"
                    f"（告警线 {MAX_DEGRADED_RATIO:.0%}），素材或抠像质量不足"
                ),
                "severity": "warning",
            }
        )

    blocking = any(issue["severity"] == "blocking" for issue in issues)
    # 台词主体匹配为告警级（空镜/旁白镜可能合法地无人物同框），不作为阻断条件
    passed = (
        not blocking
        and elements_ratio >= MIN_ELEMENTS_RATIO
        and not duplicates
        and variety >= MIN_SHOT_SIZE_VARIETY
    )

    return {
        "passed": passed,
        "checks": {
            "elements": {"ratio": elements_ratio, "threshold": MIN_ELEMENTS_RATIO},
            "duplicates": {"shots": duplicates, "threshold": 0},
            "shot_size_variety": {"value": variety, "threshold": MIN_SHOT_SIZE_VARIETY},
            "speaker_match": {"ratio": speaker_ratio, "threshold": MIN_SPEAKER_MATCH_RATIO},
            "degraded": {"ratio": degraded_ratio, "threshold": MAX_DEGRADED_RATIO},
        },
        "issues": issues,
    }
