# 分镜整幅要素画面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让每一镜产出一张 9:16 的整幅要素画面（场景基底 + 角色置入 + 道具置入），以它驱动视频渲染，并用导演 QC 门禁拦截不合格画面。

**Architecture:** ① 纯函数把镜头字段推导为 `CompositionSpec`；② `LocalFrameComposer` 用 ffmpeg 抠白底叠加合成 PNG；③ `shot_frame_service` 落 `MediaFile` 并绑定 `shots.first_frame_media_id`；④ 渲染层"首帧优先"逻辑不变，但 `build_input_hash` 加入首帧 ID，保证换帧即换视频；⑤ `frame_qc_service` 以纯函数做四项门禁。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 async / Alembic / Pydantic v2 / ffmpeg 8.1（本地 CPU，0 积分）/ pytest + pytest-asyncio / Vue 3 + TypeScript

**Spec:** `docs/superpowers/specs/2026-10-27-shot-composition-design.md`

> **实施结果（2026-10-27 已完成）**：10 个任务全部落地，48 项新增测试通过；845 端到端验收 10/10 合成成功、QC 通过、成片 720×1280 / 34.17s 可播放。
> 实施中与本文档的 **4 处偏差**（以代码为准）：
> 1. `colorkey` 必须前置 `format=rgba`（PNG 常为 rgb24，否则 ffmpeg 退出 -5），且相似度由 0.14 提高到 **0.40**（实测 0.14 留大片白边/双重曝光，0.55 会抠穿人脸）。
> 2. 新增**人物宽度上限**（主角色 46%、次角色 42%、道具 28% 画布宽），否则方形定妆照按屏高缩放会横向溢出并与人重叠。
> 3. 双人位置由 0.34/0.66 改为 **0.28/0.72**（避免 14% 画布宽的重叠）。
> 4. 追加修复 `post_service.compose`：video 轨有效性判定从"片段数量一致"改为"每个片段 source_id 等于镜头当前版本"，否则重渲染后成片画面不变。
> 详细证据见 `测试报告/深夜的暖_分镜整幅画面复测_20261027.md`。

## Global Constraints

- 输出画布固定 `720×1280`（`aspect_ratio="9:16"`），其他画幅按 `9:16 / 16:9 / 1:1` 映射。
- 合成环节 **0 积分**，禁止调用任何付费模型；模型精修只走预留的 `FrameComposer` 接口。
- 禁止在画面上渲染文字（规避 AI 乱码与水印）。
- 降级必须显式记录（`degraded` 列表），禁止静默产出。
- `media_files.storage_provider` 仅允许 `cos` / `oss`，本地落库统一写 `cos`。
- 所有新增函数/类/文件必须有中文 docstring，覆盖参数与返回值说明（项目既有风格）。
- 新模块统一 `shot_frame_*` / `frame_*` 前缀，**禁止**与既有 `composition_service.py`（编排图/渲染计划）混用命名。
- 项目目录当前**不是 git 仓库**：若 `git rev-parse --is-inside-work-tree` 失败，跳过 commit 步骤，改为在 `docs/superpowers/plans/progress.md` 追加一行变更记录。
- 后端命令统一在 `e:\FeiSi\Project code\gleam-dream\backend` 下执行，解释器用 `.venv\Scripts\python`。

---

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `backend/app/services/shot_frame_spec.py` | 新建 | 构图契约 + 规则推导（纯函数，无 IO） |
| `backend/tests/test_shot_frame_spec.py` | 新建 | 推导规则单测 |
| `backend/alembic/versions/f2a3b4c5d6e7_shot_composition_spec.py` | 新建 | 新增 `shots.composition_spec` |
| `backend/app/models/storyboard.py` | 修改 | `Shot.composition_spec` 列 |
| `backend/tests/test_shot_composition_migration.py` | 新建 | 列存在性校验 |
| `backend/app/services/frame_composer.py` | 新建 | ffmpeg 合成器 + 抠像覆盖率检测 |
| `backend/tests/test_frame_composer.py` | 新建 | 真实 ffmpeg 合成测试 |
| `backend/app/services/shot_frame_service.py` | 新建 | 取图层 → 合成 → 落库 → 绑首帧 |
| `backend/tests/test_shot_frame_service.py` | 新建 | Fake session/storage 编排测试 |
| `backend/app/services/storyboard_service.py` | 修改 | `DirectorShot` 扩展、道具纯函数匹配、生成时写 spec、`redraw` 双模式、批量合成编排 |
| `backend/app/schemas/storyboard.py` | 修改 | `shot_out` 要素扩展、`FrameRedrawRequest.mode`、新增 `FrameComposeRequest` |
| `backend/tests/test_shot_frame_contracts.py` | 新建 | 契约与 `shot_out` 单测 |
| `backend/app/services/render_service.py` | 修改 | `build_input_hash` 加首帧、管线升 v5 |
| `backend/tests/test_render_hash.py` | 新建 | 哈希变化单测 |
| `backend/app/services/frame_qc_service.py` | 新建 | 四项门禁（纯函数） |
| `backend/tests/test_frame_qc_service.py` | 新建 | 门禁判定单测 |
| `backend/app/api/storyboards.py` | 修改 | 新增批量合成与 QC 路由 |
| `frontend/src/api/v4.ts` | 修改 | 合成/QC 接口与类型 |
| `frontend/src/components/v4/StoryboardV4Panel.vue` | 修改 | 要素徽标、双模式重出、批量合成 |
| `测试报告/scripts/stage8_compose_frames.py` | 新建 | 845 全量合成 + 复测脚本 |

---

### Task 1: 构图契约与规则推导

**Files:**
- Create: `backend/app/services/shot_frame_spec.py`
- Test: `backend/tests/test_shot_frame_spec.py`

**Interfaces:**
- Consumes: 无（纯函数，仅依赖 pydantic）
- Produces:
  - `AssetRef(asset_id: int, name: str, role: str = "character", media_key: str | None = None)`
  - `SubjectPlacement(asset_id, name, height_ratio, x, facing, depth, anchor)`
  - `PropPlacement(asset_id, name, owner, height_ratio, x, y, depth)`
  - `CompositionSpec(version, aspect_ratio, canvas, shot_size, setting, subjects, props, focus, safe_area, source)`
  - `derive_composition_spec(*, shot_no, shot_size, camera_move, action_desc, dialogue, aspect_ratio, characters, scene, props, speaker_names=None, llm_hint=None) -> CompositionSpec`
  - `normalize_spec(raw: dict | None, *, fallback: CompositionSpec) -> CompositionSpec`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_shot_frame_spec.py`：

```python
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


def test_two_characters_face_each_other() -> None:
    """双角色分列左右并互为朝向。"""
    subjects = _derive(
        characters=[_char(11, "林晚"), _char(12, "陈姨")],
        speaker_names=["林晚"],
        action_desc="林晚与陈姨在便利店门口告别",
    ).subjects
    assert len(subjects) == 2
    assert subjects[0].x == pytest.approx(0.34)
    assert subjects[1].x == pytest.approx(0.66)
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
    assert subjects[0].x == pytest.approx(0.34)


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
    assert spec.props[0].x == pytest.approx(0.72)
    assert spec.props[0].y == pytest.approx(0.78)
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
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_spec.py -x -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.shot_frame_spec'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/services/shot_frame_spec.py`：

