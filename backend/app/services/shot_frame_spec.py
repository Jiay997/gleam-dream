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

#: 景别 → 角色屏幕高度占比（保证"特写大、全景小"的直觉）。
#: v7 恢复设计稿口径：特写/近景按包围盒高度可超过画布（裁切上下），
#: 中景人物占 65%，避免方形定妆照被宽度上限压成 25% 屏高的小图。
_SHOT_HEIGHT_RATIO: dict[str, float] = {
    "extreme_close_up": 1.10,
    "close_up": 0.95,
    "over_shoulder": 0.80,
    "medium": 0.65,
    "wide": 0.38,
    "extreme_wide": 0.22,
}

#: 只保留说话人单人的景别（纯特写，避免双人贴纸感）。
#: 过肩镜头必须同时出现「说话人 + 前景听者」，否则反打关系无法成立。
_SINGLE_SUBJECT_SHOT_SIZES = frozenset({"close_up", "extreme_close_up"})

#: 双人布局的中心横坐标（左=主位/说话人，右=次位/听者；间距 ≥ 0.30 画布宽）
_DUAL_X = (0.28, 0.72)

#: 构图规则版本：规则变化时递增，历史 spec（缺字段默认 1）在合成时自动重算，
#: 避免"代码修了但旧 spec 继续复用旧比例/旧锚点"。
#: v8：新增坐姿适配（sitting）——坐/蹲/趴/躺动作只合成上半身，避免"站着演坐姿"。
#: v9：新增"桌前动作"坐姿识别——动作行常省略"坐"字（如"双手压着纸沿桌面
#:     推过去""撑上桌面"），但人物实际坐在桌边；这类镜头按坐姿裁上半身，
#:     否则全身站姿贴到桌前会与剧情动作冲突、背景也被人物挡满。
LAYOUT_VERSION = 9

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
        height_ratio: 身高占画布高度的比例（>1 表示裁切上下）。
        x: 角色中心横坐标（0~1）。
        facing: 朝向（left/right/camera/away）。
        depth: 景深层次（near/mid/far），决定叠加顺序。
        anchor: 垂直锚点（bottom 底部落地 / top 顶部出血 / center 居中）。
        opacity: 不透明度（前景听者压暗时 <1）。
        blur: 高斯模糊半径（像素，前景听者虚化）。
        dim: 压暗比例（0~0.6，前景听者剪影化）。
        foreground: 是否前景遮挡层（过肩镜头的听者肩背）。
        sitting: 是否坐姿/蹲姿（合成时只贴上半身并落于屏幕下沿，避免全身
            站姿直接演"坐下"台词）。
    """

    asset_id: int
    name: str
    height_ratio: float = Field(ge=0.05, le=1.6)
    x: float = Field(ge=0.0, le=1.0)
    facing: Literal["left", "right", "camera", "away"] = "camera"
    depth: Literal["near", "mid", "far"] = "mid"
    anchor: Literal["bottom", "center", "top"] = "bottom"
    opacity: float = Field(default=1.0, ge=0.2, le=1.0)
    blur: float = Field(default=0.0, ge=0.0, le=12.0)
    dim: float = Field(default=0.0, ge=0.0, le=0.6)
    foreground: bool = False
    sitting: bool = False


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
        mode: 呈现方式（full 整幅道具特写 / hand 手持插入 / corner 角落静物）。
    """

    asset_id: int
    name: str
    owner: str | None = None
    height_ratio: float = Field(ge=0.03, le=0.6)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    depth: Literal["near", "mid", "far"] = "near"
    mode: Literal["full", "hand", "corner"] = "corner"


class CompositionSpec(BaseModel):
    """单镜整幅要素画面的完整构图描述。

    Attributes:
        version: 结构版本（便于后续演进）。
        layout_version: 构图规则版本（规则升级后旧 spec 自动重算）。
        aspect_ratio: 画幅（9:16/16:9/1:1）。
        canvas: 画布尺寸 {"w": int, "h": int}。
        shot_size: 景别（与 shots.shot_size 对齐）。
        setting: 场景基底资产 {"asset_id", "name"}，可空。
        subjects: 角色置入列表（从远到近）。
        props: 道具置入列表。
        focus: 视觉重心（character/prop/scene）。
        safe_area: 字幕安全区比例 {"top", "bottom"}。
        source: spec 来源（derived/llm/manual）。
        sitting: 本镜是否为坐姿/蹲姿（顶层标记，合成层据此只贴上半身）。
    """

    version: int = 1
    layout_version: int = 1
    aspect_ratio: str = "9:16"
    canvas: dict[str, int]
    shot_size: str
    setting: dict[str, Any] | None = None
    subjects: list[SubjectPlacement] = []
    props: list[PropPlacement] = []
    focus: Literal["character", "prop", "scene"] = "character"
    safe_area: dict[str, float]
    source: Literal["derived", "llm", "manual"] = "derived"
    sitting: bool = False


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


