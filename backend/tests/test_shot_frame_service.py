"""合成编排服务单测（Fake session / storage / composer，不触碰真实 DB 与 ffmpeg）。

本任务只验证编排：要素是否齐全、spec 是否写入、MediaFile 是否落库、
首帧是否绑定。真实 ffmpeg 合成已由 Task 3 覆盖，HTTP 链路由端到端脚本覆盖。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.frame_composer import ComposeResult
from app.services.shot_frame_service import (
    FrameInputs,
    build_composition_spec,
    compose_shot_frame,
)
from app.services.shot_frame_spec import AssetRef


class FakeStorage:
    """内存存储替代实现。

    Attributes:
        saved: 已写入的对象键列表。
    """

    def __init__(self) -> None:
        """初始化内存存储。

        Returns:
            无。
        """
        self.saved: list[str] = []

    def save_bytes(self, rel_path: str, data: bytes) -> str:
        """记录写入并返回对象键。

        Args:
            rel_path: 相对路径。
            data: 文件内容。

        Returns:
            对象键。
        """
        self.saved.append(rel_path)
        return rel_path

    def object_path(self, object_key: str) -> Path:
        """把对象键映射为一个真实存在的路径。

        返回测试文件自身：合成编排只校验 ``Path.exists()``，FakeComposer
        不读取文件内容，因此无需真实素材。

        Args:
            object_key: 对象键。

        Returns:
            当前测试文件的绝对路径（必然存在）。
        """
        return Path(__file__)


class FakeComposer:
    """固定返回 PNG 字节的合成器替代实现。

    Attributes:
        calls: 调用参数记录。
    """

    def __init__(self) -> None:
        """初始化替代合成器。

        Returns:
            无。
        """
        self.calls: list[dict] = []

    def compose(self, *, spec, setting_path, layers) -> ComposeResult:
        """记录调用并返回固定结果。

        Args:
            spec: 构图规格。
            setting_path: 场景图路径。
            layers: 前景图层。

        Returns:
            固定 PNG 字节的合成结果。
        """
        self.calls.append({"spec": spec, "setting_path": setting_path, "layers": layers})
        return ComposeResult(
            png=b"\x89PNG\r\n\x1a\nfake",
            width=720,
            height=1280,
            degraded=[],
            placed=[layer.name for layer in layers],
        )


class _FakeResult:
    """最小查询结果替代实现（仅支持 scalars().all()）。

    Attributes:
        _rows: 模拟返回的行列表。
    """

    def __init__(self, rows: list[object]) -> None:
        """保存模拟行。

        Args:
            rows: 模拟返回的 ORM 行列表。

        Returns:
            无。
        """
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        """返回自身（行即标量对象）。

        Returns:
            自身实例。
        """
        return self

    def all(self) -> list[object]:
        """返回全部行。

        Returns:
            行列表副本。
        """
        return list(self._rows)


class FakeSession:
    """最小 async 会话替代实现（仅支持 get/add/flush/execute）。

    Attributes:
        objects: ``(模型名, 主键)`` → 实例映射。
        added: 已 add 的实例列表。
        flushed: flush 次数。
    """

    def __init__(self, objects: dict[tuple[str, int], object]) -> None:
        """初始化会话替代实现。

        Args:
            objects: 预置对象映射。

        Returns:
            无。
        """
        self.objects = objects
        self.added: list[object] = []
        self.flushed = 0

    async def get(self, model, ident):  # type: ignore[no-untyped-def]
        """按模型与主键读取对象。

        Args:
            model: 模型类。
            ident: 主键。

        Returns:
            预置对象或 None。
        """
        return self.objects.get((model.__name__, int(ident)))

    async def execute(self, stmt):  # type: ignore[no-untyped-def]
        """模拟 ``select(AssetVersion).where(asset_id==X, is_current==True)``。

        从语句 whereclause 提取等值条件（asset_id / is_current），在预置
        对象图中筛选 AssetVersion 实例并按 ID 降序返回。

        Args:
            stmt: SQLAlchemy select 语句。

        Returns:
            支持 ``scalars().all()`` 的结果对象。

        Raises:
            无：条件解析失败时按空条件处理，返回全部版本。
        """
        from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

        def _eq_value(column_key: str) -> object | None:
            """从 where 子句提取某列的等值右值。

            Args:
                column_key: 列属性名（如 ``asset_id``）。

            Returns:
                等值条件的右值；无匹配条件时返回 None。
            """
            cond = getattr(stmt, "whereclause", None)
            if cond is None:
                return None
            clauses = (
                list(cond.clauses) if isinstance(cond, BooleanClauseList)
                else [cond] if isinstance(cond, BinaryExpression) else []
            )
            for part in clauses:
                left = getattr(part, "left", None)
                if left is not None and getattr(left, "key", None) == column_key:
                    return getattr(getattr(part, "right", None), "value", None)
            return None

        asset_id = _eq_value("asset_id")
        rows = [
            obj
            for (model_name, _ident), obj in self.objects.items()
            if model_name == "AssetVersion"
            and (asset_id is None or getattr(obj, "asset_id", None) == asset_id)
            and bool(getattr(obj, "is_current", False))
        ]
        rows.sort(key=lambda item: int(getattr(item, "id", 0)), reverse=True)
        return _FakeResult(rows)

    def add(self, obj: object) -> None:
        """登记新增对象。

        Args:
            obj: ORM 实例。

        Returns:
            无。
        """
        self.added.append(obj)

    async def flush(self) -> None:
        """为新增的 MediaFile 分配自增 ID。

        Returns:
            无。
        """
        self.flushed += 1
        for offset, obj in enumerate(self.added, start=1):
            if obj.__class__.__name__ == "MediaFile" and getattr(obj, "id", None) is None:
                obj.id = 900 + offset


def _shot() -> SimpleNamespace:
    """构造测试用镜头对象。

    Returns:
        含要素绑定字段的轻量对象。
    """
    return SimpleNamespace(
        id=684,
        project_id=845,
        storyboard_id=203,
        shot_no=4,
        shot_size="medium",
        camera_move="static",
        action_desc="林晚把热豆浆递给陈姨",
        dialogue="林晚：您慢点。",
        final_prompt="便利店夜内景，林晚递豆浆",
        character_asset_ids=[11, 12],
        scene_asset_id=21,
        prop_asset_ids=[31],
        composition_spec=None,
        first_frame_media_id=None,
    )


def _objects() -> dict[tuple[str, int], object]:
    """构造资产/版本/媒体对象图。

    Returns:
        供 FakeSession 使用的对象映射。
    """
    def media(media_id: int, key: str) -> SimpleNamespace:
        """构造媒体对象。

        Args:
            media_id: 媒体 ID。
            key: 对象键。

        Returns:
            媒体对象。
        """
        return SimpleNamespace(id=media_id, object_key=key, mime_type="image/png")

    objects: dict[tuple[str, int], object] = {
        ("Asset", 11): SimpleNamespace(id=11, name="林晚", asset_type="character",
                                       current_version_id=111, description="米色风衣"),
        ("Asset", 12): SimpleNamespace(id=12, name="陈姨", asset_type="character",
                                       current_version_id=122, description="深色外套"),
        ("Asset", 21): SimpleNamespace(id=21, name="便利店夜内景", asset_type="scene",
                                       current_version_id=211, description="冷白灯管"),
        ("Asset", 31): SimpleNamespace(id=31, name="热豆浆", asset_type="prop",
                                       current_version_id=311, description="纸杯"),
        # 林晚：单版本 portrait（安全参考类型）
        ("AssetVersion", 111): SimpleNamespace(
            id=111, asset_id=11, media_file_id=1001, is_current=True,
            version_type="portrait"),
        # 陈姨：表情集（九宫格）为最新版本——参考帧准入应跳过它选 portrait
        ("AssetVersion", 121): SimpleNamespace(
            id=121, asset_id=12, media_file_id=1002, is_current=True,
            version_type="expression_set"),
        ("AssetVersion", 122): SimpleNamespace(
            id=122, asset_id=12, media_file_id=1005, is_current=True,
            version_type="portrait"),
        ("AssetVersion", 211): SimpleNamespace(
            id=211, asset_id=21, media_file_id=1003, is_current=True,
            version_type="scene_ref"),
        ("AssetVersion", 311): SimpleNamespace(
            id=311, asset_id=31, media_file_id=1004, is_current=True,
            version_type="prop_ref"),
        ("MediaFile", 1001): media(1001, "assets/11.png"),
        ("MediaFile", 1002): media(1002, "assets/12_expression_grid.png"),
        ("MediaFile", 1003): media(1003, "assets/21.png"),
        ("MediaFile", 1004): media(1004, "assets/31.png"),
        ("MediaFile", 1005): media(1005, "assets/12_portrait.png"),
    }
    return objects


def test_build_spec_includes_scene_subjects_and_props() -> None:
    """编排输入必须同时带场景、两个角色与一个道具。"""
    inputs = FrameInputs(
        shot_no=4,
        shot_size="medium",
        camera_move="static",
        action_desc="林晚把热豆浆递给陈姨",
        dialogue="林晚：您慢点。",
        aspect_ratio="9:16",
        characters=[AssetRef(asset_id=11, name="林晚"), AssetRef(asset_id=12, name="陈姨")],
        scene=AssetRef(asset_id=21, name="便利店夜内景", role="scene"),
        props=[AssetRef(asset_id=31, name="热豆浆", role="prop")],
        speaker_names=["林晚"],
    )
    spec = build_composition_spec(inputs)
    assert spec.setting is not None
    assert spec.setting["name"] == "便利店夜内景"
    assert [item.name for item in spec.subjects] == ["林晚", "陈姨"]
    assert spec.props[0].name == "热豆浆"
    assert spec.canvas == {"w": 720, "h": 1280}


def test_manual_spec_takes_precedence() -> None:
    """已有人工 spec 时不再重新推导。"""
    inputs = FrameInputs(
        shot_no=1, shot_size="medium", camera_move="static",
        action_desc="", dialogue="", aspect_ratio="9:16",
        characters=[], scene=None, props=[], speaker_names=[],
    )
    manual = build_composition_spec(inputs)
    manual = manual.model_copy(update={"source": "manual", "focus": "scene"})
    assert build_composition_spec(inputs, existing=manual) is manual


@pytest.mark.asyncio(loop_scope="session")
async def test_compose_binds_media_and_first_frame() -> None:
    """编排后应落下 image 类型 MediaFile 并回写首帧。"""
    shot = _shot()
    session = FakeSession(_objects())
    storage = FakeStorage()
    composer = FakeComposer()

    result = await compose_shot_frame(
        session, shot, storage=storage, composer=composer, aspect_ratio="9:16"
    )

    assert result["shot_id"] == 684
    assert result["media_url"].startswith("/files/frames/845/")
    assert storage.saved and storage.saved[0].startswith("frames/845/")
    # FakeSession 自增起始为 901（900 + 首个 offset 1）
    assert shot.first_frame_media_id == 901
    assert session.flushed >= 1
    assert len(composer.calls) == 1
    assert {layer.name for layer in composer.calls[0]["layers"]} == {"林晚", "陈姨", "热豆浆"}
    # 参考帧准入（2026-10-29）：陈姨的表情集（九宫格）必须被跳过，
    # 取图应返回 portrait 单视角版本（assets/12_portrait.png）。
    from app.services.shot_frame_service import _resolve_media_key

    key_chenyi = await _resolve_media_key(session, 12)
    assert key_chenyi == "assets/12_portrait.png"
    key_linwan = await _resolve_media_key(session, 11)
    assert key_linwan == "assets/11.png"


@pytest.mark.asyncio(loop_scope="session")
async def test_compose_degrades_without_scene() -> None:
    """无场景资产时仍能合成并显式标记降级。"""
    shot = _shot()
    shot.scene_asset_id = None
    session = FakeSession(_objects())

    result = await compose_shot_frame(
        session, shot, storage=FakeStorage(), composer=FakeComposer(), aspect_ratio="9:16"
    )

    assert result["degraded"] == ["no_setting"]
    assert shot.first_frame_media_id is not None