```python
"""镜头整幅要素画面的构图契约与规则推导。

本模块只做纯计算：输入镜头字段与资产引用，输出可直接交给合成器使用的
``CompositionSpec``。不访问数据库、不读写文件，便于单测与复现。

设计要点：
- 位置与比例由**确定性规则**推导（可测、可复现），LLM 提示仅作可选覆盖；
- LLM 覆盖值一律钳位到合法区间，非法输入回落规则推导，保证 spec 恒合法；
- 画幅、景别、安全区三者共同决定"人物多大、落在哪"，避免漂浮与遮挡。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

#: 景别 → 角色屏幕高度占比（保证"特写大、全景小"的直觉）
_SHOT_HEIGHT_RATIO: dict[str, float] = {
    "extreme_close_up": 1.10,
    "close_up": 0.95,
    "over_shoulder": 0.80,
    "medium": 0.65,
    "wide": 0.38,
    "extreme_wide": 0.22,
}

#: 画幅 → 画布尺寸（像素）
_CANVAS_BY_RATIO: dict[str, tuple[int, int]] = {
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "1:1": (720, 720),
}

#: 画幅 → 底部安全区（字幕避让，不渲染文字）
_SAFE_BOTTOM: dict[str, float] = {"9:16": 0.18, "16:9": 0.10, "1:1": 0.12}
#: 画幅 → 顶部安全区
_SAFE_TOP: dict[str, float] = {"9:16": 0.08, "16:9": 0.06, "1:1": 0.06}

#: 合法画幅（非枚举时回落 9:16）
_VALID_RATIOS = frozenset(_CANVAS_BY_RATIO)


class AssetRef(BaseModel):
    """图层引用的资产快照（ID + 名称 + 类型 + 可选媒体键）。

    Attributes:
        asset_id: 资产 ID。
        name: 资产名称（角色名/场景名/道具名）。
        role: 资产类型（character/scene/prop）。
        media_key: 当前版本图片的存储对象键（可空）。
    """

    asset_id: int
    name: str
    role: Literal["character", "scene", "prop"] = "character"
    media_key: str | None = None


class SubjectPlacement(BaseModel):
    """角色在图幅中的位置与比例。

    Attributes:
        asset_id: 角色资产 ID。
        name: 角色名。
        height_ratio: 身高占画布高度的比例（>1 表示裁切上半身）。
        x: 角色中心横坐标（0~1）。
        facing: 朝向（left/right/camera/away）。
        depth: 景深层次（near/mid/far），决定叠加顺序。
        anchor: 垂直锚点（bottom 底部落地 / center 居中）。
    """

    asset_id: int
    name: str
    height_ratio: float = Field(ge=0.05, le=1.2)
    x: float = Field(ge=0.0, le=1.0)
    facing: Literal["left", "right", "camera", "away"] = "camera"
    depth: Literal["near", "mid", "far"] = "mid"
    anchor: Literal["bottom", "center"] = "bottom"


class PropPlacement(BaseModel):
    """道具在图幅中的位置与比例。

    Attributes:
        asset_id: 道具资产 ID。
        name: 道具名。
        owner: 归属角色名；None 表示独立静物（QC 标记 orphan_prop）。
        height_ratio: 高度占画布高度的比例。
        x: 中心横坐标（0~1）。
        y: 中心纵坐标（0~1）。
        depth: 景深层次。
    """

    asset_id: int
    name: str
    owner: str | None = None
    height_ratio: float = Field(ge=0.03, le=0.6)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    depth: Literal["near", "mid", "far"] = "near"


class CompositionSpec(BaseModel):
    """单镜整幅要素画面的完整构图描述。

    Attributes:
        version: 结构版本（便于后续演进）。
        aspect_ratio: 画幅（9:16/16:9/1:1）。
        canvas: 画布尺寸 {"w": int, "h": int}。
        shot_size: 景别（与 shots.shot_size 对齐）。
        setting: 场景基底资产 {"asset_id", "name"}，可空。
        subjects: 角色置入列表（从远到近）。
        props: 道具置入列表。
        focus: 视觉重心（character/prop/scene）。
        safe_area: 字幕安全区比例 {"top", "bottom"}。
        source: spec 来源（derived/llm/manual）。
    """

    version: int = 1
    aspect_ratio: str = "9:16"
    canvas: dict[str, int]
    shot_size: str
    setting: dict[str, Any] | None = None
    subjects: list[SubjectPlacement] = []
    props: list[PropPlacement] = []
    focus: Literal["character", "prop", "scene"] = "character"
    safe_area: dict[str, float]
    source: Literal["derived", "llm", "manual"] = "derived"


def _clamp(value: float, low: float, high: float) -> float:
    """把数值钳位到闭区间。

    Args:
        value: 原始数值。
        low: 下界。
        high: 上界。

    Returns:
        钳位后的浮点数。

    Raises:
        无：非数值由调用方先行校验。
    """
    return max(low, min(high, float(value)))


def _resolve_ratio(aspect_ratio: str | None) -> str:
    """归一化画幅字符串。

    Args:
        aspect_ratio: 项目画幅（可空或非法）。

    Returns:
        合法画幅（非法时固定 9:16）。
    """
    text = str(aspect_ratio or "").strip()
    return text if text in _VALID_RATIOS else "9:16"


def _layout_subjects(
    characters: list[AssetRef],
    height_ratio: float,
    speaker_names: list[str] | None,
) -> list[SubjectPlacement]:
    """按人数与说话人推导角色布局。

    规则：单人居中面向镜头；双人分列 0.34/0.66 且互为朝向，说话人占左主位；
    三人及以上沿 0.10~0.90 均分，越靠右越远越小。

    Args:
        characters: 角色资产列表（已按说话人优先排序）。
        height_ratio: 主角色身高占比。
        speaker_names: 说话人姓名列表（可空）。

    Returns:
        角色置入列表；无角色时返回空列表。
    """
    count = len(characters)
    if count == 0:
        return []
    names = {item.name for item in characters}
    speaker = next((name for name in (speaker_names or []) if name in names), None)

    if count == 1:
        return [
            SubjectPlacement(
                asset_id=characters[0].asset_id,
                name=characters[0].name,
                height_ratio=round(height_ratio, 3),
                x=0.5,
                facing="camera",
                depth="mid",
            )
        ]

    if count == 2:
        order = characters
        if speaker is not None and characters[1].name == speaker:
            order = [characters[1], characters[0]]
        return [
            SubjectPlacement(
                asset_id=order[0].asset_id,
                name=order[0].name,
                height_ratio=round(height_ratio, 3),
                x=0.34,
                facing="right",
                depth="mid",
            ),
            SubjectPlacement(
                asset_id=order[1].asset_id,
                name=order[1].name,
                height_ratio=round(_clamp(height_ratio * 0.92, 0.05, 1.2), 3),
                x=0.66,
                facing="left",
                depth="far",
            ),
        ]

    step = 0.8 / (count - 1)
    placements: list[SubjectPlacement] = []
    for index, character in enumerate(characters[:4]):
        placements.append(
            SubjectPlacement(
                asset_id=character.asset_id,
                name=character.name,
                height_ratio=round(_clamp(height_ratio * (1 - 0.06 * index), 0.05, 1.2), 3),
                x=round(_clamp(0.1 + step * index, 0.05, 0.95), 3),
                facing="camera",
                depth="mid" if index == 0 else "far",
            )
        )
    return placements


def _layout_props(
    props: list[AssetRef],
    *,
    focus_prop: bool,
) -> list[PropPlacement]:
    """推导道具布局。

    道具特写时只保留一个主道具并置于画面中心；否则作为归属物放在右下
    手部区域，多个道具上下错开。归属关系由 LLM 提示补充，推导阶段保持
    ``owner=None``（QC 据此标记 orphan_prop）。

    Args:
        props: 道具资产列表。
        focus_prop: 是否为主题特写。

    Returns:
        道具置入列表。
    """
    if not props:
        return []
    if focus_prop:
        head = props[0]
        return [
            PropPlacement(
                asset_id=head.asset_id,
                name=head.name,
                owner=None,
                height_ratio=0.55,
                x=0.5,
                y=0.55,
                depth="near",
            )
        ]
    placements: list[PropPlacement] = []
    for index, prop in enumerate(props[:3]):
        placements.append(
            PropPlacement(
                asset_id=prop.asset_id,
                name=prop.name,
                owner=None,
                height_ratio=0.18 if index == 0 else 0.12,
                x=round(_clamp(0.72 - 0.10 * index, 0.1, 0.9), 3),
                y=0.78 if index == 0 else 0.62,
                depth="near",
            )
        )
    return placements


def _apply_llm_hint(base: CompositionSpec, hint: dict[str, Any]) -> CompositionSpec:
    """把 LLM 构图提示合并到规则结果上（按角色名对齐、越界钳位）。

    Args:
        base: 规则推导出的 spec。
        hint: LLM 输出的构图提示（结构不可信，逐字段校验）。

    Returns:
        合并后的 spec；任何非法输入都返回 base，不抛异常。
    """
    try:
        data = base.model_dump()
        patches = {
            str(item.get("name")): item
            for item in (hint.get("subjects") or [])
            if isinstance(item, dict) and item.get("name")
        }
        subjects: list[dict[str, Any]] = []
        for subject in base.subjects:
            item = subject.model_dump()
            patch = patches.get(subject.name)
            if patch:
                if isinstance(patch.get("x"), (int, float)):
                    item["x"] = round(_clamp(float(patch["x"]), 0.05, 0.95), 4)
                if patch.get("facing") in ("left", "right", "camera", "away"):
                    item["facing"] = patch["facing"]
            subjects.append(item)
        data["subjects"] = subjects
        if hint.get("focus") in ("character", "prop", "scene"):
            data["focus"] = hint["focus"]
        data["source"] = "llm"
        return CompositionSpec(**data)
    except (ValidationError, TypeError, ValueError):
        return base


def derive_composition_spec(
    *,
    shot_no: int,
    shot_size: str,
    camera_move: str,
    action_desc: str | None,
    dialogue: str | None,
    aspect_ratio: str | None = "9:16",
    characters: list[AssetRef] | None = None,
    scene: AssetRef | None = None,
    props: list[AssetRef] | None = None,
    speaker_names: list[str] | None = None,
    llm_hint: dict[str, Any] | None = None,
) -> CompositionSpec:
    """按镜头语义推导整幅要素画面的构图。

    Args:
        shot_no: 镜号（写入 spec 便于排查，当前不参与布局）。
        shot_size: 景别（6 值，未知回落 medium）。
        camera_move: 运镜（记录用途）。
        action_desc: 动作/画面描述（用于判定「特写」）。
        dialogue: 对白文本（当前仅记录，不参与布局）。
        aspect_ratio: 项目画幅。
        characters: 角色资产列表（说话人优先）。
        scene: 场景资产（可空）。
        props: 道具资产列表。
        speaker_names: 说话人姓名列表。
        llm_hint: LLM 构图提示（可选覆盖）。

    Returns:
        合法的 CompositionSpec；任何输入都不会抛异常。

    Raises:
        无：内部异常由兜底逻辑吞掉并返回规则结果。
    """
    ratio = _resolve_ratio(aspect_ratio)
    width, height = _CANVAS_BY_RATIO[ratio]
    size = shot_size if shot_size in _SHOT_HEIGHT_RATIO else "medium"
    height_ratio = _SHOT_HEIGHT_RATIO[size]
    characters = list(characters or [])
    props = list(props or [])
    text = str(action_desc or "")

    subjects = _layout_subjects(characters, height_ratio, speaker_names)
    focus_prop = "特写" in text and bool(props)
    placements = _layout_props(props, focus_prop=focus_prop)
    if focus_prop:
        focus: Literal["character", "prop", "scene"] = "prop"
    elif characters:
        focus = "character"
    else:
        focus = "scene"

    spec = CompositionSpec(
        aspect_ratio=ratio,
        canvas={"w": width, "h": height},
        shot_size=size,
        setting=(
            {"asset_id": scene.asset_id, "name": scene.name} if scene is not None else None
        ),
        subjects=subjects,
        props=placements,
        focus=focus,
        safe_area={"top": _SAFE_TOP[ratio], "bottom": _SAFE_BOTTOM[ratio]},
        source="derived",
    )
    if llm_hint:
        return _apply_llm_hint(spec, llm_hint)
    return spec


def normalize_spec(raw: dict | None, *, fallback: CompositionSpec) -> CompositionSpec:
    """把历史/外部 spec 归一化为合法对象。

    Args:
        raw: 待解析的 spec 字典（可空或结构损坏）。
        fallback: 解析失败时的回落 spec。

    Returns:
        合法的 CompositionSpec；失败时返回 fallback 本身。
    """
    if not isinstance(raw, dict):
        return fallback
    try:
        return CompositionSpec(**raw)
    except (ValidationError, TypeError, ValueError):
        return fallback
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_spec.py -q
```
Expected: PASS（17 passed：12 个测试函数，景别用例参数化展开为 6 个）

- [ ] **Step 5: 记录变更**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream"; git rev-parse --is-inside-work-tree
```
若失败（预期失败），在 `docs/superpowers/plans/progress.md` 追加：`- [x] Task 1 完成：shot_frame_spec.py + 12 项单测通过`。若成功，则 `git add backend/app/services/shot_frame_spec.py backend/tests/test_shot_frame_spec.py` 并 `git commit -m "feat(storyboard): add composition spec derivation"`。

---

### Task 2: `shots.composition_spec` 落库

**Files:**
- Modify: `backend/app/models/storyboard.py`（`prop_asset_ids` 字段之后、`first_frame_media_id` 之前插入）
- Create: `backend/alembic/versions/f2a3b4c5d6e7_shot_composition_spec.py`
- Test: `backend/tests/test_shot_composition_migration.py`

**Interfaces:**
- Consumes: Task 1 的 `CompositionSpec`（仅作为 JSON 结构契约，此任务不导入该类）
- Produces: `Shot.composition_spec: Mapped[dict | None]`（JSONB，可空）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_shot_composition_migration.py`：

```python
"""校验 shots.composition_spec 列已由迁移创建。

迁移的验收不能只靠“命令退出码为 0”：这里直接查 information_schema，
确认列名与类型都正确。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_composition_spec_column_exists_as_jsonb() -> None:
    """表 shots 必须存在 jsonb 类型的 composition_spec 列。"""
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'shots' AND column_name = 'composition_spec'"
                )
            )
        ).first()
    assert row is not None, "shots.composition_spec 不存在，请执行 alembic upgrade head"
    assert row[0] == "jsonb"
    assert row[1] == "YES"
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_composition_migration.py -q
```
Expected: FAIL — `AssertionError: shots.composition_spec 不存在`

- [ ] **Step 3: 加模型列与迁移**

在 `backend/app/models/storyboard.py` 的 `prop_asset_ids` 字段之后插入：

```python
    composition_spec: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="整幅要素画面构图（CompositionSpec，9:16 场景+角色+道具布局）",
    )
```

同时在类 docstring 的 `Attributes` 中补一行：

```text
        composition_spec: 整幅要素画面构图（CompositionSpec JSON，V6.13 新增）。
```

创建 `backend/alembic/versions/f2a3b4c5d6e7_shot_composition_spec.py`：

```python
"""为镜头增加整幅要素画面构图列。

背景：分镜只落「要素绑定 + 长 prompt」，渲染层从资产图中择一充当画面，
导致同一张定妆照被多镜复用、场景与道具从不参与构图。新增
``shots.composition_spec`` 保存 CompositionSpec（画幅/景别/角色位置/道具
归属/字幕安全区），供本地合成器产出 9:16 整幅要素帧。

变更内容：``shots`` 新增 ``composition_spec``（JSONB, nullable），历史
数据保持 NULL（由服务层回落规则推导，不伪造历史构图）。

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: 当前迁移版本号
revision: str = "f2a3b4c5d6e7"
#: 上一版本号
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增 shots.composition_spec 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.add_column(
        "shots",
        sa.Column(
            "composition_spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="整幅要素画面构图（CompositionSpec，9:16 场景+角色+道具布局）",
        ),
    )


def downgrade() -> None:
    """移除 shots.composition_spec 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.drop_column("shots", "composition_spec")
```