def _pick_main(characters: list[AssetRef], speaker_names: list[str] | None) -> AssetRef:
    """确定镜头主角色（说话人优先，否则取第一个）。

    Args:
        characters: 角色资产列表。
        speaker_names: 说话人姓名列表（可空）。

    Returns:
        主角色资产引用。

    Raises:
        IndexError: 角色列表为空（调用方保证非空）。
    """
    names = {item.name for item in characters}
    for name in speaker_names or []:
        if name in names:
            return next(item for item in characters if item.name == name)
    return characters[0]


def _layout_subjects(
    characters: list[AssetRef],
    height_ratio: float,
    speaker_names: list[str] | None,
    shot_size: str,
    shot_no: int = 1,
) -> list[SubjectPlacement]:
    """按景别、人数与说话人推导角色布局。

    规则（v7）：
    - 纯特写（close_up/extreme_close_up）：只保留说话人单人，顶部出血；
    - 过肩：说话人位于右中景，听者作为前景肩背压在右侧（虚化+压暗），
      形成真实的反打关系，避免"只有一个人对着镜头说话"；
    - 中景/全景双人：说话人占左主位 0.28，听者 0.72，间距 ≥0.30 画布宽；
    - 三人以上按 0.18~0.82 均分；
    - 同角色单人特写按镜号做确定性取景微变（±5% 身高、±3% 横向），
      避免同一角色两镜特写首帧完全一致、被观众识破为重复定格。

    Args:
        characters: 角色资产列表（说话人优先）。
        height_ratio: 主角色身高占比。
        speaker_names: 说话人姓名列表（可空）。
        shot_size: 景别。
        shot_no: 镜号（用于单人特写取景微变）。

    Returns:
        角色置入列表；无角色时返回空列表。

    Raises:
        无。
    """
    count = len(characters)
    if count == 0:
        return []
    main = _pick_main(characters, speaker_names)
    # 纯特写用顶部出血（半身特写）；其他景别按脚底/画框底部落地，
    # 否则人物会被推到画面上半部、只剩一截身体（实测穿帮）。
    close_shot = shot_size in ("close_up", "extreme_close_up")
    anchor: Literal["bottom", "top"] = "top" if close_shot else "bottom"
    if shot_size in _SINGLE_SUBJECT_SHOT_SIZES or count == 1:
        ratio_delta, x_delta = 0.0, 0.0
        if count == 1 and shot_size in _SINGLE_SUBJECT_SHOT_SIZES:
            variant = (max(1, int(shot_no)) - 1) % 3
            ratio_delta, x_delta = ((0.0, 0.0), (-0.05, 0.03), (0.06, -0.02))[variant]
        return [
            SubjectPlacement(
                asset_id=main.asset_id,
                name=main.name,
                height_ratio=round(_clamp(height_ratio + ratio_delta, 0.05, 1.6), 3),
                x=round(_clamp(0.5 + x_delta, 0.05, 0.95), 3),
                facing="camera",
                depth="mid",
                anchor=anchor,
            )
        ]

    secondary = next(item for item in characters if item.asset_id != main.asset_id)
    if shot_size == "over_shoulder":
        # 同一说话人换镜再出现过肩时按镜号微调站位与景深，避免两镜首帧
        # 完全一致（观众会识破为同一张图重复切换）。
        ots_variant = (max(1, int(shot_no)) - 1) % 3
        main_dx, main_ratio, fg_dx = (
            (0.0, 0.0, 0.0),
            (-0.04, -0.04, -0.02),
            (0.04, 0.05, 0.03),
        )[ots_variant]
        # 前景听者不露正脸：合成器会裁掉上半身，只留肩背作为遮挡层。
        return [
            SubjectPlacement(
                asset_id=main.asset_id,
                name=main.name,
                height_ratio=round(
                    _clamp(height_ratio + main_ratio, 0.05, 1.6), 3
                ),
                x=round(_clamp(0.34 + main_dx, 0.05, 0.95), 3),
                facing="right",
                depth="mid",
                anchor="bottom",
            ),
            SubjectPlacement(
                asset_id=secondary.asset_id,
                name=secondary.name,
                height_ratio=round(_clamp(height_ratio * 1.08, 0.05, 1.6), 3),
                x=round(_clamp(0.92 + fg_dx, 0.05, 0.95), 3),
                facing="left",
                depth="near",
                anchor="bottom",
                opacity=0.96,
                blur=12.0,
                dim=0.55,
                foreground=True,
            ),
        ]

    if count == 2:
        # P2-7 修复：远景双人镜两人身高接近（0.92 倍）且同地面线、
        # 同景深，避免"前景大头 + 后景小人"的拼贴感；中近景保留
        # 0.80 倍的近大远小景深比例。
        wide_two = shot_size in ("wide", "extreme_wide")
        secondary_scale = 0.92 if wide_two else 0.80
        secondary_depth = "mid" if wide_two else "far"
        return [
            SubjectPlacement(
                asset_id=main.asset_id,
                name=main.name,
                height_ratio=round(height_ratio, 3),
                x=_DUAL_X[0],
                facing="right",
                depth="mid",
                anchor="bottom",
            ),
            SubjectPlacement(
                asset_id=secondary.asset_id,
                name=secondary.name,
                height_ratio=round(_clamp(height_ratio * secondary_scale, 0.05, 1.6), 3),
                x=_DUAL_X[1],
                facing="left",
                depth=secondary_depth,
                anchor="bottom",
            ),
        ]

    step = 0.64 / (count - 1)
    placements: list[SubjectPlacement] = []
    for index, character in enumerate(characters[:4]):
        placements.append(
            SubjectPlacement(
                asset_id=character.asset_id,
                name=character.name,
                height_ratio=round(_clamp(height_ratio * (1 - 0.07 * index), 0.05, 1.6), 3),
                x=round(_clamp(0.18 + step * index, 0.08, 0.92), 3),
                facing="camera",
                depth="mid" if index == 0 else "far",
                anchor="bottom",
            )
        )
    return placements


