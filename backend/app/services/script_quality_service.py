"""剧本结构质量自评服务（P0 剧本模块优化）。

背景：短剧剧本的留存率取决于"3 秒钩子、冲突密度、结尾悬念"三要素，
但剧本生成链路此前只做契约校验（字段合法性），没有任何结构性打分，
钩子弱、平铺直叙的剧本会直接流入分镜与渲染，造成整条链路成本浪费。

本模块提供纯规则（零 LLM 成本）的结构评分：

- :func:`evaluate_episode`：按"3 秒钩子（40 分）/ 冲突升级密度（30 分）/
  结尾悬念（20 分）/ 台词质量（10 分）"打分，输出 verdict 与逐项问题；
- :func:`build_rewrite_feedback`：把扣分项转写成重写指令，供剧本生成
  节点在低于阈值时自动重写一次；
- :func:`REWRITE_TRIGGER_SCORE`：自动重写触发阈值（75 分）。

评分口径（对齐交付 rubric）：
- 钩子：前 3 秒（按语速 4 字/秒估算前 12 字）内出现问句/感叹/悬念词/
  冲突动词之一；前两行口播行必须存在；
- 冲突：台词情绪向高唤醒（tension/anger/fear/surprise）迁移、或台词
  命中冲突动词词表记 1 个冲突点；≥2 个冲突升级点满分；
- 悬念：末句口播以 ？/！/… 收尾、或命中悬念词（到底/真相/竟然/别走/
  等等/还没完），或集 hook 字段非空；
- 台词质量：无纯口水行（嗯/啊/哦/是吗/好），单行 ≤60 字占比 ≥80%。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "REWRITE_TRIGGER_SCORE",
    "build_rewrite_feedback",
    "evaluate_episode",
]

#: 自动重写触发阈值：总得分低于该值时剧本生成节点重写一次
REWRITE_TRIGGER_SCORE: int = 75
#: 视觉可拍性触发阈值（满分 10）：低于该值即使结构分达标也要重写一次。
#: AI 图像/视频只能看见剧本写出的视觉信息，视觉要素缺失会直接变成画面跑偏。
VISUAL_REWRITE_TRIGGER_SCORE: int = 7

#: 具象动作动词词表（与 motion_service.ACTION_VERBS 同源，取高频子集；
#: 在此独立定义避免剧本自评模块依赖媒体层）
_VISUAL_ACTION_VERBS: tuple[str, ...] = (
    "抬手", "转身", "走近", "后退", "低头", "抬头", "伸手", "握住", "推开",
    "坐下", "起身", "奔跑", "回头", "微笑", "皱眉", "落泪", "拥抱", "递给",
    "接过", "跑", "走", "站起", "拿起", "放下", "看向", "说", "喊", "哭",
    "笑", "颤抖", "攥紧", "翻开", "合上", "靠近", "离开", "蹲下", "扶住",
    "摇头", "点头", "俯身", "迈步", "挥手", "倒下", "抱住", "擦", "写",
    "喝", "吃", "穿过", "停下", "弯腰", "捡起", "打开", "关上", "拉", "推",
)

#: 时间/氛围特征词：用于判定 location 是否为「场景类型+时间+氛围」完整描述
_LOCATION_ATMOSPHERE_MARKERS: tuple[str, ...] = (
    "清晨", "早晨", "上午", "午后", "下午", "傍晚", "黄昏", "夜晚", "深夜",
    "白天", "夜", "光", "灯", "雨", "雪", "风", "氛围", "安静", "嘈杂",
    "温暖", "冷", "昏暗", "明亮", "霓虹", "夕阳", "晨",
)

#: 因果衔接特征词（场次 summary 必须写明「上一场的结果导致本场…」）
_CAUSE_MARKERS: tuple[str, ...] = (
    "承接", "因为", "所以", "导致", "于是", "因此", "结果", "随后", "紧接着",
)

#: 高唤醒情绪（冲突点判定）
_HIGH_AROUSAL: frozenset[str] = frozenset({"tension", "anger", "fear", "surprise"})

#: 钩子特征词：问句/悬念/突发
_HOOK_MARKERS: tuple[str, ...] = (
    "？", "?", "！", "!", "…", "突然", "竟然", "居然", "为什么", "凭什么",
    "谁", "别", "快", "跑", "救", "等一下", "住手", "求", "到底", "真相",
)

#: 冲突动词词表（台词命中即记冲突点）
_CONFLICT_VERBS: tuple[str, ...] = (
    "抢", "夺", "砸", "吵", "骂", "质问", "拦", "追", "断", "拒绝", "撕",
    "打", "推", "警告", "威胁", "背叛", "隐瞒", "逼", "赶", "滚", "分手",
    "离婚", "辞退", "开除", "揭穿", "拆穿", "宣战", "对峙", "争吵", "反抗",
)

#: 结尾悬念词
_SUSPENSE_MARKERS: tuple[str, ...] = (
    "？", "?", "…", "到底", "真相", "竟然", "居然", "还没完", "别走", "等等",
    "接下来", "第二天", "三章", "下回", "未完", "才刚刚开始", "想知道",
)

#: 流畅口语行模式（整行匹配）
_FILLER_PATTERN = re.compile(r"^[嗯啊哦呃嘿哈呵噢哎呀好行是吗对噢噢~,，。.!！?？\s]+$")

#: 角色名 + 破折号写法（剧本约定「角色名——着装+发型+动作」），用于识别
#: 某场出场了哪些角色。
_CHARACTER_DASH_PATTERN = re.compile(r"^([\u4e00-\u9fa5]{2,4})——")

#: 服装/发型特征词：判定该角色的 action 行是否写明了着装（2026-11 QA 新增）。
#: 跨场着装缺失会让分镜取不到服装文本，进而导致成片角色形象漂移。
_OUTFIT_MARKERS: tuple[str, ...] = (
    "开衫", "毛衣", "针织", "衬衫", "衬衣", "T恤", "吊带", "西装", "外套",
    "风衣", "大衣", "夹克", "卫衣", "长裙", "短裙", "连衣裙", "裙",
    "长裤", "牛仔裤", "阔腿裤", "西装裤", "工装裤", "短裤", "制服", "围裙",
    "旗袍", "汉服", "背心", "马甲", "丝质", "棉麻", "粗花呢", "皮衣", "雨衣",
    "发簪", "马尾", "丸子头", "短发", "长发", "卷发", "直发", "齐肩", "寸头",
    "挽起", "披肩", "眼镜", "领带", "领结",
)

#: 语速估算（字/秒）：用于"前 3 秒"的字数换算
_SPEECH_CHARS_PER_SECOND: float = 4.0


def _spoken_lines(episode: dict[str, Any]) -> list[dict[str, Any]]:
    """收集单集剧本中所有口播行（台词+旁白），保持顺序。

    Args:
        episode: 剧本集字典（含 scenes[].lines[]）。

    Returns:
        ``[{"line_type", "speaker", "content", "emotion"}]`` 扁平列表。
    """
    spoken: list[dict[str, Any]] = []
    for scene in episode.get("scenes") or []:
        for line in scene.get("lines") or []:
            if str(line.get("line_type") or "") in ("dialogue", "narration"):
                spoken.append(line)
    return spoken


def _score_hook(episode: dict[str, Any]) -> tuple[int, int, list[str]]:
    """评"3 秒钩子"维度。

    Args:
        episode: 剧本集字典。

    Returns:
        ``(得分, 满分, 问题列表)`` 三元组。
    """
    full = 40
    spoken = _spoken_lines(episode)
    if not spoken:
        return 0, full, ["全集没有台词/旁白行，无法建立钩子"]
    issues: list[str] = []
    budget = int(_SPEECH_CHARS_PER_SECOND * 3)
    used = 0
    hook_line: str | None = None
    for line in spoken:
        content = str(line.get("content") or "").strip()
        if not content:
            continue
        if used + len(content) <= budget:
            used += len(content)
            hook_line = content
        else:
            break
    if hook_line is None:
        issues.append(f"前 3 秒（≈{budget} 字）内没有口播内容，开场冷启动")
        return 0, full, issues
    if _FILLER_PATTERN.match(hook_line):
        issues.append(f"开场首句为口水行（{hook_line[:12]}），钩子无效")
        return 5, full, issues
    if any(marker in hook_line for marker in _HOOK_MARKERS):
        return full, full, issues
    issues.append(f"开场首句无问句/感叹/悬念词等钩子特征（{hook_line[:24]}）")
    return 12, full, issues


def _score_conflict(episode: dict[str, Any]) -> tuple[int, int, list[str]]:
    """评"冲突升级密度"维度：30 秒内至少 2 个冲突升级点得满分。

    Args:
        episode: 剧本集字典。

    Returns:
        ``(得分, 满分, 问题列表)`` 三元组。
    """
    full = 30
    spoken = _spoken_lines(episode)
    issues: list[str] = []
    points = 0
    prev_emotion: str | None = None
    for line in spoken:
        content = str(line.get("content") or "")
        emotion = str(line.get("emotion") or "") or None
        if any(verb in content for verb in _CONFLICT_VERBS):
            points += 1
            continue
        if emotion in _HIGH_AROUSAL and prev_emotion not in (None, emotion):
            # 情绪向高唤醒迁移视为一次升级；同为高唤醒的连续行记半个点，
            # 两个连续高唤醒行凑一个冲突点
            points += 0.5
        prev_emotion = emotion
    if points >= 2:
        return full, full, issues
    if points >= 1.5:
        return 22, full, issues
    issues.append(
        f"冲突升级点不足（{points:g} 个 < 2 个）：缺少情绪迁移与冲突动词驱动的对抗节拍"
    )
    return int(points * 12), full, issues


def _score_ending(episode: dict[str, Any]) -> tuple[int, int, list[str]]:
    """评"结尾悬念"维度。

    Args:
        episode: 剧本集字典。

    Returns:
        ``(得分, 满分, 问题列表)`` 三元组。
    """
    full = 20
    spoken = _spoken_lines(episode)
    hook_text = str(episode.get("hook") or "").strip()
    issues: list[str] = []
    last = spoken[-1] if spoken else None
    if last is None:
        issues.append("无口播行，无法形成结尾悬念")
        return 0, full, issues
    content = str(last.get("content") or "").strip()
    if any(marker in content for marker in _SUSPENSE_MARKERS):
        return full, full, issues
    if hook_text and any(marker in hook_text for marker in _SUSPENSE_MARKERS):
        return 15, full, issues
    if hook_text:
        issues.append(f"末句口播与集钩子均无悬念特征（hook={hook_text[:20]}）")
        return 8, full, issues
    issues.append("结尾缺悬念：末句未以 ？/… 收尾也无悬念词，集 hook 为空")
    return 0, full, issues


def _score_dialogue(episode: dict[str, Any]) -> tuple[int, int, list[str]]:
    """评"台词质量"维度：无口水行、单行长度克制。

    Args:
        episode: 剧本集字典。

    Returns:
        ``(得分, 满分, 问题列表)`` 三元组。
    """
    full = 10
    spoken = _spoken_lines(episode)
    if not spoken:
        return 0, full, ["无口播行"]
    issues: list[str] = []
    filler = [
        str(line.get("content") or "")
        for line in spoken
        if _FILLER_PATTERN.match(str(line.get("content") or ""))
    ]
    long_lines = [
        str(line.get("content") or "")
        for line in spoken
        if len(str(line.get("content") or "")) > 60
    ]
    if len(filler) > max(0, len(spoken) // 4):
        issues.append(f"口水行过多（{len(filler)}/{len(spoken)}）")
    if long_lines:
        issues.append(f"超长台词 {len(long_lines)} 行（>60 字），建议拆短")
    penalty = 5 * (1 if filler and len(filler) > max(0, len(spoken) // 4) else 0)
    penalty += 4 * (1 if long_lines else 0)
    return max(0, full - penalty), full, issues


def _score_visual(episode: dict[str, Any]) -> tuple[int, int, list[str]]:
    """评"视觉可拍性"维度（满分 10）：剧本是否自带完整视觉信息。

    对应质检项（2026-11 新增，见编剧提示词 _VISUAL_CRAFT）：
    - location 是否为「场景类型 + 时间 + 氛围」完整描述（禁止裸地点）；
    - action 行是否写出具象动作动词（禁止「她很生气」这类抽象描述）；
    - action 行是否标注「道具：…」或「机位：…」；
    - 场次 summary 是否写明与上一场的因果（承接/因为/所以/导致…）；
    - 每场是否至少有一个 action 行（否则整场无法生成画面）。

    Args:
        episode: 剧本集字典（含 scenes[].location/summary/lines[]）。

    Returns:
        ``(得分, 满分, 问题列表)`` 三元组；无场次时返回 0 分并说明原因。

    Raises:
        无：字段缺失按不达标处理。
    """
    full = 10
    scenes = list(episode.get("scenes") or [])
    if not scenes:
        return 0, full, ["全集没有场次，无法评估视觉可拍性"]

    complete_locations = 0
    cause_scenes = 0
    scenes_with_action = 0
    action_total = 0
    action_with_verb = 0
    action_with_prop_or_camera = 0

    for scene in scenes:
        location = str(scene.get("location") or "").strip()
        if len(location) >= 6 and any(
            marker in location for marker in _LOCATION_ATMOSPHERE_MARKERS
        ):
            complete_locations += 1
        summary = str(scene.get("summary") or "").strip()
        if any(marker in summary for marker in _CAUSE_MARKERS):
            cause_scenes += 1
        actions = [
            str(line.get("content") or "").strip()
            for line in (scene.get("lines") or [])
            if str(line.get("line_type") or "") == "action" and str(line.get("content") or "").strip()
        ]
        if actions:
            scenes_with_action += 1
        for content in actions:
            action_total += 1
            if any(verb in content for verb in _VISUAL_ACTION_VERBS):
                action_with_verb += 1
            if "道具：" in content or "道具:" in content or "机位：" in content or "机位:" in content:
                action_with_prop_or_camera += 1

    scene_count = len(scenes)
    score = 0.0
    issues: list[str] = []

    ratio = complete_locations / scene_count
    score += ratio * 2
    if ratio < 1.0:
        issues.append(
            f"location 完整度 {complete_locations}/{scene_count}："
            "location 必须写「场景类型+时间+氛围」，禁止裸地点"
        )

    if action_total == 0:
        issues.append("全集没有 action 行，画面动作完全无从生成")
    else:
        ratio = action_with_verb / action_total
        score += ratio * 3
        if ratio < 1.0:
            issues.append(
                f"具象动作 {action_with_verb}/{action_total}："
                "action 行缺少动作动词（禁止「她很生气」这类抽象描述）"
            )
        ratio = action_with_prop_or_camera / action_total
        score += ratio * 2
        if ratio < 0.6:
            issues.append(
                f"道具/机位标注 {action_with_prop_or_camera}/{action_total}："
                "action 行应写「道具：…」与「机位：…」，否则下游只能猜"
            )

    ratio = cause_scenes / scene_count
    score += ratio * 2
    if ratio < 1.0:
        issues.append(
            f"场次因果 {cause_scenes}/{scene_count}："
            "summary 须以「承接：…」写明本场由上一场结果导致"
        )

    ratio = scenes_with_action / scene_count
    score += ratio
    if ratio < 1.0:
        issues.append(
            f"动作行覆盖 {scenes_with_action}/{scene_count}："
            "每个场次至少要有 1 个 action 行，否则该场无法生成画面"
        )

    # 着装逐场覆盖率（2026-11 QA 新增）：「角色名——着装」必须在该场出现。
    # 分镜按场次取用服装文本，任一角色在某场缺着装 → 该场该角色的镜头
    # 失去服装约束，成片形象漂移（验收实测 7/12 镜漂移）。
    missing_outfits: list[str] = []
    outfit_checked = 0
    for scene in scenes:
        scene_no = scene.get("scene_no") or scene.get("order_index") or "?"
        scene_actions = [
            str(line.get("content") or "").strip()
            for line in (scene.get("lines") or [])
            if str(line.get("line_type") or "") == "action"
        ]
        # 本场出场角色：action 行「角色名——…」写法 + 台词说话人
        scene_characters: list[str] = []
        for content in scene_actions:
            match = _CHARACTER_DASH_PATTERN.match(content)
            if match and match.group(1) not in scene_characters:
                scene_characters.append(match.group(1))
        for line in scene.get("lines") or []:
            speaker = str(line.get("speaker_name") or "").strip()
            if speaker and speaker not in scene_characters:
                scene_characters.append(speaker)
        for name in scene_characters:
            outfit_checked += 1
            if any(
                name in content and any(m in content for m in _OUTFIT_MARKERS)
                for content in scene_actions
            ):
                continue
            missing_outfits.append(f"场{scene_no}·{name}")

    if outfit_checked and missing_outfits:
        issues.append(
            "着装缺失 " + f"{len(missing_outfits)}/{outfit_checked}："
            + "、".join(missing_outfits[:6])
            + "（每个角色在每个场次的首次 action 行必须写「角色名——着装+发型」，"
            "跨场省略会导致该场镜头形象漂移）"
        )
        # 门槛：缺失超过两成时把视觉分压到重写阈值以下，强制再生成一稿
        # （两稿择优不会让质量变差）。
        if len(missing_outfits) / outfit_checked > 0.2:
            score = min(score, 6.0)

    return int(round(score)), full, issues


def evaluate_episode(episode: dict[str, Any]) -> dict[str, Any]:
    """对单集剧本做结构质量自评（纯规则，零 LLM 成本）。

    Args:
        episode: 剧本集字典（EpisodeScript 结构 dict 形式）。

    Returns:
        评估报告字典：``{"score", "visual_score", "verdict", "dimensions",
        "issues", "should_rewrite"}``；score 为结构分（100 制，不含视觉分），
        visual_score 为视觉可拍性（10 制，独立计分）；verdict ∈
        passed(≥75)/warning(60-74)/failed(<60)；视觉分低于
        :data:`VISUAL_REWRITE_TRIGGER_SCORE` 时同样触发重写。
    """
    dims = {
        "hook_3s": _score_hook(episode),
        "conflict_escalation": _score_conflict(episode),
        "ending_suspense": _score_ending(episode),
        "dialogue_quality": _score_dialogue(episode),
    }
    visual_got, visual_full, visual_issues = _score_visual(episode)
    score = 0
    issues: list[str] = []
    for name, (got, full, dim_issues) in dims.items():
        score += got
        issues.extend(f"[{name}] {item}" for item in dim_issues)
    issues.extend(f"[visual_craft] {item}" for item in visual_issues)
    dimensions = {
        name: {"score": got, "full": full, "issues": dim_issues}
        for name, (got, full, dim_issues) in dims.items()
    }
    # 视觉可拍性独立计分（不并入 100 分结构分，避免改变既有阈值语义）：
    # 低于阈值同样触发一次重写，确保剧本自带动作/场景/道具信息。
    dimensions["visual_craft"] = {
        "score": visual_got,
        "full": visual_full,
        "issues": visual_issues,
    }
    verdict = "passed" if score >= 75 else ("warning" if score >= 60 else "failed")
    should_rewrite = (
        score < REWRITE_TRIGGER_SCORE
        or visual_got < VISUAL_REWRITE_TRIGGER_SCORE
    )
    return {
        "score": int(score),
        "visual_score": int(visual_got),
        "verdict": verdict,
        "dimensions": dimensions,
        "issues": issues,
        "should_rewrite": should_rewrite,
    }


def build_rewrite_feedback(report: dict[str, Any]) -> str:
    """把自评报告转写为重写指令文本（注入重写 prompt 的 mode_hint）。

    Args:
        report: :func:`evaluate_episode` 返回的报告字典。

    Returns:
        重写指令字符串（≤800 字）；无问题项时返回通用强化指令。
    """
    issues = list(report.get("issues") or [])
    if not issues:
        return (
            "上稿结构自评未达标，请强化 3 秒钩子、冲突密度与结尾悬念；"
            "同时补齐视觉要素：location 三要素、action 行动作动词与「道具/机位」标注。"
        )
    text = "；".join(issue.replace("[", "").replace("]", ": ", 1) for issue in issues)
    instruction = (
        f"上稿剧本自评 {report.get('score', 0)}/100（视觉可拍性 "
        f"{report.get('visual_score', 0)}/10）未达标，必须修复：{text}。"
        "重写要求：开场首句 12 字内抛出钩子（问句/突发/悬念）；"
        "全片至少 2 个冲突升级点（情绪迁移或冲突动词）；"
        "结尾以 ？/…/悬念词收尾留钩子；台词精炼无口水；"
        "每个场次先写 1 个 action 行：具象动作（含动作动词）+ 表情姿态 + "
        "「道具：…」+「机位：固定/推近/环绕/跟随」；"
        "location 必须写「场景类型+时间+氛围」，summary 以「承接：上一场…」写明因果。"
    )
    return instruction[:800]