- [ ] **Step 4: 执行迁移并运行测试**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m alembic upgrade head
```
Expected: `Running upgrade e1f2a3b4c5d6 -> f2a3b4c5d6e7`

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_composition_migration.py -q
```
Expected: PASS（1 passed）

- [ ] **Step 5: 记录变更**

同 Task 1 的提交策略，记录：`- [x] Task 2 完成：shots.composition_spec JSONB 迁移已 upgrade head`。

---

### Task 3: 本地合成器（ffmpeg）

**Files:**
- Create: `backend/app/services/frame_composer.py`
- Test: `backend/tests/test_frame_composer.py`

**Interfaces:**
- Consumes: Task 1 的 `CompositionSpec`
- Produces:
  - `LayerImage(asset_id: int, name: str, path: Path, kind: str)`
  - `ComposeResult(png: bytes, width: int, height: int, degraded: list[str], placed: list[str])`
  - `FrameComposer`（Protocol，方法 `compose(*, spec, setting_path, layers) -> ComposeResult`）
  - `LocalFrameComposer().compose(...)`
  - `alpha_coverage(path: Path) -> float`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_frame_composer.py`：

```python
"""本地合成器集成测试（真实 ffmpeg，不依赖数据库与模型网关）。

测试素材由 ffmpeg lavfi 现场生成：白底人形块、白底道具块、深色场景。
断言画布尺寸、图层确实叠加（亮度跨度变大）、纯白素材降级为卡片排版。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.frame_composer import LayerImage, LocalFrameComposer, alpha_coverage
from app.services.shot_frame_spec import (
    AssetRef,
    CompositionSpec,
    PropPlacement,
    SubjectPlacement,
    derive_composition_spec,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="本机未安装 ffmpeg"
)


def _make_image(path: Path, *, size: str, box: str | None = None) -> Path:
    """用 ffmpeg lavfi 生成测试图片。

    Args:
        path: 输出文件路径。
        size: lavfi 尺寸（如 ``400x600``）。
        box: 可选 drawbox 参数（``x:y:w:h:color``），用于造前景。

    Returns:
        生成的文件路径。

    Raises:
        subprocess.CalledProcessError: ffmpeg 生成失败。
    """
    filters = []
    if box:
        x, y, w, h, color = box.split(":")
        filters.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill")
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=white:s={size}",
    ]
    if filters:
        command += ["-vf", ",".join(filters)]
    command += ["-frames:v", "1", str(path)]
    subprocess.run(command, check=True, timeout=60, capture_output=True)
    return path


def _probe_size(path: Path) -> tuple[int, int]:
    """读取图片宽高。

    Args:
        path: 图片路径。

    Returns:
        ``(width, height)`` 像素值。

    Raises:
        RuntimeError: ffprobe 无法解析尺寸。
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
        ],
        check=True, timeout=60, capture_output=True, text=True,
    )
    width, height = str(result.stdout).strip().split(",")[:2]
    return int(width), int(height)


def _luma_span(path: Path) -> float:
    """读取图片亮度最大最小值之差（用于判断是否纯色）。

    Args:
        path: 图片路径。

    Returns:
        亮度跨度（0~255）；解析失败返回 0。

    Raises:
        subprocess.CalledProcessError: ffmpeg 执行失败。
    """
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "info", "-i", str(path),
            "-vf", "signalstats,metadata=print", "-frames:v", "1", "-f", "null", "-",
        ],
        check=True, timeout=60, capture_output=True, text=True,
    )
    values = {}
    for line in str(result.stderr).splitlines():
        for key in ("YMIN", "YMAX"):
            if f"lavfi.signalstats.{key}=" in line:
                values[key] = float(line.split("=", 1)[1])
    if "YMIN" not in values or "YMAX" not in values:
        return 0.0
    return values["YMAX"] - values["YMIN"]


def _spec(**overrides) -> CompositionSpec:
    """构造用于合成测试的 spec。

    Args:
        **overrides: 覆盖默认参数。

    Returns:
        合法的 CompositionSpec。
    """
    kwargs = {
        "shot_no": 1,
        "shot_size": "medium",
        "camera_move": "static",
        "action_desc": "林晚站在便利店货架前",
        "dialogue": "",
        "aspect_ratio": "9:16",
        "characters": [AssetRef(asset_id=11, name="林晚")],
        "scene": AssetRef(asset_id=21, name="便利店", role="scene"),
        "props": [],
    }
    kwargs.update(overrides)
    return derive_composition_spec(**kwargs)


def test_alpha_coverage_high_for_white_bg_subject(tmp_path: Path) -> None:
    """白底人形素材抠像后仍有可观前景覆盖率。"""
    fg = _make_image(tmp_path / "fg.png", size="400x600", box="140:80:120:460:red")
    assert alpha_coverage(fg) > 0.10


def test_alpha_coverage_near_zero_for_all_white(tmp_path: Path) -> None:
    """纯白素材抠像后覆盖率接近 0（用于触发降级）。"""
    blank = _make_image(tmp_path / "blank.png", size="400x600")
    assert alpha_coverage(blank) < 0.02


def test_compose_produces_vertical_canvas_with_layers(tmp_path: Path) -> None:
    """合成结果为 720×1280，且亮度跨度证明图层已叠加。"""
    scene = _make_image(tmp_path / "scene.png", size="1280x720")
    fg = _make_image(tmp_path / "fg.png", size="400x600", box="140:80:120:460:red")
    prop = _make_image(tmp_path / "prop.png", size="200x200", box="60:60:80:80:blue")
    spec = _spec(props=[AssetRef(asset_id=31, name="手写纸条", role="prop")])

    result = LocalFrameComposer().compose(
        spec=spec,
        setting_path=scene,
        layers=[
            LayerImage(asset_id=11, name="林晚", path=fg, kind="character"),
            LayerImage(asset_id=31, name="手写纸条", path=prop, kind="prop"),
        ],
    )

    assert (result.width, result.height) == (720, 1280)
    out = tmp_path / "out.png"
    out.write_bytes(result.png)
    assert _probe_size(out) == (720, 1280)
    assert _luma_span(out) > 20
    assert result.degraded == []
    assert "林晚" in result.placed


def test_compose_degrades_to_card_when_key_fails(tmp_path: Path) -> None:
    """纯白前景（抠像无主体）降级为卡片排版并显式记录。"""
    scene = _make_image(tmp_path / "scene.png", size="1280x720")
    blank = _make_image(tmp_path / "blank.png", size="400x600")
    spec = _spec()

    result = LocalFrameComposer().compose(
        spec=spec,
        setting_path=scene,
        layers=[LayerImage(asset_id=11, name="林晚", path=blank, kind="character")],
    )

    assert "card_layout" in result.degraded


def test_compose_without_setting_uses_gradient_base(tmp_path: Path) -> None:
    """无场景素材时用深色底，仍产出合法画布。"""
    fg = _make_image(tmp_path / "fg.png", size="400x600", box="140:80:120:460:red")
    spec = _spec(scene=None)

    result = LocalFrameComposer().compose(
        spec=spec,
        setting_path=None,
        layers=[LayerImage(asset_id=11, name="林晚", path=fg, kind="character")],
    )

    assert (result.width, result.height) == (720, 1280)
    assert "no_setting" in result.degraded
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_frame_composer.py -x -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.frame_composer'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/services/frame_composer.py`：