def match_props(props: list[AssetRef], text: str) -> list[AssetRef]:
    """按画面描述匹配道具（名称、后缀核心词或末位名词命中）。

    Args:
        props: 候选道具列表。
        text: 镜头动作/画面描述。

    Returns:
        命中的道具列表（保持原顺序）；无命中时返回空列表。

    Raises:
        无。
    """
    matched: list[AssetRef] = []
    for prop in props:
        name = str(prop.name or "").strip()
        tokens = {name, name[-2:], name[-3:], name[-1:]}
        if any(token and token in text for token in tokens):
            matched.append(prop)
    return matched


def _layout_props(
    props: list[AssetRef],
    *,
    focus_prop: bool,
    text: str,
    shot_size: str = "medium",
) -> list[PropPlacement]:
    """推导道具布局。

    v7 规则：
    - 道具特写镜：道具作为整幅画面（mode=full）；
    - 中景/全景：道具以小图插入右下角（mode=corner），让"碗/锅/照片"
      这类叙事道具真实可见，而不是从画面里消失；
    - 特写/过肩：画面焦点在人物脸部或反打关系，道具出画（返回空列表），
      避免小卡片压在脸上。

    Args:
        props: 道具资产列表。
        focus_prop: 是否为主题特写。
        text: 镜头动作/画面描述（用于选主道具）。
        shot_size: 景别。

    Returns:
        道具置入列表；无道具或该景别不适用时返回空列表。

    Raises:
        无。
    """
    if not props:
        return []
    matched = match_props(props, text)
    head = matched[0] if matched else props[0]
    if focus_prop:
        return [
            PropPlacement(
                asset_id=head.asset_id,
                name=head.name,
                owner=None,
                height_ratio=0.55,
                x=0.5,
                y=0.55,
                depth="near",
                mode="full",
            )
        ]
    if shot_size in _SINGLE_SUBJECT_SHOT_SIZES or shot_size == "over_shoulder":
        return []
    return [
        PropPlacement(
            asset_id=head.asset_id,
            name=head.name,
            owner=None,
            height_ratio=0.22,
            x=0.86,
            y=0.90,
            depth="near",
            mode="corner",
        )
    ]


