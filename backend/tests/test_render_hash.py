"""渲染入参哈希单测：首帧必须影响幂等键。

背景：合成换帧后若哈希不变，任务会被"幂等复用"到旧视频，表现为
"改了首帧但成片没变"。这里锁定首帧参与摘要这一行为。
"""

from __future__ import annotations

from app.services.render_service import RENDER_PIPELINE_VERSION, build_input_hash


def test_hash_changes_when_frame_changes() -> None:
    """首帧媒体 ID 变化必须导致哈希变化。"""
    base = dict(
        prompt="便利店夜内景，林晚递豆浆",
        duration_ms=3000,
        anchor_ids=[111, 211],
        aspect_ratio="9:16",
    )
    first = build_input_hash(frame_media_id=1001, **base)
    second = build_input_hash(frame_media_id=1002, **base)
    assert first != second


def test_hash_changes_when_frame_removed() -> None:
    """有首帧与无首帧必须区分。"""
    base = dict(prompt="p", duration_ms=3000, anchor_ids=[1], aspect_ratio="9:16")
    assert build_input_hash(frame_media_id=1001, **base) != build_input_hash(**base)


def test_hash_is_stable_for_same_inputs() -> None:
    """同样入参必须得到同样哈希（幂等前提）。"""
    kwargs = dict(
        prompt="p", duration_ms=3000, anchor_ids=[2, 1],
        voice_clip_ids=[5], aspect_ratio="9:16", frame_media_id=77,
    )
    assert build_input_hash(**kwargs) == build_input_hash(**kwargs)


def test_pipeline_version_bumped_for_composition_change() -> None:
    """构图规则升级时管线版本必须同步递增，否则旧产物会被误复用。"""
    from app.services.shot_frame_spec import LAYOUT_VERSION

    # 2026-11 首帧修复后管线版本升级为 i2v-motion-v9：
    # 首帧优先级改为「本镜合成首帧 → 同场尾帧 → 资产图」，
    # 提交前自动合成首帧，换场禁止尾帧续接。
    assert RENDER_PIPELINE_VERSION.startswith("i2v-motion-v9-")
    assert LAYOUT_VERSION >= 7