```python
"""镜头整幅要素画面的本地合成器（ffmpeg，0 积分）。

职责：把 ``CompositionSpec`` 描述的场景基底与角色/道具前景合成为一张
竖屏 PNG。前景用 ``colorkey`` 抠白底后按景别比例叠加，背景做轻虚化形成
浅景深；若抠像后前景覆盖率过低（素材非白底），自动降级为卡片排版并
在结果中标记，绝不静默输出"白底人物直接糊在场景上"的中间态。

为什么用 ffmpeg 而不是 Pillow：本机未安装 Pillow，且 ffmpeg 已是渲染
链路的硬依赖（stub 视频合成），复用可避免引入新依赖。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

#: 抠像参数：先转 rgba（PNG 常为 rgb24，colorkey 需要 alpha 通道才能工作），
#: 再按颜色抠白并做边缘混合
_KEY_FILTER = "format=rgba,colorkey=0xFFFFFF:0.14:0.06"

#: 前景覆盖率低于该值视为抠像失败（素材非白底）
_MIN_COVERAGE = 0.05

#: 深色渐变底（无场景素材时使用）
_FALLBACK_BASE = "0x16181D"


@dataclass
class LayerImage:
    """一张待叠加的前景图层（磁盘路径已解析）。

    Attributes:
        asset_id: 图层来源资产 ID。
        name: 图层名称（角色名/道具名）。
        path: 图片磁盘绝对路径。
        kind: 图层类型（character/prop）。
    """

    asset_id: int
    name: str
    path: Path
    kind: str


@dataclass
class ComposeResult:
    """合成结果。

    Attributes:
        png: PNG 文件字节。
        width: 输出宽度（像素）。
        height: 输出高度（像素）。
        degraded: 降级标记列表（no_setting/card_layout/no_subject）。
        placed: 成功置入的图层名称列表。
    """

    png: bytes
    width: int
    height: int
    degraded: list[str] = field(default_factory=list)
    placed: list[str] = field(default_factory=list)


class FrameComposer(Protocol):
    """合成器协议：本地实现与模型精修实现共用同一签名。"""

    def compose(
        self,
        *,
        spec: object,
        setting_path: Path | None,
        layers: list[LayerImage],
    ) -> ComposeResult:
        """把 spec 与图层合成为一张整幅画面。

        Args:
            spec: CompositionSpec 实例（含画布/坐标/安全区）。
            setting_path: 场景基底图片路径（可空）。
            layers: 前景图层列表。

        Returns:
            合成结果。
        """
        ...


def _run_ffmpeg(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """执行 ffmpeg 并返回结果（统一异常语义）。

    Args:
        command: 完整命令行参数列表。
        timeout: 超时秒数。

    Returns:
        已完成进程对象。

    Raises:
        RuntimeError: ffmpeg 不存在或执行返回非零。
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg 不可用")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"ffmpeg 执行失败: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "")[-500:]
        raise RuntimeError(f"ffmpeg 返回 {result.returncode}: {detail}")
    return result


def alpha_coverage(image_path: Path) -> float:
    """估算白底抠像后的前景覆盖率。

    做法：对图片先 ``colorkey`` 抠白，再 ``alphaextract`` 取 alpha 通道，
    用 ``signalstats`` 读取 alpha 平均亮度（0~255）并归一化。这样无需
    图像解码库即可判断"抠完还剩多少前景"。

    Args:
        image_path: 待检测图片路径。

    Returns:
        覆盖率（0~1）；无法解析时返回 0.0。

    Raises:
        无：ffmpeg 不可用或解析失败一律返回 0.0，由调用方降级。
    """
    command = [
        "ffmpeg", "-hide_banner", "-v", "info", "-i", str(image_path),
        "-vf", f"{_KEY_FILTER},alphaextract,signalstats,metadata=print",
        "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        result = _run_ffmpeg(command, timeout=60)
    except RuntimeError:
        return 0.0
    # metadata=print 的输出通道随 ffmpeg 版本/日志级别变化（stdout 或 stderr），
    # 两处都解析，避免因日志路由差异误判为"覆盖率 0"
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", output)
    if match is None:
        return 0.0
    return max(0.0, min(1.0, float(match.group(1)) / 255.0))


def _xy_expr(placement_x: float, placement_y: float | None, anchor: str, safe_bottom: float) -> tuple[str, str]:
    """把相对坐标转换为 overlay 表达式。

    ffmpeg 的 overlay 表达式可访问主画面宽高 ``W/H`` 与前景宽高 ``w/h``，
    因此无需事先知道缩放后的前景尺寸。

    Args:
        placement_x: 前景中心横坐标（0~1）。
        placement_y: 前景中心纵坐标（0~1）；底部对齐时忽略。
        anchor: 垂直锚点（bottom/center）。
        safe_bottom: 底部安全区比例。

    Returns:
        ``(x 表达式, y 表达式)``。

    Raises:
        无。
    """
    x_expr = f"W*{placement_x:.4f}-w/2"
    if anchor == "bottom":
        y_expr = f"H*{1.0 - safe_bottom:.4f}-h"
    else:
        center_y = 0.5 if placement_y is None else placement_y
        y_expr = f"H*{center_y:.4f}-h/2"
    return x_expr, y_expr


class LocalFrameComposer:
    """基于 ffmpeg 的本地合成器（0 积分，CPU 合成）。

    图层顺序固定为：场景基底 → 远角色 → 近角色 → 道具，保证前后遮挡
    关系与叙事一致（近处道具不会被远处角色盖住）。
    """

    def compose(
        self,
        *,
        spec: object,
        setting_path: Path | None,
        layers: list[LayerImage],
    ) -> ComposeResult:
        """合成一张整幅要素画面。

        Args:
            spec: CompositionSpec 实例（鸭子类型读取 canvas/focus/safe_area）。
            setting_path: 场景基底图片路径；None 时用深色底并标记 no_setting。
            layers: 前景图层列表（角色与道具）。

        Returns:
            合成结果（含 PNG 字节与降级标记）。

        Raises:
            RuntimeError: ffmpeg 不可用或合成命令失败。
        """
        canvas = getattr(spec, "canvas")
        width = int(canvas["w"])
        height = int(canvas["h"])
        safe_bottom = float(getattr(spec, "safe_area")["bottom"])
        subjects = list(getattr(spec, "subjects"))
        props = list(getattr(spec, "props"))

        degraded: list[str] = []
        placed: list[str] = []

        # 1. 背景输入：场景图或深色底
        inputs: list[str] = []
        if setting_path is not None and Path(setting_path).exists():
            inputs += ["-i", str(setting_path)]
        else:
            inputs += ["-f", "lavfi", "-i", f"color=c={_FALLBACK_BASE}:s={width}x{height}"]
            degraded.append("no_setting")

        # 2. 前景输入：按 depth 排序（far→mid→near），同深度保持原顺序
        depth_rank = {"far": 0, "mid": 1, "near": 2}
        ordered_layers: list[tuple[LayerImage, object, str]] = []
        for subject in subjects:
            match = next(
                (layer for layer in layers if layer.asset_id == subject.asset_id), None
            )
            if match is not None:
                ordered_layers.append((match, subject, "subject"))
        for prop in props:
            match = next(
                (layer for layer in layers if layer.asset_id == prop.asset_id), None
            )
            if match is not None:
                ordered_layers.append((match, prop, "prop"))
        ordered_layers.sort(key=lambda item: depth_rank.get(str(getattr(item[1], "depth", "mid")), 1))

        if not ordered_layers:
            degraded.append("no_subject")

        filter_parts = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},eq=saturation=0.92:brightness=-0.015,"
            f"gblur=sigma=1.2[bg]"
        ]
        for index, (layer, placement, _kind) in enumerate(ordered_layers, start=1):
            inputs += ["-i", str(layer.path)]
            target_h = int(round(height * float(getattr(placement, "height_ratio", 0.4))))
            target_h = max(2, min(target_h, height * 2))
            coverage = alpha_coverage(Path(layer.path))
            if coverage < _MIN_COVERAGE:
                # 非白底素材：不抠像，改为卡片式置入并显式降级
                filter_parts.append(
                    f"[{index}:v]scale=-1:{target_h},format=rgba,"
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.22:t=10[fg{index}]"
                )
                if "card_layout" not in degraded:
                    degraded.append("card_layout")
            else:
                filter_parts.append(
                    f"[{index}:v]{_KEY_FILTER},scale=-1:{target_h},format=rgba[fg{index}]"
                )
            x_expr, y_expr = _xy_expr(
                float(getattr(placement, "x", 0.5)),
                float(getattr(placement, "y", 0.5)) if _kind == "prop" else None,
                str(getattr(placement, "anchor", "bottom")),
                safe_bottom,
            )
            source = "[bg]" if index == 1 else f"[t{index - 1}]"
            filter_parts.append(
                f"{source}[fg{index}]overlay=x='{x_expr}':y='{y_expr}'[t{index}]"
            )
            placed.append(layer.name)

        last = f"[t{len(ordered_layers)}]" if ordered_layers else "[bg]"
        filter_parts.append(f"{last}vignette=PI/5,format=rgb24[out]")

        with tempfile.TemporaryDirectory(prefix="frame_compose_") as tmp:
            output = Path(tmp) / "frame.png"
            command = ["ffmpeg", "-y", "-v", "error", *inputs,
                       "-filter_complex", ";".join(filter_parts),
                       "-map", "[out]", "-frames:v", "1", str(output)]
            _run_ffmpeg(command)
            if not output.exists():
                raise RuntimeError("合成失败：未生成输出文件")
            png = output.read_bytes()

        return ComposeResult(
            png=png,
            width=width,
            height=height,
            degraded=degraded,
            placed=placed,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_frame_composer.py -q
```
Expected: PASS（5 passed）。若 `test_compose_degrades_to_card_when_key_fails` 未命中降级，检查 `alpha_coverage` 输出是否为 0 并确认 `_MIN_COVERAGE=0.05` 未被改动。

- [ ] **Step 5: 记录变更**

记录：`- [x] Task 3 完成：LocalFrameComposer + 6 项 ffmpeg 集成测试通过`。

---

### Task 4: 合成编排服务（取图层 → 合成 → 落库 → 绑首帧）

**Files:**
- Create: `backend/app/services/shot_frame_service.py`
- Test: `backend/tests/test_shot_frame_service.py`

**Interfaces:**
- Consumes: Task 1 `CompositionSpec`/`AssetRef`/`derive_composition_spec`；Task 3 `LayerImage`/`ComposeResult`/`FrameComposer`
- Produces:
  - `FrameInputs(shot_no, shot_size, camera_move, action_desc, dialogue, aspect_ratio, characters, scene, props, speaker_names)`
  - `build_composition_spec(inputs, *, existing=None, llm_hint=None) -> CompositionSpec`
  - `async compose_shot_frame(session, shot, *, storage, composer, feedback=None, aspect_ratio=None) -> dict`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_shot_frame_service.py`：

```python
"""合成编排服务单测（Fake session / storage / composer，不触碰真实 DB 与 ffmpeg）。

本任务只验证编排：要素是否齐全、spec 是否写入、MediaFile 是否落库、
首帧是否绑定。真实 ffmpeg 合成已由 Task 3 覆盖，HTTP 链路由 Task 10 的
端到端脚本覆盖。
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

pytestmark = pytest.mark.asyncio(loop_scope="session")


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


class FakeSession:
    """最小 async 会话替代实现（仅支持 get/add/flush）。

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
                                       current_version_id=121, description="深色外套"),
        ("Asset", 21): SimpleNamespace(id=21, name="便利店夜内景", asset_type="scene",
                                       current_version_id=211, description="冷白灯管"),
        ("Asset", 31): SimpleNamespace(id=31, name="热豆浆", asset_type="prop",
                                       current_version_id=311, description="纸杯"),
        ("AssetVersion", 111): SimpleNamespace(id=111, media_file_id=1001),
        ("AssetVersion", 121): SimpleNamespace(id=121, media_file_id=1002),
        ("AssetVersion", 211): SimpleNamespace(id=211, media_file_id=1003),
        ("AssetVersion", 311): SimpleNamespace(id=311, media_file_id=1004),
        ("MediaFile", 1001): media(1001, "assets/11.png"),
        ("MediaFile", 1002): media(1002, "assets/12.png"),
        ("MediaFile", 1003): media(1003, "assets/21.png"),
        ("MediaFile", 1004): media(1004, "assets/31.png"),
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
    assert shot.first_frame_media_id == 900
    assert session.flushed >= 1
    assert len(composer.calls) == 1
    assert {layer.name for layer in composer.calls[0]["layers"]} == {"林晚", "陈姨", "热豆浆"}


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
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_service.py -x -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.shot_frame_service'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/services/shot_frame_service.py`：

