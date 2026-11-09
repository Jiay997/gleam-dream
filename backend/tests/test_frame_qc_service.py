"""导演 QC 门禁单测（纯函数）。

四项门禁：要素齐备率、相邻重复帧、景别多样性、台词主体匹配；
外加降级占比告警。测试直接构造 FrameQcItem，不依赖数据库。
"""

from __future__ import annotations

from app.services.frame_qc_service import FrameQcItem, evaluate_frames
from app.services.shot_frame_spec import (
    AssetRef,
    CompositionSpec,
    derive_composition_spec,
)


def _spec(
    *, subjects: list[str], setting: str | None = "便利店", props: list[str] | None = None
) -> CompositionSpec:
    """构造测试用 spec。

    Args:
        subjects: 角色名列表。
        setting: 场景名（None 表示无场景）。
        props: 道具名列表。

    Returns:
        合法的 CompositionSpec。
    """
    return derive_composition_spec(
        shot_no=1,
        shot_size="medium",
        camera_move="static",
        action_desc="",
        dialogue="",
        aspect_ratio="9:16",
        characters=[AssetRef(asset_id=index + 100, name=name) for index, name in enumerate(subjects)],
        scene=AssetRef(asset_id=200, name=setting, role="scene") if setting else None,
        props=[AssetRef(asset_id=300 + index, name=name, role="prop") for index, name in enumerate(props or [])],
        speaker_names=[],
    )


def _item(
    shot_no: int,
    *,
    subjects: list[str],
    setting: str | None = "便利店",
    props: list[str] | None = None,
    frame_media_id: int | None = 1000,
    degraded: list[str] | None = None,
    speakers: list[str] | None = None,
    shot_size: str = "medium",
) -> FrameQcItem:
    """构造 QC 条目。

    Args:
        shot_no: 镜号。
        subjects: 角色名列表。
        setting: 场景名。
        props: 道具名列表。
        frame_media_id: 首帧媒体 ID。
        degraded: 降级标记。
        speakers: 台词说话人。
        shot_size: 景别。

    Returns:
        FrameQcItem 实例。
    """
    return FrameQcItem(
        shot_id=shot_no,
        shot_no=shot_no,
        shot_size=shot_size,
        spec=_spec(subjects=subjects, setting=setting, props=props),
        frame_media_id=frame_media_id,
        degraded=list(degraded or []),
        speakers=list(speakers or []),
    )


def test_all_good_frames_pass() -> None:
    """要素齐全且无重复时整体通过。"""
    items = [
        _item(1, subjects=["林晚"], shot_size="wide"),
        _item(2, subjects=["林晚", "陈姨"], frame_media_id=1001, shot_size="medium",
              props=["热豆浆"]),
        _item(3, subjects=[], frame_media_id=1002, shot_size="close_up"),
    ]
    report = evaluate_frames(items)
    assert report["passed"] is True
    assert report["checks"]["elements"]["ratio"] == 1.0


def test_missing_setting_is_blocking() -> None:
    """缺场景的镜头计入要素缺失并阻断。"""
    items = [
        _item(1, subjects=["林晚"], setting=None),
        _item(2, subjects=["林晚"], frame_media_id=1001),
        _item(3, subjects=["林晚"], frame_media_id=1002),
    ]
    report = evaluate_frames(items)
    assert report["passed"] is False
    assert any(issue["code"] == "missing_setting" for issue in report["issues"])


def test_duplicate_adjacent_frames_are_blocking() -> None:
    """相邻镜头共用同一首帧会被拦截。"""
    items = [
        _item(1, subjects=["林晚"], frame_media_id=1000),
        _item(2, subjects=["林晚"], frame_media_id=1000),
        _item(3, subjects=["林晚"], frame_media_id=1002),
    ]
    report = evaluate_frames(items)
    assert any(issue["code"] == "duplicate_frame" for issue in report["issues"])
    assert report["passed"] is False


def test_single_shot_size_is_blocking() -> None:
    """整板只有一种景别时判为镜头语言不合格。"""
    items = [_item(index, subjects=["林晚"], frame_media_id=1000 + index) for index in range(1, 6)]
    report = evaluate_frames(items)
    assert any(issue["code"] == "shot_size_variety" for issue in report["issues"])


def test_speaker_not_in_subjects_warns() -> None:
    """说话人不在画面角色中时告警（不阻断）。"""
    items = [
        _item(1, subjects=["陈姨"], speakers=["林晚"], shot_size="close_up"),
        _item(2, subjects=["林晚"], frame_media_id=1001),
        _item(3, subjects=["林晚"], frame_media_id=1002, shot_size="wide"),
    ]
    report = evaluate_frames(items)
    codes = [issue["code"] for issue in report["issues"]]
    assert "speaker_absent" in codes
    assert report["passed"] is True


def test_degraded_ratio_warns() -> None:
    """降级占比过高时告警。"""
    items = [
        _item(1, subjects=["林晚"], degraded=["card_layout"]),
        _item(2, subjects=["林晚"], frame_media_id=1001, degraded=["no_setting"]),
        _item(3, subjects=["林晚"], frame_media_id=1002, shot_size="wide"),
    ]
    report = evaluate_frames(items)
    assert report["checks"]["degraded"]["ratio"] > 0.2
    assert any(issue["code"] == "degraded_high" for issue in report["issues"])
