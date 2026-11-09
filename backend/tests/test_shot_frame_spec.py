"""构图契约与规则推导单测（纯函数，不依赖数据库与 ffmpeg）。

覆盖：景别→身高比单调、单人/双人/多人布局、说话人优先、道具特写、
LLM 提示越界归一化、画幅映射、无角色场景镜。
"""

from __future__ import annotations

import pytest

from app.services.shot_frame_spec import (
    AssetRef,
    CompositionSpec,
    derive_composition_spec,
    normalize_spec,
)


def _char(asset_id: int, name: str) -> AssetRef:
    """构造角色资产引用。

    Args:
        asset_id: 资产 ID。
        name: 角色名。

    Returns:
        角色类型的 AssetRef。
    """
    return AssetRef(asset_id=asset_id, name=name, role="character")


def _prop(asset_id: int, name: str) -> AssetRef:
    """构造道具资产引用。

    Args:
        asset_id: 资产 ID。
        name: 道具名。

    Returns:
        道具类型的 AssetRef。
    """
    return AssetRef(asset_id=asset_id, name=name, role="prop")


def _scene(asset_id: int, name: str) -> AssetRef:
    """构造场景资产引用。

    Args:
        asset_id: 资产 ID。
        name: 场景名。

    Returns:
        场景类型的 AssetRef。
    """
    return AssetRef(asset_id=asset_id, name=name, role="scene")


def _derive(**overrides):
    """用默认参数调用推导并允许覆盖。

    Args:
        **overrides: 覆盖默认关键字参数。

    Returns:
        推导出的 CompositionSpec。
    """
    kwargs = {
        "shot_no": 1,
        "shot_size": "medium",
        "camera_move": "static",
        "action_desc": "林晚站在便利店货架前",
        "dialogue": "林晚：今天也辛苦了。",
        "aspect_ratio": "9:16",
        "characters": [_char(11, "林晚")],
        "scene": _scene(21, "便利店夜内景"),
        "props": [],
        "speaker_names": ["林晚"],
    }
    kwargs.update(overrides)
    return derive_composition_spec(**kwargs)


def test_canvas_is_vertical_720x1280() -> None:
    """默认画幅必须是 9:16 的 720×1280。"""
    spec = _derive()
    assert spec.canvas == {"w": 720, "h": 1280}
    assert spec.aspect_ratio == "9:16"


@pytest.mark.parametrize(
    "shot_size,expected",
    [
        ("extreme_wide", 0.22),
        ("wide", 0.38),
        ("medium", 0.65),
        ("over_shoulder", 0.80),
        ("close_up", 0.95),
        ("extreme_close_up", 1.10),
    ],
)
def test_shot_size_maps_to_height_ratio(shot_size: str, expected: float) -> None:
    """六种景别各自映射到确定的身高占比。"""
    spec = _derive(shot_size=shot_size)
    assert spec.subjects[0].height_ratio == pytest.approx(expected, abs=1e-6)


def test_single_character_centered_and_facing_camera() -> None:
    """单角色居中、面向镜头、底部落地。"""
    subject = _derive().subjects[0]
    assert subject.x == pytest.approx(0.5)
    assert subject.facing == "camera"
    assert subject.anchor == "bottom"


def test_single_close_up_varies_framing_by_shot_no() -> None:
    """同一角色两次单人特写按镜号取景微变，避免首帧像素级重复。"""
    first = _derive(shot_size="close_up", shot_no=6).subjects[0]
    second = _derive(shot_size="close_up", shot_no=8).subjects[0]
    assert (first.height_ratio, first.x) != (second.height_ratio, second.x)


def test_over_shoulder_varies_staging_by_shot_no() -> None:
    """同一组反打再次出现时按镜号微调站位，避免两镜首帧完全一致。"""
    def ots(shot_no: int):
        return _derive(
            shot_size="over_shoulder",
            shot_no=shot_no,
            characters=[_char(11, "林晚"), _char(12, "陈姨")],
            speaker_names=["陈姨"],
            action_desc="陈姨递过照片，林晚低头看",
        ).subjects

    first, second = ots(5), ots(9)
    assert (first[0].x, first[0].height_ratio) != (
        second[0].x,
        second[0].height_ratio,
    )


def test_two_characters_face_each_other() -> None:
    """双角色分列左右并互为朝向。"""
    subjects = _derive(
        characters=[_char(11, "林晚"), _char(12, "陈姨")],
        speaker_names=["林晚"],
        action_desc="林晚与陈姨在便利店门口告别",
    ).subjects
    assert len(subjects) == 2
    assert subjects[0].x == pytest.approx(0.28)
    assert subjects[1].x == pytest.approx(0.72)
    assert subjects[0].facing == "right"
    assert subjects[1].facing == "left"


def test_speaker_moves_to_left_slot_when_second() -> None:
    """第二位角色说话时，说话人被排到左侧主位（谁说话给谁）。"""
    subjects = _derive(
        characters=[_char(11, "林晚"), _char(12, "陈姨")],
        speaker_names=["陈姨"],
        action_desc="陈姨把纸条递给林晚",
    ).subjects
    assert subjects[0].name == "陈姨"
    assert subjects[0].x == pytest.approx(0.28)


def test_prop_closeup_becomes_focus_subject() -> None:
    """动作描述含「特写」且有道具时，道具成为视觉重心。"""
    spec = _derive(
        action_desc="手写纸条特写，字迹被雨水洇开",
        props=[_prop(31, "手写纸条")],
        characters=[],
        speaker_names=[],
    )
    assert spec.focus == "prop"
    assert spec.props[0].height_ratio == pytest.approx(0.55)
    assert spec.props[0].x == pytest.approx(0.5)
    assert spec.subjects == []


def test_no_character_falls_back_to_scene_focus() -> None:
    """无角色的空镜以场景为重心。"""
    spec = _derive(characters=[], speaker_names=[], props=[])
    assert spec.focus == "scene"
    assert spec.subjects == []


def test_orphan_prop_goes_bottom_right_and_keeps_owner_none() -> None:
    """无归属道具固定右下，owner 保持 None（供 QC 标记 orphan_prop）。"""
    spec = _derive(props=[_prop(31, "关东煮")])
    assert spec.props[0].x == pytest.approx(0.86)
    assert spec.props[0].y == pytest.approx(0.90)
    assert spec.props[0].owner is None


def test_llm_hint_out_of_range_is_clamped() -> None:
    """LLM 给出的越界坐标被钳位而非抛错。"""
    spec = _derive(llm_hint={"subjects": [{"name": "林晚", "x": 1.8}]})
    assert spec.subjects[0].x == pytest.approx(0.95)
    assert spec.source == "llm"


def test_llm_hint_unknown_field_ignored() -> None:
    """LLM 提示中的未知字段被忽略，spec 仍合法。"""
    spec = _derive(llm_hint={"subjects": [{"name": "林晚", "bogus": 1}], "focus": "nope"})
    assert spec.focus == "character"
    assert spec.subjects[0].x == pytest.approx(0.5)


def test_horizontal_canvas_follows_aspect_ratio() -> None:
    """16:9 时画布切换为 1280×720。"""
    spec = _derive(aspect_ratio="16:9")
    assert spec.canvas == {"w": 1280, "h": 720}


def test_normalize_spec_recovers_from_broken_json() -> None:
    """历史脏数据回落 fallback，不抛异常。"""
    fallback = _derive()
    assert normalize_spec({"subjects": "oops"}, fallback=fallback) is fallback
    assert normalize_spec(None, fallback=fallback) is fallback
    assert isinstance(normalize_spec(fallback.model_dump(), fallback=fallback), CompositionSpec)