```python
"""镜头整幅要素画面的编排服务。

职责链：读取镜头绑定的角色/场景/道具资产 → 推导或复用 CompositionSpec
→ 调合成器产出 PNG → 落 MediaFile → 回写 ``shots.first_frame_media_id``。

设计边界：
- 不负责提交事务与操作历史（由调用方 storyboard_service 负责），便于
  在单测中用 FakeSession 验证编排；
- 合成器通过参数注入，模型精修实现只需替换 ``composer``；
- 资产图片缺失时降级为对应图层缺席并记录，而不是整体失败。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.asset import Asset, AssetVersion, MediaFile
from app.models.project import Project
from app.services.frame_composer import ComposeResult, FrameComposer, LayerImage, LocalFrameComposer
from app.services.shot_frame_spec import (
    AssetRef,
    CompositionSpec,
    derive_composition_spec,
    normalize_spec,
)


@dataclass
class FrameInputs:
    """构图推导所需的镜头快照。

    Attributes:
        shot_no: 镜号。
        shot_size: 景别。
        camera_move: 运镜。
        action_desc: 动作/画面描述。
        dialogue: 对白文本。
        aspect_ratio: 项目画幅。
        characters: 角色资产列表。
        scene: 场景资产（可空）。
        props: 道具资产列表。
        speaker_names: 说话人姓名列表。
    """

    shot_no: int
    shot_size: str
    camera_move: str
    action_desc: str | None
    dialogue: str | None
    aspect_ratio: str
    characters: list[AssetRef] = field(default_factory=list)
    scene: AssetRef | None = None
    props: list[AssetRef] = field(default_factory=list)
    speaker_names: list[str] = field(default_factory=list)


def build_composition_spec(
    inputs: FrameInputs,
    *,
    existing: CompositionSpec | None = None,
    llm_hint: dict[str, Any] | None = None,
) -> CompositionSpec:
    """推导或复用镜头构图。

    已有人工/LLM 产物（``source`` 为 manual/llm）时直接复用，避免每次
    合成覆盖人工调整；否则按规则推导。

    Args:
        inputs: 镜头快照。
        existing: 已落库的 spec（可空）。
        llm_hint: LLM 构图提示（可空）。

    Returns:
        合法的 CompositionSpec。

    Raises:
        无：内部推导对任何输入都返回合法结果。
    """
    if existing is not None and existing.source in ("manual", "llm"):
        return existing
    base = derive_composition_spec(
        shot_no=inputs.shot_no,
        shot_size=inputs.shot_size,
        camera_move=inputs.camera_move,
        action_desc=inputs.action_desc,
        dialogue=inputs.dialogue,
        aspect_ratio=inputs.aspect_ratio,
        characters=inputs.characters,
        scene=inputs.scene,
        props=inputs.props,
        speaker_names=inputs.speaker_names,
        llm_hint=llm_hint,
    )
    if existing is not None:
        return normalize_spec(existing.model_dump(), fallback=base)
    return base


async def _resolve_media_key(session: Any, asset_id: int) -> str | None:
    """读取资产当前版本图片的对象键。

    Args:
        session: 数据库会话（含 ``get``）。
        asset_id: 资产 ID。

    Returns:
        图片对象键；资产/版本/媒体缺失或非图片时返回 None。

    Raises:
        无：任何查询异常由调用方兜底为空图层。
    """
    asset = await session.get(Asset, int(asset_id))
    if asset is None or asset.current_version_id is None:
        return None
    version = await session.get(AssetVersion, int(asset.current_version_id))
    if version is None or version.media_file_id is None:
        return None
    media = await session.get(MediaFile, int(version.media_file_id))
    if media is None or not media.object_key:
        return None
    mime = str(getattr(media, "mime_type", "") or "").lower()
    key = str(media.object_key).lower()
    if mime and not mime.startswith("image/") and not key.endswith(
        (".png", ".jpg", ".jpeg", ".webp")
    ):
        return None
    return str(media.object_key)


async def _load_refs(session: Any, shot: Any) -> FrameInputs:
    """读取镜头绑定资产并整理为构图输入。

    Args:
        session: 数据库会话。
        shot: 镜头实例。

    Returns:
        构图推导输入（含每项资产的媒体对象键）。

    Raises:
        无：缺失资产按空处理，不阻断合成。
    """
    characters: list[AssetRef] = []
    for raw in shot.character_asset_ids or []:
        if not str(raw).isdigit():
            continue
        asset = await session.get(Asset, int(raw))
        if asset is None:
            continue
        characters.append(
            AssetRef(
                asset_id=int(asset.id),
                name=str(asset.name or f"角色{asset.id}"),
                role="character",
                media_key=await _resolve_media_key(session, int(asset.id)),
            )
        )

    scene: AssetRef | None = None
    if shot.scene_asset_id:
        asset = await session.get(Asset, int(shot.scene_asset_id))
        if asset is not None:
            scene = AssetRef(
                asset_id=int(asset.id),
                name=str(asset.name or f"场景{asset.id}"),
                role="scene",
                media_key=await _resolve_media_key(session, int(asset.id)),
            )

    props: list[AssetRef] = []
    for raw in shot.prop_asset_ids or []:
        if not str(raw).isdigit():
            continue
        asset = await session.get(Asset, int(raw))
        if asset is None:
            continue
        props.append(
            AssetRef(
                asset_id=int(asset.id),
                name=str(asset.name or f"道具{asset.id}"),
                role="prop",
                media_key=await _resolve_media_key(session, int(asset.id)),
            )
        )

    return FrameInputs(
        shot_no=int(shot.shot_no),
        shot_size=str(shot.shot_size or "medium"),
        camera_move=str(shot.camera_move or "static"),
        action_desc=shot.action_desc,
        dialogue=shot.dialogue,
        aspect_ratio=str(getattr(shot, "aspect_ratio", None) or "9:16"),
        characters=characters,
        scene=scene,
        props=props,
    )


async def compose_shot_frame(
    session: Any,
    shot: Any,
    *,
    storage: Any,
    composer: FrameComposer | None = None,
    feedback: str | None = None,
    aspect_ratio: str | None = None,
) -> dict[str, Any]:
    """合成整幅要素画面并绑定为镜头首帧。

    Args:
        session: 数据库会话（调用方负责 commit）。
        shot: 镜头实例。
        storage: 本地存储客户端（提供 save_bytes/object_path）。
        composer: 合成器（默认 LocalFrameComposer）。
        feedback: 人工重出反馈（追加到 spec 来源说明，暂不改布局）。
        aspect_ratio: 项目画幅（覆盖镜头缺省）。

    Returns:
        ``{shot_id, media_url, object_key, width, height, degraded, placed,
        spec_source, elements}``。

    Raises:
        RuntimeError: 合成器执行失败（由调用方转换为业务错误）。
    """
    inputs = await _load_refs(session, shot)
    if aspect_ratio:
        inputs.aspect_ratio = aspect_ratio
    else:
        # 镜头本身不存画幅，缺省时回读项目设置（16:9 项目不能按 9:16 合成）
        project = await session.get(Project, int(shot.project_id))
        if project is not None and getattr(project, "aspect_ratio", None):
            inputs.aspect_ratio = str(project.aspect_ratio)

    existing = None
    if getattr(shot, "composition_spec", None):
        existing = normalize_spec(
            shot.composition_spec,
            fallback=build_composition_spec(inputs),
        )
    spec = build_composition_spec(inputs, existing=existing)
    if feedback:
        spec = spec.model_copy(update={"source": "manual"})

    layers: list[LayerImage] = []
    for ref in [*inputs.characters, *inputs.props]:
        if not ref.media_key:
            continue
        try:
            path = storage.object_path(ref.media_key)
        except Exception:  # noqa: BLE001 - 存储实现差异，统一按缺失处理
            continue
        if Path(path).exists():
            layers.append(
                LayerImage(
                    asset_id=ref.asset_id,
                    name=ref.name,
                    path=Path(path),
                    kind="character" if ref.role == "character" else "prop",
                )
            )

    setting_path: Path | None = None
    if inputs.scene is not None and inputs.scene.media_key:
        try:
            candidate = storage.object_path(inputs.scene.media_key)
        except Exception:  # noqa: BLE001 - 存储实现差异，统一按缺失处理
            candidate = None
        if candidate is not None and Path(candidate).exists():
            setting_path = Path(candidate)

    active_composer = composer or LocalFrameComposer()
    result: ComposeResult = active_composer.compose(
        spec=spec, setting_path=setting_path, layers=layers
    )
    # 场景缺失属于编排层可知的降级（合成器只知道"没拿到基底"，不知道原因）
    degraded = list(result.degraded)
    if setting_path is None and "no_setting" not in degraded:
        degraded.append("no_setting")

    object_key = storage.save_bytes(
        f"frames/{int(shot.project_id)}/{int(shot.shot_no):02d}_{uuid.uuid4().hex[:12]}.png",
        result.png,
    )
    media = MediaFile(
        project_id=int(shot.project_id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=object_key,
        url=f"/files/{object_key}",
        media_type="image",
        mime_type="image/png",
        file_size_bytes=len(result.png),
        width=result.width,
        height=result.height,
        meta={
            "composition": True,
            "degraded": degraded,
            "spec_source": spec.source,
            "placed": result.placed,
        },
    )
    session.add(media)
    await session.flush()

    shot.first_frame_media_id = int(media.id)
    shot.composition_spec = spec.model_dump()

    return {
        "shot_id": int(shot.id),
        "media_url": f"/files/{object_key}",
        "object_key": object_key,
        "width": result.width,
        "height": result.height,
        "degraded": degraded,
        "placed": result.placed,
        "spec_source": spec.source,
        "elements": {
            "setting": spec.setting,
            "subjects": [item.name for item in spec.subjects],
            "props": [item.name for item in spec.props],
        },
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_service.py -q
```
Expected: PASS（4 passed）

- [ ] **Step 5: 记录变更**

记录：`- [x] Task 4 完成：shot_frame_service 编排 + 4 项单测通过`。

---

### Task 5: 分镜生成接入构图（LLM 道具字段 + 生成时写 spec）

**Files:**
- Modify: `backend/app/services/storyboard_service.py`（`DirectorShot`、`_build_director_prompts`、新增 `_match_prop_assets`、`_run_storyboard_generation` 组装段）
- Test: `backend/tests/test_shot_frame_contracts.py`（新建，与 Task 6 共用）

**Interfaces:**
- Consumes: Task 1 `AssetRef`/`derive_composition_spec`；Task 4 无
- Produces:
  - `DirectorShot.prop_names: list[str]`、`DirectorShot.composition: dict | None`
  - `_match_prop_assets(assets: list[Asset], names: list[str] | None, text_blob: str) -> list[Asset]`
  - `Shot.composition_spec` 在生成时写入（`source="derived"` 或 `"llm"`）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_shot_frame_contracts.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_contracts.py -x -q
```
Expected: FAIL — `ImportError: cannot import name '_match_prop_assets'`

- [ ] **Step 3: 写最小实现**

在 `backend/app/services/storyboard_service.py` 的 `DirectorShot` 类中追加两个字段（放在 `scene_name` 之后）：

```python
    prop_names: list[str] = []          # 参数注释：该镜出现的道具名（项目资产名）
    composition: dict[str, Any] | None = None  # 参数注释：LLM 构图提示（可选覆盖）
```

并在 `DirectorShot` 的 `Attributes` docstring 中补：

```text
        prop_names: 该镜出现的道具名列表（用于精确绑定道具资产）。
        composition: 可选构图提示，含 focus/subjects/props 的位置与归属。
```

在 `_build_director_prompts` 的 `【输出契约】` 字符串中追加字段（保持既有键不变）：

```python
        '"emotion_tag":"tension","dialogue_refs":[2,3],"character_names":["角色名"],'
        '"scene_name":"场景名","prop_names":["道具名"],"composition":{"focus":"character"}}]}'
```

注意末尾必须是 `}}]}`（闭合 composition 对象 → 闭合 shot 对象 → 闭合 shots 数组 → 闭合根对象），少一个括号会导致 LLM 契约 JSON 非法。

并在 `【工作区约定】` 后追加一条规则：

```python
        "9. prop_names 只填项目道具资产名（无则空数组）；composition 可选，给出 "
        "subjects 中角色的 x（0~1）与 facing（left/right/camera/away），道具给 owner。\n"
```

在模块内新增纯函数（放在 `_match_asset` 之后）：

```python
def _match_prop_assets(
    assets: list[Asset],
    names: list[str] | None,
    text_blob: str,
) -> list[Asset]:
    """匹配镜头涉及的道具资产。

    匹配优先级：LLM 明确给出的道具名（精确/包含）→ 文本命中兜底。
    单字名称不参与匹配（避免"纸""杯"这类通用字误命中），非 prop 类型
    一律排除。

    Args:
        assets: 项目资产列表。
        names: LLM 输出的道具名列表（可空）。
        text_blob: 画面描述 + 对白 + 动作文本（用于兜底匹配）。

    Returns:
        命中的道具资产列表（按资产原顺序去重）。

    Raises:
        无：入参结构异常时返回空列表，不阻断分镜生成。
    """
    props = [
        asset
        for asset in assets
        if str(getattr(asset, "asset_type", "")) == "prop"
        and len(str(getattr(asset, "name", "") or "").strip()) >= 2
    ]
    if not props:
        return []

    wanted = [str(name).strip() for name in (names or []) if str(name).strip()]
    matched: list[Asset] = []
    for asset in props:
        name = str(asset.name).strip()
        if any(name == want or name in want or want in name for want in wanted):
            matched.append(asset)
    if matched:
        return matched

    blob = str(text_blob or "")
    if not blob:
        return []
    for asset in props:
        if str(asset.name).strip() in blob:
            matched.append(asset)
    return matched
```

在 `_run_storyboard_generation` 中替换道具绑定段（原 `matched_props` 列表推导）：

```python
                # 道具绑定：优先 LLM 明确给出的 prop_names，文本命中兜底
                matched_props = _match_prop_assets(
                    project_assets, item.prop_names, prop_text_blob
                )
```

并在 `session.add(Shot(...))` 中增加 `composition_spec` 参数（放在 `asset_anchor_ids` 之后）：

```python
                    composition_spec=derive_composition_spec(
                        shot_no=idx + 1,
                        shot_size=shot_size,
                        camera_move=camera_move,
                        action_desc=action_desc or item.description.strip(),
                        dialogue=dialogue,
                        aspect_ratio=str(getattr(project, "aspect_ratio", None) or "9:16"),
                        characters=[
                            AssetRef(asset_id=int(asset.id), name=str(asset.name or ""))
                            for asset in matched_characters
                        ],
                        scene=(
                            AssetRef(
                                asset_id=int(matched_scene.id),
                                name=str(matched_scene.name or ""),
                                role="scene",
                            )
                            if matched_scene is not None
                            else None
                        ),
                        props=[
                            AssetRef(
                                asset_id=int(asset.id),
                                name=str(asset.name or ""),
                                role="prop",
                            )
                            for asset in matched_props
                        ],
                        speaker_names=ordered_names,
                        llm_hint=item.composition,
                    ).model_dump(),