def layout_issues(spec: CompositionSpec) -> list[str]:
    """校验构图布局是否可信（LLM/历史 spec 进入合成前的安全网）。

    Args:
        spec: 待校验的构图规格。

    Returns:
        问题码列表；空列表表示可接受。

    Raises:
        无。
    """
    issues: list[str] = []
    subjects = list(spec.subjects)
    if not subjects and not spec.props:
        issues.append("empty_layout")
    xs = [float(item.x) for item in subjects]
    for index, left in enumerate(xs):
        for right in xs[index + 1 :]:
            if abs(left - right) < 0.30:
                issues.append("subjects_too_close")
    if spec.shot_size in _SINGLE_SUBJECT_SHOT_SIZES and len(subjects) > 1:
        issues.append("closeup_multi_subject")
    if spec.focus == "prop":
        if not spec.props:
            issues.append("prop_focus_without_prop")
        elif len(spec.props) > 1:
            issues.append("prop_focus_multi_prop")
        elif spec.props[0].mode != "full":
            issues.append("prop_focus_not_full")
        if subjects:
            # 道具特写是满幅道具画面，人物不应留在 spec.subjects（否则 QC
            # 会误判说话人在画面中，前端要素徽标也与实际画面不一致）。
            issues.append("prop_focus_with_subjects")
    else:
        for prop in spec.props:
            if prop.mode == "full" or float(prop.height_ratio) > 0.40:
                issues.append("prop_covers_character")
                break
    if subjects and spec.props and spec.focus != "prop":
        for prop in spec.props:
            for subject in subjects:
                # 右下角静物（y≥0.72）属于前景台面，不算挡脸；只拦截落在
                # 头部/胸口区域的插入图。
                if abs(float(prop.x) - float(subject.x)) < 0.22 and 0.35 <= float(prop.y) <= 0.68:
                    issues.append("prop_overlaps_face")
                    return issues
    return issues


def _apply_llm_hint(base: CompositionSpec, hint: dict[str, Any]) -> CompositionSpec:
    """把 LLM 构图提示合并到规则结果上（按角色名对齐、越界钳位）。

    Args:
        base: 规则推导出的 spec。
        hint: LLM 输出的构图提示（结构不可信，逐字段校验）。

    Returns:
        合并后的 spec；任何非法输入都返回 base，不抛异常。

    Raises:
        无。
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
        candidate = CompositionSpec(**data)
        # LLM 只允许微调位置；若整体布局不可信（双人贴叠/特写多主体/道具挡脸），
        # 直接回落规则结果，避免"来源标记为 llm 就永久复用错误构图"。
        return base if layout_issues(candidate) else candidate
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
        shot_no: 镜号（用于单人特写的确定性取景微变）。
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

    subjects = _layout_subjects(
        characters, height_ratio, speaker_names, size, shot_no
    )
    # 2026-11 QA 修复（站姿演坐姿）：动作行明确写"坐/蹲/躺"时，主角色标记
    # sitting，合成层只贴上半身并落于屏幕下沿（视觉上像坐在桌前），避免
    # 全身站姿与台词动作互相打架。
    sit_pose = any(
        key in text
        for key in ("坐在", "坐下", "坐着", "坐回", "坐进", "蹲下", "蹲在", "躺", "趴", "坐", "落座")
    )
    # 桌前语境：剧本常写"压着纸沿桌面推过去/撑上桌面/摊开在桌沿"而不写"坐"，
    # 但人物显然坐在桌边。这类动作若按全身站姿合成，会与"桌面推纸"冲突，
    # 人物也会把室内背景挡满，因此一并按坐姿裁上半身。
    desk_pose = (
        any(key in text for key in ("桌面", "桌前", "桌沿", "桌边", "吧台前"))
        and any(key in text for key in ("推", "递", "压", "摊", "翻", "签", "撑", "放"))
    )
    if subjects and (sit_pose or desk_pose):
        subjects[0] = subjects[0].model_copy(update={"sitting": True})
    focus_prop = "特写" in text and bool(props)
    placements = _layout_props(
        props, focus_prop=focus_prop, text=text, shot_size=size
    )
    if focus_prop:
        focus: Literal["character", "prop", "scene"] = "prop"
        # 道具特写是"满幅道具画面"，人物不出镜（台词作画外音）。若保留
        # subjects，QC 会误判"说话人在画面中"（假阳性），前端要素徽标也会
        # 显示并未出现的人物，与实际画面不一致。
        subjects = []
    elif characters:
        focus = "character"
    else:
        focus = "scene"

    spec = CompositionSpec(
        layout_version=LAYOUT_VERSION,
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
        # 顶层冗余标记：即使 per-subject 字段在历史 spec/序列化中被丢弃，
        # 合成层也能可靠识别坐姿（QA 复现过 placement 标记未生效的情况）。
        sitting=bool((sit_pose or desk_pose) and subjects),
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

    Raises:
        无。
    """
    if not isinstance(raw, dict):
        return fallback
    try:
        return CompositionSpec(**raw)
    except (ValidationError, TypeError, ValueError):
        return fallback
