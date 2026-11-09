"""分镜契约扩展与道具匹配单测。

覆盖：DirectorShot 接受 prop_names/composition 且拒绝非法类型；道具匹配
优先使用 LLM 名称、文本命中兜底、过短名称与无关资产被过滤。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.services.storyboard_service import DirectorShot, _match_prop_assets


def _shot(**overrides) -> DirectorShot:
    """构造默认导演镜头契约。

    Args:
        **overrides: 覆盖字段。

    Returns:
        DirectorShot 实例。
    """
    data = {
        "order_index": 1,
        "shot_size": "medium",
        "camera_move": "static",
        "duration_sec": 3,
        "description": "林晚站在便利店货架前",
        "dialogue_refs": [1],
        "character_names": ["林晚"],
        "scene_name": "便利店夜内景",
    }
    data.update(overrides)
    return DirectorShot(**data)


def test_director_shot_accepts_prop_names_and_composition() -> None:
    """新增字段可被 LLM 契约接受。"""
    shot = _shot(
        prop_names=["手写纸条"],
        composition={"focus": "prop", "props": [{"name": "手写纸条", "owner": "林晚"}]},
    )
    assert shot.prop_names == ["手写纸条"]
    assert shot.composition["focus"] == "prop"


def test_director_shot_defaults_are_empty() -> None:
    """未提供新字段时保持向后兼容的默认值。"""
    shot = _shot()
    assert shot.prop_names == []
    assert shot.composition is None


@pytest.mark.parametrize("bad", [123, {"a": 1}, "手写纸条"])
def test_director_shot_rejects_non_list_prop_names(bad: object) -> None:
    """prop_names 必须是字符串列表。"""
    with pytest.raises(ValidationError):
        _shot(prop_names=bad)


def _asset(asset_id: int, name: str, asset_type: str = "prop") -> SimpleNamespace:
    """构造轻量资产对象。

    Args:
        asset_id: 资产 ID。
        name: 资产名。
        asset_type: 资产类型。

    Returns:
        含 id/name/asset_type 的对象。
    """
    return SimpleNamespace(id=asset_id, name=name, asset_type=asset_type)


def test_match_props_prefers_explicit_names() -> None:
    """LLM 明确给出的道具名优先命中。"""
    assets = [_asset(31, "手写纸条"), _asset(32, "关东煮")]
    matched = _match_prop_assets(assets, ["关东煮"], "林晚递过一张纸")
    assert [item.name for item in matched] == ["关东煮"]


def test_match_props_falls_back_to_text() -> None:
    """无显式名称时按文本命中兜底。"""
    assets = [_asset(31, "手写纸条"), _asset(32, "关东煮")]
    matched = _match_prop_assets(assets, None, "镜头给到手写纸条的特写")
    assert [item.name for item in matched] == ["手写纸条"]


def test_match_props_ignores_short_names_and_other_types() -> None:
    """单字名称与非道具资产不参与匹配。"""
    assets = [_asset(31, "纸"), _asset(32, "手写纸条"), _asset(33, "林晚", "character")]
    matched = _match_prop_assets(assets, None, "纸 手写纸条 林晚")
    assert [item.name for item in matched] == ["手写纸条"]


def test_shot_out_exposes_composition_and_elements() -> None:
    """镜头响应必须暴露构图与要素清单，前端才能展示徽标。"""
    from app.models.storyboard import Shot
    from app.schemas.storyboard import shot_out

    shot = Shot(
        storyboard_id=1,
        project_id=2,
        episode_id=3,
        shot_no=4,
        order_index=0,
        shot_size="medium",
        camera_move="static",
        duration_ms=3000,
        status="draft",
        composition_spec={
            "version": 1,
            "aspect_ratio": "9:16",
            "canvas": {"w": 720, "h": 1280},
            "shot_size": "medium",
            "setting": {"asset_id": 21, "name": "便利店夜内景"},
            "subjects": [
                {"asset_id": 11, "name": "林晚", "height_ratio": 0.65, "x": 0.5,
                 "facing": "camera", "depth": "mid", "anchor": "bottom"}
            ],
            "props": [],
            "focus": "character",
            "safe_area": {"top": 0.08, "bottom": 0.18},
            "source": "derived",
        },
    )
    data = shot_out(shot)
    assert data["composition_spec"]["canvas"] == {"w": 720, "h": 1280}
    assert data["frame_elements"]["setting"]["name"] == "便利店夜内景"
    assert data["frame_elements"]["subjects"] == ["林晚"]


def test_shot_out_without_spec_returns_none_elements() -> None:
    """无构图时 frame_elements 为 None，不伪造要素。"""
    from app.models.storyboard import Shot
    from app.schemas.storyboard import shot_out

    shot = Shot(
        storyboard_id=1, project_id=2, episode_id=3, shot_no=1, order_index=0,
        shot_size="medium", camera_move="static", duration_ms=3000, status="draft",
    )
    data = shot_out(shot)
    assert data["composition_spec"] is None
    assert data["frame_elements"] is None


def test_frame_redraw_request_defaults_to_compose() -> None:
    """静帧重出默认走本地合成（0 成本）。"""
    from app.schemas.storyboard import FrameRedrawRequest

    assert FrameRedrawRequest().mode == "compose"
    assert FrameRedrawRequest(mode="generate").mode == "generate"


class _FakeStoryboardSession:
    """只支持按主键读取分镜板的最小会话替身。

    Attributes:
        storyboard: 预置的分镜板对象。
    """

    def __init__(self, storyboard: object) -> None:
        """初始化替身会话。

        Args:
            storyboard: 预置分镜板对象。

        Returns:
            无。
        """
        self.storyboard = storyboard

    async def get(self, model, ident):  # type: ignore[no-untyped-def]
        """按模型返回预置分镜板。

        Args:
            model: 模型类。
            ident: 主键。

        Returns:
            分镜板对象或 None。
        """
        if model.__name__ == "Storyboard":
            return self.storyboard
        return None


@pytest.mark.asyncio(loop_scope="session")
async def test_redraw_rejects_locked_storyboard() -> None:
    """分镜板锁定后重出必须被拒绝（40001）。

    锁定后镜头 status 会被渲染流程改写为 rendered，因此必须按分镜板状态
    判定，否则前端禁用可被直接调接口绕过。
    """
    from app.core.errors import BizError
    from app.services.storyboard_service import redraw_shot_frame

    shot = SimpleNamespace(
        id=1,
        project_id=2,
        storyboard_id=3,
        shot_no=1,
        status="rendered",
        redraft_count=0,
    )
    session = _FakeStoryboardSession(SimpleNamespace(id=3, status="locked"))
    with pytest.raises(BizError) as exc:
        await redraw_shot_frame(session, shot, None, 1, "compose")
    assert exc.value.code == 40001
    assert "锁定" in str(exc.value.message)


@pytest.mark.asyncio(loop_scope="session")
async def test_redraw_rejects_locked_shot() -> None:
    """镜头级 locked 状态同样拒绝重出（双保险）。"""
    from app.core.errors import BizError
    from app.services.storyboard_service import redraw_shot_frame

    shot = SimpleNamespace(
        id=1,
        project_id=2,
        storyboard_id=3,
        shot_no=1,
        status="locked",
        redraft_count=0,
    )
    session = _FakeStoryboardSession(SimpleNamespace(id=3, status="draft"))
    with pytest.raises(BizError) as exc:
        await redraw_shot_frame(session, shot, None, 1, "compose")
    assert exc.value.code == 40001
    assert "锁定" in str(exc.value.message)