```

在 `storyboard_service.py` 顶部导入区追加：

```python
from app.services.shot_frame_spec import AssetRef, derive_composition_spec
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_contracts.py -q
```
Expected: PASS（8 passed：5 个 DirectorShot + 3 个道具匹配）

再跑既有分镜回归：
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_asset_storyboard.py tests/test_v4_features.py -q
```
Expected: PASS（无新增失败；若 `_build_director_prompts` 断言文本的用例失败，按其断言更新期望字符串）

- [ ] **Step 5: 记录变更**

记录：`- [x] Task 5 完成：DirectorShot 扩展 + 道具匹配纯函数 + 生成时写 composition_spec`。

---

### Task 6: 重出双模式、批量合成接口、`shot_out` 扩展

**Files:**
- Modify: `backend/app/services/storyboard_service.py`（`redraw_shot_frame`、新增 `compose_storyboard_frames`）
- Modify: `backend/app/schemas/storyboard.py`（`shot_out`、`FrameRedrawRequest`、新增 `FrameComposeRequest`）
- Modify: `backend/app/api/storyboards.py`（redraw 传 mode、新增批量与 QC 路由）
- Test: `backend/tests/test_shot_frame_contracts.py`（追加用例）

**Interfaces:**
- Consumes: Task 4 `compose_shot_frame`；Task 8 `evaluate_frames`（QC 路由在 Task 8 之后接线）
- Produces:
  - `redraw_shot_frame(session, shot, feedback, user_id, mode="compose") -> dict`
  - `async compose_storyboard_frames(session, storyboard, shot_ids, user_id, *, storage=None) -> dict`
  - `shot_out` 增加 `composition_spec` 与 `frame_elements`
  - `FrameRedrawRequest.mode: Literal["compose","generate"]`
  - `FrameComposeRequest(shot_ids: list[int] | None)`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_shot_frame_contracts.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_contracts.py -q
```
Expected: FAIL — `AssertionError`（`composition_spec` KeyError / `mode` 属性不存在）

- [ ] **Step 3: 写最小实现**

在 `backend/app/schemas/storyboard.py` 的 `FrameRedrawRequest` 中追加字段：

```python
    mode: Literal["compose", "generate"] = Field(
        default="compose",
        description="compose=本地合成整幅要素帧（0 成本）；generate=文生图（≤3 次）",
    )
```

并在该文件导入区确认已导入 `Literal`（若无则补 `from typing import Any, Literal`）。

在 `shot_out` 中插入两段（放在 `redraft_count` 之后）：

```python
        "composition_spec": shot.composition_spec,
        "frame_elements": _frame_elements(shot.composition_spec),
```

并在 `shot_out` 之前新增内部函数：

```python
def _frame_elements(spec: dict | None) -> dict[str, Any] | None:
    """把构图 spec 精简为前端展示用的要素清单。

    Args:
        spec: CompositionSpec 字典（可空）。

    Returns:
        ``{setting, subjects, props, focus, degraded}``；无 spec 时返回 None。
    """
    if not isinstance(spec, dict):
        return None
    setting = spec.get("setting") or {}
    subjects = [
        str(item.get("name"))
        for item in (spec.get("subjects") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    props = [
        str(item.get("name"))
        for item in (spec.get("props") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {
        "setting": {"name": setting.get("name"), "present": bool(setting.get("name"))},
        "subjects": subjects,
        "props": props,
        "focus": spec.get("focus"),
        "degraded": list(spec.get("degraded") or []),
    }
```

新增批量请求体（放在 `FrameRedrawRequest` 之后）：

```python
class FrameComposeRequest(BaseModel):
    """整板批量合成首帧请求体。

    Attributes:
        shot_ids: 指定镜头 ID 列表；为空表示整板全部镜头。
    """

    model_config = ConfigDict(extra="forbid")

    shot_ids: list[int] | None = Field(default=None, description="指定镜头；空=整板")
```

在 `backend/app/services/storyboard_service.py` 中改写 `redraw_shot_frame` 的签名与分支（保留原 generate 逻辑）：

```python
async def redraw_shot_frame(
    session: AsyncSession,
    shot: Shot,
    feedback: str | None,
    user_id: int,
    mode: str = "compose",
) -> dict[str, Any]:
    """静帧重出：默认本地合成整幅要素帧，可选文生图。

    Args:
        session: 请求级数据库会话。
        shot: 镜头 ORM 实例。
        feedback: 重出反馈（≤50 字）。
        user_id: 操作人用户 ID。
        mode: ``compose`` 走本地合成（0 成本、不消耗重出次数）；
            ``generate`` 走文生图（每镜 ≤3 次）。

    Returns:
        ``{shot_id, mode, redraft_count, remaining, media_url, degraded}``。

    Raises:
        BizError: 60004 重出次数超限（仅 generate）；80001 模型不可用。
    """
    from app.infra.storage import storage as default_storage
    from app.services import shot_frame_service

    if mode == "compose":
        result = await shot_frame_service.compose_shot_frame(
            session,
            shot,
            storage=default_storage,
            feedback=feedback,
        )
        await record_op(
            session,
            storyboard_id=int(shot.storyboard_id),
            user_id=user_id,
            op_type="redraw",
            target_shot_ids=[int(shot.id)],
            after={
                "mode": "compose",
                "feedback": feedback,
                "media_url": result["media_url"],
                "degraded": result["degraded"],
            },
        )
        await session.commit()
        return {
            "shot_id": int(shot.id),
            "mode": "compose",
            "redraft_count": int(shot.redraft_count or 0),
            "remaining": max(0, MAX_REDRAFT_COUNT - int(shot.redraft_count or 0)),
            "media_url": result["media_url"],
            "degraded": result["degraded"],
            "elements": result["elements"],
        }

    # ---- generate：沿用文生图与原重出次数限制 ----
    # 以下三个局部导入沿用原实现（延迟导入避免模块级循环依赖），改写时不可删除
    from app.infra.model_gateway.base import GenRequest
    from app.infra.model_gateway.gateway import model_gateway
    from app.models.asset import MediaFile

    if int(shot.redraft_count or 0) >= MAX_REDRAFT_COUNT:
        raise BizError(
            ErrorCode.RETRY_LIMIT_EXCEEDED,
            f"该镜头静帧已重出 {MAX_REDRAFT_COUNT} 次，建议修改 prompt 后重新拆解",
            data={"redraft_count": int(shot.redraft_count or 0), "limit": MAX_REDRAFT_COUNT},
        )
    prompt = shot.final_prompt or shot.image_prompt or shot.action_desc or "画面"
    if feedback:
        prompt = f"{prompt}。调整要求：{feedback}"
    result = await model_gateway.generate(
        "image",
        "text_to_image",
        GenRequest(
            capability="image",
            prompt=prompt,
            params={"size": "720x1280", "shot_no": int(shot.shot_no)},
        ),
        project_id=int(shot.project_id),
    )
    if not result.ok:
        raise BizError(ErrorCode.MODEL_UNAVAILABLE, (result.error or "静帧重出失败")[:200])
    media = MediaFile(
        project_id=int(shot.project_id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=result.object_key,
        url=result.media_url,
        media_type="image",
        mime_type=result.meta.get("mime_type"),
        width=result.meta.get("width"),
        height=result.meta.get("height"),
        file_size_bytes=int(result.meta.get("file_size_bytes") or 0),
        meta={"simulated": result.meta.get("simulated"), "redraft": True},
    )
    session.add(media)
    await session.flush()
    shot.first_frame_media_id = int(media.id)
    shot.redraft_count = int(shot.redraft_count or 0) + 1
    await record_op(
        session,
        storyboard_id=int(shot.storyboard_id),
        user_id=user_id,
        op_type="redraw",
        target_shot_ids=[int(shot.id)],
        after={"mode": "generate", "feedback": feedback, "media_url": result.media_url},
    )
    await session.commit()
    return {
        "shot_id": int(shot.id),
        "mode": "generate",
        "redraft_count": int(shot.redraft_count),
        "remaining": max(0, MAX_REDRAFT_COUNT - int(shot.redraft_count)),
        "media_url": result.media_url,
        "degraded": [],
        "elements": None,
    }
```

在 `storyboard_service.py` 末尾新增批量编排（放在 `redraw_shot_frame` 之后）：

```python
async def compose_storyboard_frames(
    session: AsyncSession,
    storyboard: Storyboard,
    shot_ids: list[int] | None,
    user_id: int,
) -> dict[str, Any]:
    """为整板（或指定镜头）批量合成整幅要素首帧。

    单镜失败不阻断整批：逐镜返回状态，前端据此高亮失败镜。

    Args:
        session: 请求级数据库会话。
        storyboard: 分镜板实例。
        shot_ids: 指定镜头 ID；为空表示整板。
        user_id: 操作人用户 ID。

    Returns:
        ``{total, succeeded, failed, results}``，results 每项含
        ``{shot_id, shot_no, status, media_url, degraded, error}``。

    Raises:
        无：单镜异常被捕获并记入 results。
    """
    from app.infra.storage import storage as default_storage
    from app.services import shot_frame_service

    rows = await StoryboardRepository.list_shots(session, int(storyboard.id))
    wanted = {int(item) for item in (shot_ids or [])}
    targets = [shot for shot in rows if not wanted or int(shot.id) in wanted]

    results: list[dict[str, Any]] = []
    succeeded = 0
    for shot in targets:
        try:
            outcome = await shot_frame_service.compose_shot_frame(
                session, shot, storage=default_storage
            )
            succeeded += 1
            results.append(
                {
                    "shot_id": int(shot.id),
                    "shot_no": int(shot.shot_no),
                    "status": "ok",
                    "media_url": outcome["media_url"],
                    "degraded": outcome["degraded"],
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单镜失败不应中断整批
            results.append(
                {
                    "shot_id": int(shot.id),
                    "shot_no": int(shot.shot_no),
                    "status": "failed",
                    "media_url": None,
                    "degraded": [],
                    "error": str(exc)[:200],
                }
            )
    await record_op(
        session,
        storyboard_id=int(storyboard.id),
        user_id=user_id,
        op_type="batch_edit",
        target_shot_ids=[int(item["shot_id"]) for item in results],
        after={"action": "compose_frames", "succeeded": succeeded},
    )
    await session.commit()
    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }
```

在 `backend/app/api/storyboards.py` 的 redraw 路由中传入 mode：

```python
    data = await storyboard_service.redraw_shot_frame(
        session, shot, payload.feedback, int(user_id), payload.mode
    )
```

并在该文件中新增批量路由（放在 redraw 路由之后）：

```python
@router.post("/storyboards/{storyboard_id}/frames/compose")
async def post_storyboard_frames_compose(
    storyboard_id: int,
    payload: FrameComposeRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """批量合成整板整幅要素首帧（0 成本，本地 ffmpeg）。

    Args:
        storyboard_id: 分镜板 ID。
        payload: 请求体（可选指定镜头）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {total, succeeded, failed, results}。

    Raises:
        BizError: 20003 无权限；10002 分镜板不存在。
    """
    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "edit")
    data = await storyboard_service.compose_storyboard_frames(
        session, storyboard, payload.shot_ids, int(user_id)
    )
    return success_envelope(data, request_id)
```

在 `storyboards.py` 的 schema 导入列表中补 `FrameComposeRequest`。

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_shot_frame_contracts.py -q
```
Expected: PASS（11 passed）

再跑分镜与渲染回归：
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_asset_storyboard.py tests/test_v4_features.py tests/test_render.py -q
```
Expected: PASS（无新增失败）

- [ ] **Step 5: 记录变更**

记录：`- [x] Task 6 完成：redraw 双模式 + 批量合成接口 + shot_out 要素清单`。

---

### Task 7: 渲染联动（首帧进入幂等哈希 + 管线版本 v5）

**Files:**
- Modify: `backend/app/services/render_service.py`（`RENDER_PIPELINE_VERSION`、`build_input_hash`、调用处）
- Test: `backend/tests/test_render_hash.py`（新建）

**Interfaces:**
- Consumes: `shot.first_frame_media_id`（Task 4 写入）
- Produces:
  - `build_input_hash(prompt, duration_ms, anchor_ids, voice_clip_ids=None, anchor_pack_hash=None, aspect_ratio=None, frame_media_id=None) -> str`
  - `RENDER_PIPELINE_VERSION = "image-driven-v5-20261027"`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_render_hash.py`：

```python
"""渲染入参哈希单测：首帧必须影响幂等键。

背景：合成换帧后若哈希不变，任务会被"幂等复用"到旧视频，表现为
"改了首帧但成片没变"。这里锁定首帧参与摘要这一行为。
"""

from __future__ import annotations

import pytest

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


def test_pipeline_version_is_v5() -> None:
    """管线版本升到 v5，保证历史产物不被误复用。"""
    assert RENDER_PIPELINE_VERSION.startswith("image-driven-v5-")
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_render_hash.py -q
```
Expected: FAIL — `TypeError: build_input_hash() got an unexpected keyword argument 'frame_media_id'`

- [ ] **Step 3: 写最小实现**

在 `backend/app/services/render_service.py` 中：

1) 版本号改为：

```python
RENDER_PIPELINE_VERSION: str = "image-driven-v5-20261027"
```

2) `build_input_hash` 增加参数并纳入摘要：

```python
def build_input_hash(
    prompt: str | None,
    duration_ms: int,
    anchor_ids: list[int] | None,
    voice_clip_ids: list[int] | None = None,
    anchor_pack_hash: str | None = None,
    aspect_ratio: str | None = None,
    frame_media_id: int | None = None,
) -> str:
    """构造渲染任务入参哈希。

    摘要覆盖 ``prompt | duration | 锚点IDs | 配音片段IDs | 锚点包哈希 |
    画幅 | 首帧媒体ID | 渲染管线版本``。

    首帧参与摘要的原因：整幅要素画面由本地合成产出，换帧后若哈希不变，
    任务会被幂等复用旧产物，表现为"改了首帧但成片没变"。

    Args:
        prompt: 最终组装 prompt（可空按空串参与摘要）。
        duration_ms: 镜头时长（毫秒）。
        anchor_ids: 锚点资产版本 ID 列表（排序后参与摘要）。
        voice_clip_ids: F5.13 音画对齐输入——镜头绑定的最新配音片段 ID。
        anchor_pack_hash: F3.13 多维锚点包引用摘要（缺包时为空）。
        aspect_ratio: 项目画幅（9:16/16:9/1:1，可空）。
        frame_media_id: 镜头首帧媒体 ID（可空）。

    Returns:
        64 位十六进制哈希。
    """
    anchors = ",".join(str(a) for a in sorted(anchor_ids or []))
    voices = ",".join(str(v) for v in sorted(voice_clip_ids or []))
    pack = anchor_pack_hash or ""
    ratio = aspect_ratio or ""
    frame = "" if frame_media_id is None else str(int(frame_media_id))
    return sha256_hash(
        f"{prompt or ''}|{duration_ms}|{anchors}|{voices}|{pack}|{ratio}|{frame}"
        f"|{RENDER_PIPELINE_VERSION}"
    )
```

3) 调用处（约第 477 行）追加首帧参数：

```python
        ih = build_input_hash(
            shot.final_prompt,
            int(shot.duration_ms),
            anchor_ids,
            clip_ids,
            str(pack.get("pack_hash") or ""),
            str(project.aspect_ratio or ""),
            int(shot.first_frame_media_id) if shot.first_frame_media_id else None,
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_render_hash.py -q
```
Expected: PASS（4 passed）

Run 回归：
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_render.py -q
```
Expected: PASS

- [ ] **Step 5: 记录变更**

记录：`- [x] Task 7 完成：build_input_hash 纳入首帧 + 管线版本 v5`。

---

### Task 8: 导演 QC 门禁

**Files:**
- Create: `backend/app/services/frame_qc_service.py`
- Test: `backend/tests/test_frame_qc_service.py`（新建）
- Modify: `backend/app/api/storyboards.py`（新增 `GET /storyboards/{id}/frames/qc`）

**Interfaces:**
- Consumes: `CompositionSpec`（Task 1）
- Produces:
  - `FrameQcItem(shot_id, shot_no, shot_size, spec, frame_media_id, degraded, speakers)`
  - `evaluate_frames(items: list[FrameQcItem]) -> dict`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_frame_qc_service.py`：

```python
"""导演 QC 门禁单测（纯函数）。

四项门禁：要素齐备率、相邻重复帧、景别多样性、台词主体匹配；
外加降级占比告警。测试直接构造 FrameQcItem，不依赖数据库。
"""

from __future__ import annotations

from app.services.frame_qc_service import FrameQcItem, evaluate_frames
from app.services.shot_frame_spec import (
    AssetRef,
    CompositionSpec,
    SubjectPlacement,
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
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_frame_qc_service.py -x -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.frame_qc_service'`

- [ ] **Step 3: 写最小实现**

创建 `backend/app/services/frame_qc_service.py`：

```python
"""导演视角的画面 QC 门禁（纯函数）。

与既有 ``_qc_check``（伪向量人脸一致性）互补：本模块检查"这一镜有没有
把人、景、物讲清楚"，直接对应审核标准 S1–S4：
- 要素齐备率（S2）：场景 + 至少一个主体 + 应出现的道具；
- 相邻重复帧（S1）：相邻镜不得共用同一首帧或同一来源资产；
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
    """

    shot_id: int
    shot_no: int
    shot_size: str
    spec: CompositionSpec | None
    frame_media_id: int | None
    degraded: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)


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
                "message": f"降级镜头占比 {degraded_ratio:.0%}（告警线 {MAX_DEGRADED_RATIO:.0%}），素材或抠像质量不足",
                "severity": "warning",
            }
        )

    blocking = any(issue["severity"] == "blocking" for issue in issues)
    passed = (
        not blocking
        and elements_ratio >= MIN_ELEMENTS_RATIO
        and not duplicates
        and variety >= MIN_SHOT_SIZE_VARIETY
        and speaker_ratio >= MIN_SPEAKER_MATCH_RATIO
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
```

在 `backend/app/api/storyboards.py` 新增 QC 路由（放在批量合成路由之后）：

```python
@router.get("/storyboards/{storyboard_id}/frames/qc")
async def get_storyboard_frames_qc(
    storyboard_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """获取整板画面 QC 门禁结果（对视听叙事表达的可测检查）。

    Args:
        storyboard_id: 分镜板 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {passed, checks, issues, items}。

    Raises:
        BizError: 20003 无权限；10002 分镜板不存在。
    """
    from app.services import frame_qc_service
    from app.services.shot_frame_spec import CompositionSpec, normalize_spec
    from app.models.script import ScriptLine
    from app.repositories.storyboard_repo import StoryboardRepository

    storyboard = await _storyboard_guard(storyboard_id, user_id, session, "view")
    rows = await StoryboardRepository.list_shots(session, int(storyboard.id))
    items: list[frame_qc_service.FrameQcItem] = []
    for shot in rows:
        fallback = CompositionSpec(
            canvas={"w": 720, "h": 1280},
            shot_size=str(shot.shot_size or "medium"),
            safe_area={"top": 0.08, "bottom": 0.18},
        )
        spec = (
            normalize_spec(shot.composition_spec, fallback=fallback)
            if shot.composition_spec
            else None
        )
        speakers: list[str] = []
        text = str(shot.dialogue or "").strip()
        if text:
            lines = (
                await session.execute(
                    select(ScriptLine).where(
                        ScriptLine.project_id == int(shot.project_id),
                        ScriptLine.is_deleted.is_(False),
                        ScriptLine.line_type.in_(("dialogue", "narration")),
                    )
                )
            ).scalars().all()
            for line in lines:
                content = str(line.content or "").strip()
                if content and content in text and line.speaker_name:
                    name = str(line.speaker_name).strip()
                    if name and name not in speakers:
                        speakers.append(name)
        items.append(
            frame_qc_service.FrameQcItem(
                shot_id=int(shot.id),
                shot_no=int(shot.shot_no),
                shot_size=str(shot.shot_size or "medium"),
                spec=spec,
                frame_media_id=int(shot.first_frame_media_id) if shot.first_frame_media_id else None,
                degraded=list(
                    (shot.composition_spec or {}).get("degraded")
                    if isinstance(shot.composition_spec, dict)
                    else []
                ),
                speakers=speakers,
            )
        )
    report = frame_qc_service.evaluate_frames(items)
    report["items"] = [
        {"shot_id": item.shot_id, "shot_no": item.shot_no, "frame_media_id": item.frame_media_id}
        for item in items
    ]
    return success_envelope(report, request_id)
```

在 `storyboards.py` 顶部导入区补 `from sqlalchemy import select`（若尚未导入）。

- [ ] **Step 4: 运行测试确认通过**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -m pytest tests/test_frame_qc_service.py -q
```
Expected: PASS（6 passed）

Run 语法与导入回归：
```powershell
cd "e:\FeiSi\Project code\gleam-dream\backend"; .venv\Scripts\python -c "import app.main; print('ok')"
```
Expected: 输出 `ok`

- [ ] **Step 5: 记录变更**

记录：`- [x] Task 8 完成：frame_qc_service 五检 + QC 路由`。

---

### Task 9: 前端（要素徽标、双模式重出、批量合成）

**Files:**
- Modify: `frontend/src/api/v4.ts`
- Modify: `frontend/src/components/v4/StoryboardV4Panel.vue`

**Interfaces:**
- Consumes: Task 6/8 后端接口
- Produces: `composeStoryboardFrames(storyboardId, shotIds?)`、`getStoryboardFramesQc(storyboardId)`、`redrawShotFrame(shotId, feedback, mode)`

- [ ] **Step 1: 扩展 API 客户端**

在 `frontend/src/api/v4.ts` 中替换 `redrawShotFrame`：

```typescript
/**
 * 重出镜头首帧（默认本地合成整幅要素画面）。
 * @param shotId 镜头 ID
 * @param feedback 反馈（≤50 字）
 * @param mode compose=本地合成（0 成本）；generate=文生图（≤3 次）
 * @returns 重出结果
 */
export function redrawShotFrame(
  shotId: string | number,
  feedback?: string,
  mode: 'compose' | 'generate' = 'compose',
): Promise<{
  shot_id: number
  mode: 'compose' | 'generate'
  redraft_count: number
  remaining: number
  media_url: string | null
  degraded: string[]
  elements: {
    setting: { name?: string | null; present: boolean } | null
    subjects: string[]
    props: string[]
  } | null
  suggest_change_prompt?: boolean
}> {
  return http.patch(`/shots/${shotId}/frame/redraw`, {
    feedback: feedback ?? null,
    mode,
  })
}

/**
 * 批量合成整板整幅要素首帧。
 * @param storyboardId 分镜板 ID
 * @param shotIds 指定镜头（空=整板）
 * @returns 批量结果
 */
export function composeStoryboardFrames(
  storyboardId: string | number,
  shotIds?: number[],
): Promise<{
  total: number
  succeeded: number
  failed: number
  results: Array<{
    shot_id: number
    shot_no: number
    status: 'ok' | 'failed'
    media_url: string | null
    degraded: string[]
    error: string | null
  }>
}> {
  return http.post(`/storyboards/${storyboardId}/frames/compose`, {
    shot_ids: shotIds ?? null,
  })
}

/**
 * 获取整板画面 QC 门禁结果。
 * @param storyboardId 分镜板 ID
 * @returns 门禁结果
 */
export function getStoryboardFramesQc(storyboardId: string | number): Promise<{
  passed: boolean
  checks: Record<string, { ratio?: number; value?: number; threshold: number; shots?: number[] }>
  issues: Array<{ code: string; shot_no: number | null; message: string; severity: string }>
  items: Array<{ shot_id: number; shot_no: number; frame_media_id: number | null }>
}> {
  return http.get(`/storyboards/${storyboardId}/frames/qc`)
}
```

- [ ] **Step 2: 前端面板接线**

在 `frontend/src/components/v4/StoryboardV4Panel.vue` 中，把 `composeStoryboardFrames, getStoryboardFramesQc` 追加到文件顶部**已有的** `from '../../api/v4'` 具名导入里（不要新增第二条 import，否则触发重复导入检查）。该文件当前已有 `redrawShotFrame` 在同一条导入中，改后形如：

```typescript
import {
  composeStoryboardFrames,
  getStoryboardFramesQc,
  redrawShotFrame,
  // ...既有其它导入保持原样
} from '../../api/v4'
```

同时把既有 `redraw` 函数里的调用改为显式文生图，避免默认模式改成 compose 后行为漂移：

```typescript
    const result = await redrawShotFrame(shotId, undefined, 'generate')
```

在 `redraw` 函数之后新增：

```typescript
/**
 * 重出整幅要素画面（本地合成，0 成本）。
 * @param shotId 镜头 ID
 * @returns 无
 */
async function composeFrame(shotId: number): Promise<void> {
  submitting.value = true
  try {
    const result = await redrawShotFrame(shotId, undefined, 'compose')
    const degraded = result.degraded?.length ? `（降级：${result.degraded.join('、')}）` : ''
    toast.success(`整幅画面已合成${degraded}`)
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 批量合成整板首帧并展示 QC 门禁结果。
 * @returns 无
 */
async function composeAllFrames(): Promise<void> {
  submitting.value = true
  try {
    const result = await composeStoryboardFrames(props.storyboardId)
    const qc = await getStoryboardFramesQc(props.storyboardId)
    toast.success(`已合成 ${result.succeeded}/${result.total} 镜`)
    if (!qc.passed) {
      const first = qc.issues[0]
      toast.warning(first ? `QC 未通过：${first.message}` : 'QC 未通过')
    }
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}
```

在模板"镜头快捷操作"区块中，替换「重出静帧」按钮并追加批量按钮：

```vue
        <AppButton
          v-for="shotId in shotIds.slice(0, 8)"
          :key="`compose-${shotId}`"
          variant="subtle"
          size="sm"
          :loading="submitting"
          @click="composeFrame(shotId)"
        >
          重出整幅画面 #{{ shotId }}
        </AppButton>
        <AppButton variant="ghost" size="sm" :loading="submitting" @click="composeAllFrames">
          批量合成整板首帧
        </AppButton>
```

同时保留原「纯生成」入口（改为调用 `redraw(shotId)`，仍走文生图）：

```vue
        <AppButton
          v-for="shotId in shotIds.slice(0, 8)"
          :key="`redraw-${shotId}`"
          variant="ghost"
          size="sm"
          :loading="submitting"
          @click="redraw(shotId)"
        >
          纯生成 #{{ shotId }}
        </AppButton>
```

- [ ] **Step 3: 类型检查**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream\frontend"; npm run typecheck
```
Expected: 无错误输出（exit 0）。若报 `Cannot find name 'composeStoryboardFrames'`，检查 Step 2 的导入路径是否为相对路径 `../../api/v4`。

- [ ] **Step 4: 记录变更**

记录：`- [x] Task 9 完成：前端要素徽标入口 + 批量合成 + typecheck 通过`。

---

### Task 10: 845 端到端复测与验收报告

**Files:**
- Create: `测试报告/scripts/stage8_compose_frames.py`
- Create: `测试报告/深夜的暖_分镜整幅画面复测_20261027.md`

**Interfaces:**
- Consumes: Task 6 批量合成接口、Task 8 QC 接口、既有渲染/合成/导出链路
- Produces: 前后对比帧、S1–S5 评分表、AC-1~AC-7 结论

- [ ] **Step 1: 写复测脚本**

创建 `测试报告/scripts/stage8_compose_frames.py`：

```python
"""845「深夜的暖」整幅要素画面端到端复测脚本。

流程：批量合成整板首帧 → 拉取 QC 门禁 → 输出逐镜要素与降级清单 →
（可选）触发重渲染并导出成片。所有请求走本地 8100 端口，复用
``测试报告/scripts/.token`` 的离线签发令牌。

运行：
    python 测试报告/scripts/stage8_compose_frames.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8100"
STORYBOARD_ID = 203
PROJECT_ID = 845
TOKEN_PATH = Path(__file__).with_name(".token")


def _client() -> httpx.Client:
    """构造带鉴权的客户端。

    Returns:
        httpx 客户端。

    Raises:
        SystemExit: 令牌文件缺失。
    """
    if not TOKEN_PATH.exists():
        sys.exit(f"缺少令牌文件：{TOKEN_PATH}")
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    return httpx.Client(
        base_url=BASE, timeout=300.0, headers={"Authorization": f"Bearer {token}"}
    )


def main() -> None:
    """执行批量合成与 QC 并打印结果。

    Returns:
        无。

    Raises:
        SystemExit: 合成或 QC 请求失败。
    """
    with _client() as client:
        compose = client.post(
            f"/storyboards/{STORYBOARD_ID}/frames/compose", json={"shot_ids": None}
        )
        if compose.status_code != 200:
            sys.exit(f"批量合成失败 {compose.status_code}: {compose.text[:500]}")
        payload = compose.json()["data"]
        print(json.dumps({"compose": payload}, ensure_ascii=False, indent=2))

        qc = client.get(f"/storyboards/{STORYBOARD_ID}/frames/qc")
        if qc.status_code != 200:
            sys.exit(f"QC 请求失败 {qc.status_code}: {qc.text[:500]}")
        report = qc.json()["data"]
        print(json.dumps({"qc": report}, ensure_ascii=False, indent=2))

        failed = [item for item in payload["results"] if item["status"] != "ok"]
        if failed:
            sys.exit(f"存在合成失败镜头：{failed}")

        out = Path(__file__).parents[1] / "artifacts" / "frames_compose_845.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"compose": payload, "qc": report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 执行合成与 QC**

前置：后端 8100 已运行（若未运行，先在 `backend` 下启动 uvicorn）。

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream"; .venv\Scripts\python 测试报告\scripts\stage8_compose_frames.py
```
若仓库根无 `.venv`，改用 `backend\.venv\Scripts\python`。

Expected: 输出 `compose.succeeded == 10`（或整板实际镜数），`qc.passed` 为 `true` 或仅含 warning 级 issues。

- [ ] **Step 3: 抽帧目视复核**

Run:
```powershell
cd "e:\FeiSi\Project code\gleam-dream"; ffmpeg -y -v error -i "backend\storage\frames\845\{合成文件名}.png" -vf "scale=360:-1" "测试报告\artifacts\compose_check.png"
```
对 10 张合成帧逐一目视，确认：场景可见、角色落在合理位置、道具在画面内、无白边毛刺。

- [ ] **Step 4: 重渲染并合成成片**

Run（复用既有渲染与合成脚本；若脚本名不同，用 `测试报告\scripts\` 下 stage4/stage5 对应脚本）：
```powershell
cd "e:\FeiSi\Project code\gleam-dream"; .venv\Scripts\python 测试报告\scripts\stage4b_rerender.py
```
Expected: 渲染任务 `image_driven=true`，产物画面为合成帧而非白墙定妆照；随后执行既有 `stage5` 字幕与 `stage6` 合成、`stage7` 导出。

- [ ] **Step 5: 写复测报告**

创建 `测试报告/深夜的暖_分镜整幅画面复测_20261027.md`，必须包含：

1. 复测环境（项目 845、分镜板 203、管线 `image-driven-v5-20261027`、0 积分）；
2. 逐镜表格：镜号 / 场景 / 角色 / 道具 / 降级 / 首帧 URL；
3. S1–S5 前后对比得分表（前值取 `深夜的暖_全链路测试报告_20261027.md`：4/0/5/3/通过）；
4. AC-1~AC-7 逐项结论与证据（接口返回 JSON 片段 + 抽帧文件名）；
5. 遗留问题与下一步（如需模型精修的关键镜清单）。

- [ ] **Step 6: 记录变更**

记录：`- [x] Task 10 完成：845 整板合成 succeeded=10、QC 通过、复测报告已出`。

---

## Self-Review

**1. Spec coverage**

| Spec 章节 | 落点 |
| --- | --- |
| §3.1 `shot_frame_spec` 契约与推导 | Task 1 |
| §3.1 `frame_composer`（本地 + 预留模型） | Task 3（`FrameComposer` Protocol 即预留接口） |
| §3.1 `shot_frame_service` | Task 4 |
| §3.1 `frame_qc_service` | Task 8 |
| §3.2 `DirectorShot` 扩展 + 提示词 | Task 5 |
| §3.2 生成时写 spec | Task 5 |
| §3.2 redraw 双模式 | Task 6 |
| §3.2 `shot_out` 要素 | Task 6 |
| §3.2 `build_input_hash` + v5 | Task 7 |
| §3.2 前端 | Task 9 |
| §4.1 `composition_spec` JSONB + 迁移 | Task 2 |
| §5 合成算法与兜底矩阵 | Task 3（`no_setting`/`card_layout`/`no_subject`）+ Task 4（缺图层跳过） |
| §7 接口 | Task 6（redraw/批量）+ Task 8（QC） |
| §8 错误处理 | Task 3（RuntimeError）+ Task 6（逐镜 failed）+ Task 10（阻断提示） |
| §9 门禁阈值 | Task 8 |
| §10 测试策略 | Task 1/2/3/4/5/6/7/8 单测与集成 + Task 10 端到端 |
| §11 AC-1~AC-7 | Task 10 报告逐项核验 |
| §13 批次 B1–B6 | 依次对应 Task 1+2 / 3 / 4 / 5 / 6+7 / 8+9+10 |

**2. Placeholder scan**

已检查：无 `TBD`/`TODO`/「类似 Task N」；每个代码步骤均给出完整实现或精确的插入位置。

**3. Type consistency**

- `CompositionSpec.canvas` 恒为 `dict[str, int]`（`{"w","h"}`），Task 3/4/6/8 一致。
- `ComposeResult` 字段 `png/width/height/degraded/placed` 在 Task 3 定义、Task 4 消费，一致。
- `FrameQcItem` 字段 `shot_id/shot_no/shot_size/spec/frame_media_id/degraded/speakers` 在 Task 8 定义与 API 构造处一致。
- `redraw_shot_frame` 返回值新增 `mode/degraded/elements`，前端 Task 9 类型与之对齐。
- `build_input_hash` 末位新增 `frame_media_id`，Task 7 测试与调用处参数顺序一致（按关键字传入）。

**4. 已知取舍**

- Task 6 未给 `redraw` compose 分支的自动化测试：该分支依赖 `record_op`（查询 `storyboard_ops`）与提交，FakeSession 无法低成本覆盖；改由 Task 10 端到端与 Task 4 的编排测试共同保障。
- `frame_qc_service` 的「空镜无角色」判定为 warning 而非 blocking，避免误伤建立镜头。
