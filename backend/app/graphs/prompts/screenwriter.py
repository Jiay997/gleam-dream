"""编剧 Agent 提示词模块（五段式，中文）。

提供两个提示词构造函数（均返回"系统提示词 + 用户提示词"二元组）：

- :func:`build_outline_prompt`：大纲阶段——产出剧名/梗概/人物小传/
  分集计划/write_todos 计划；
- :func:`build_episode_prompt`：逐集阶段——按分集计划产出单集
  「场-行」结构化剧本；human_feedback / compliance_findings 非空时
  切换为局部改写/弱化重审模式。

五段式结构（PRD 附录 B）：角色与目标 / 项目上下文 / 工作区约定 /
预算与边界 / 输出契约。输出契约以注释内嵌 JSON 结构说明，
LLM 以 JSON 模式（response_format=json_object）返回。

视觉可拍性约定：AI 图像/视频生成只能"看见"剧本里写出的视觉信息，
因此大纲的人物外形必须给出固定着装/发型基准，逐集剧本的每个场次、
动作行必须自带场景氛围、具象动作、关键道具与运镜建议，并遵守
角色服装一致性、动作连贯、道具状态延续等连续性铁律（见
:data:`_VISUAL_CRAFT`）。
"""

from __future__ import annotations

import json
from typing import Any

# 画风预设键 -> 中文名映射（项目上下文段渲染用）
_STYLE_LABELS: dict[str, str] = {
    "live_action": "真人感",
    "anime": "二次元",
    "chinese_style": "国风",
    "three_d": "3D",
}

# 单集目标时长区间（秒，PRD F2.5：1-3 分钟）
EPISODE_DURATION_MIN = 60
EPISODE_DURATION_MAX = 180

# 视觉可拍性与连续性铁律（编剧提示词第一段追加项，编号接续成功标准 ⑥）
# 设计依据：AI 视频生成只能"看见"写出来的内容，未写出的视觉信息等于不存在；
# 因此把「场景描述格式 / 具象动作 / 道具标注 / 运镜建议 / 服装与道具连续性 /
# 钩子密度」全部前置为编剧硬约束，避免分镜阶段再靠导演二次凭空补全。
_VISUAL_CRAFT = (
    "⑦ 视觉可拍性（AI 视频生成只能看见你写出的内容，看不见你脑中的想象；"
    "禁止只写台词和粗略情节）：\n"
    "  - location 必须是「场景类型 + 时间 + 氛围」三要素齐全的完整描述"
    "（正确：现代都市咖啡馆，午后，暖光透过落地窗，背景有少量顾客，氛围安静；"
    "错误：咖啡厅），禁止裸地点；\n"
    "  - 着装与发型逐场必写（2026-11 修复）：每一场里，每个出场角色的第一次"
    "action 行都必须复用「角色名——着装+发型」的写法"
    "（如「林薇——米色风衣，头发微乱，神情焦虑，推门进来」「周砚——深灰西装，"
    "领带松垮，站在柜台后」），着装文本须与该角色在剧集设定中的 appearance 基准"
    "逐字一致；不得因为「上一场已经写过」而在新场次省略——下游分镜按场次取用"
    "服装描述，省略会导致该场角色形象漂移；\n"
    "  - action 行必须用角色真名作主语，禁止用「他/她/两人/对方」指代"
    "（下游按角色名绑定角色参考图，代词会让镜头丢失角色锚点、形象自由发挥）；\n"
    "  - action 行必须是「具体动作 + 表情 + 身体姿态」的具象描述"
    "（正确：林薇猛地站起来，双手撑在桌面上，手指收紧，眉头紧锁瞪着对面；"
    "错误：她很生气 / 两人发生争吵），禁止抽象概括；\n"
    "  - 画面关键道具写进 action 行并用「道具：」标注（如「道具：桌角摊开的离婚协议书」）；"
    "每个 action 行末尾用「机位：固定/推近/环绕/跟随」给出运镜建议，拿不准就写「机位：固定」；\n"
    "⑧ 连续性铁律：\n"
    "  - 角色一致性：同一角色的服装、发型全剧一致；只有 stage_direction 行明确声明"
    "「已换XX装」时才允许改变，且换装后所有后续场次沿用新装；\n"
    "  - 动作连贯：上一行的结束动作 = 下一行的起始状态，禁止人物瞬移、姿势突变；\n"
    "  - 场景连续性：同一场景拆成多行/多场时，location 文本逐字一致，不得改写同义说法；\n"
    "  - 道具连续性：道具出现、损坏、易主必须在行内交代，后续场次不得凭空复原或消失；\n"
    "  - 因果链：每场 summary 以「承接：上一场的XX结果导致本场…」开头写明因果，禁止情节跳跃。\n"
    "⑨ 节奏控制（60-90 秒单集规格）：\n"
    "  - 以 action 行作为镜头计数单位：60-90 秒须 8~12 个 action 行，每行 2~4 秒可拍内容；\n"
    "  - 每 3 个 action 行内必须出现一个钩子（冲突/反转/悬念）；\n"
    "  - 开头 5 秒（首场前 2 行、首句台词约 12 字内）必须出现核心冲突，禁止铺垫式冷开场。\n"
)


def _project_context(state: dict[str, Any]) -> str:
    """渲染五段式第二段「项目上下文」。

    Args:
        state: 编排状态（取 idea/genre/style_preset_key/aspect_ratio/target_episode_count）。

    Returns:
        上下文段文本。
    """
    style = _STYLE_LABELS.get(str(state.get("style_preset_key") or "live_action"), "真人感")
    idea = str(state.get("idea") or "").strip() or "（用户未提供更多细节，请自行发挥）"
    return (
        f"- 用户创意：{idea}\n"
        f"- 项目题材：{state.get('genre') or '都市情感'}\n"
        f"- 画风预设：{state.get('style_preset_key')}（{style}）\n"
        f"- 画幅：{state.get('aspect_ratio') or '9:16'}（竖屏短剧）\n"
        f"- 目标：共 {state.get('target_episode_count', 1)} 集，"
        f"单集时长 {EPISODE_DURATION_MIN}-{EPISODE_DURATION_MAX} 秒"
    )


def _budget_and_limits(state: dict[str, Any]) -> str:
    """渲染五段式第四段「预算与边界」。

    Args:
        state: 编排状态（取 estimated_credits 展示预算镜像）。

    Returns:
        预算边界段文本。
    """
    remaining = int(state.get("estimated_credits") or 0)
    return (
        f"- 本项目预估总预算：{remaining} 积分（由确定性计费节点管理，你无需也无法操作）\n"
        f"- 允许：产出大纲、人物、分集计划、逐集剧本的结构化 JSON。\n"
        f"- 禁止（硬边界，违反即失败）：\n"
        f"  1. 生成涉政、色情、暴力血腥、涉未成年人恋爱或性暗示的内容；\n"
        f"  2. 影射真实公众人物姓名/肖像，或抄袭已公映作品情节；\n"
        f"  3. 决定或猜测任何计费、权限、模型选择结果（这些由确定性节点处理）。\n"
        f"- 不确定时宁可保守，降低表达强度而非臆测。"
    )


_OUTLINE_CONTRACT = """{
  "title": "剧名（<=60字）",
  "genre": "题材",
  "logline": "一句话梗概（<=300字）",
  "characters": [
    {
      "name": "人物名",
      "role": "protagonist|antagonist|supporting|extra",
      "appearance": "外形与着装描述（<=500字，必须含固定着装+发型+体态基准，全剧据此保持一致）",
      "age_range": "如 25-30",
      "gender": "male|female|other|unspecified",
      "bio": "人物小传（可空）"
    }
  ],
  "episode_plans": [
    {"episode_no": 1, "title": "集标题", "synopsis": "本集剧情概要", "hook": "结尾钩子"}
  ],
  "write_todos": {
    "todos": [
      {"step": 1, "title": "步骤标题", "detail": "步骤说明（可空）"}
    ]
  }
}"""

_EPISODE_CONTRACT = """{
  "episode_no": 1,
  "title": "集标题",
  "hook": "结尾钩子（<=200字）",
  "summary": "本集剧情概要（<=500字）",
  "scenes": [
    {
      "scene_no": 1,
      "location": "地点（场景类型+时间+氛围的完整描述，禁止裸地点）",
      "time_of_day": "dawn|morning|noon|afternoon|dusk|night（与 location 中的时间一致）",
      "int_ext": "int|ext（可空）",
      "summary": "本场概要（以「承接：上一场的XX结果导致本场…」开头 + 触发事件 + 情绪反应）",
      "lines": [
        {
          "line_type": "dialogue|narration|action|stage_direction",
          "speaker": "说话人（dialogue 必填，其余为 null）",
          "content": "行内容（<=500字，口语化可配音；action 行须为「具象动作+表情+姿态+道具：…+机位：…」）",
          "emotion": "calm|joy|anger|sadness|fear|surprise|tension|romance（dialogue 必填，语气只写在此字段）"
        }
      ]
    }
  ]
}"""


def build_outline_prompt(state: dict[str, Any]) -> tuple[str, str]:
    """构造大纲阶段提示词（系统 + 用户）。

    Args:
        state: 编排状态（ProjectState 的已填字段）。

    Returns:
        (system_prompt, user_prompt) 二元组。
    """
    system = (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的编剧 Agent（screenwriter），一名专攻竖屏短剧（9:16）的职业编剧。\n"
        "你精通 1-3 分钟单集叙事：开场 3 秒抓人、每 15-20 秒一个小反转、每集结尾留钩子。\n"
        "你的目标是产出「可直接进入分镜」的剧本骨架：剧名、一句话梗概、人物小传、分集计划，"
        "并给出你的工作计划（write_todos），计划对用户可见。\n"
        "成功标准：① 分集计划覆盖全部目标集数且集号连续；② 每集计划均含结尾钩子；"
        "③ 人物关系清晰、动机充分；"
        "④ 每个人物的 appearance 必须给出「固定着装 + 发型 + 体态」基准"
        "（AI 资产生成与跨场一致性都以它为准，不得只写性格或笼统长相）。\n\n"
        "【第二段 · 项目上下文】\n"
        f"{_project_context(state)}\n\n"
        "【第三段 · 工作区约定】\n"
        "你的产物由编排节点代写入项目工作区 /project/{project_id}/，你只需按契约输出 JSON：\n"
        "- 大纲与人物 → /project/script/outline.md\n"
        "- 逐集剧本（后续阶段） → /project/script/ep01.md 起逐集编号\n"
        "命名规范：集号两位补零（ep01、ep02）。禁止：直接写数据库、调用渲染/计费工具。\n\n"
        "【第四段 · 预算与边界】\n"
        f"{_budget_and_limits(state)}\n\n"
        "【第五段 · 输出契约】\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释文字），结构如下：\n"
        f"{_OUTLINE_CONTRACT}\n"
        "要求：episode_plans 的集号从 1 连续递增到目标集数；write_todos 覆盖"
        "「大纲 → 人物 → 分集 → 逐集对白」四步。"
    )
    user = (
        f"请为目标 {state.get('target_episode_count', 1)} 集的竖屏短剧产出大纲。"
        "只输出符合契约的 JSON。"
    )
    return system, user


def build_episode_prompt(
    state: dict[str, Any],
    outline: dict[str, Any],
    episode_plan: dict[str, Any],
    *,
    human_feedback: str | None = None,
    compliance_findings: list[dict[str, Any]] | None = None,
    previous_episodes: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """构造逐集剧本生成/局部改写提示词（系统 + 用户）。

    Args:
        state: 编排状态。
        outline: 大纲契约产物（title/logline/characters）。
        episode_plan: 当前集的分集计划（episode_no/title/synopsis/hook）。
        human_feedback: 用户 reject 反馈（非空时进入局部改写模式）。
        compliance_findings: 审核发现（非空时进入弱化重审模式）。
        previous_episodes: 前序已生成集的摘要（保持连贯）。

    Returns:
        (system_prompt, user_prompt) 二元组。
    """
    characters = outline.get("characters", [])
    character_lines = "\n".join(
        f"  - {c.get('name')}（{c.get('role')}，{c.get('age_range')}）：{c.get('appearance')}"
        for c in characters
    ) or "  -（无人物清单，请依据分集计划自行设定）"

    prev_lines = ""
    if previous_episodes:
        prev_lines = "\n".join(
            f"  - 第 {ep.get('episode_no')} 集《{ep.get('title')}》：{ep.get('summary')}"
            for ep in previous_episodes[-3:]
        )

    mode_hint = "全新生成本集剧本。"
    if human_feedback:
        mode_hint = (
            f"这是「局部改写」任务。用户反馈如下，请针对性修正（其余内容尽量保留）：\n"
            f"  用户反馈：{human_feedback}"
        )
    if compliance_findings:
        finding_lines = "\n".join(
            f"  - [{f.get('level')}] {f.get('location')}：{f.get('description')}；"
            f"建议：{f.get('suggestion')}"
            for f in compliance_findings
        )
        mode_hint += (
            f"\n这是「弱化重审」任务。上稿命中以下内容安全风险，请按建议弱化改写"
            f"（保持剧情骨架不变）：\n{finding_lines}"
        )

    system = (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的编剧 Agent（screenwriter），一名专攻竖屏短剧（9:16）的职业编剧。\n"
        "你精通 1-3 分钟单集叙事：开场 3 秒抓人、每 15-20 秒一个小反转、每集结尾留钩子。\n"
        "本次任务：按分集计划产出单集结构化剧本（场-行三级），对白口语化且可配音。\n"
        "成功标准：① 2-3 个场次、每场 6-12 行（30 秒规格下调为每场 3-4 行）；"
        "② 每集结尾有明确钩子；③ 不触发内容安全边界；"
        "④ 台词时长预算（F21 配套）：对白朗读时长 ≈ 总字数÷4字/秒，"
        "必须 ≤ 单集目标时长的 75%——30 秒规格全剧对白 ≤ 90 字（6~8 行、每行 ≤ 15 字、"
        "narration ≤ 2 行）；60 秒规格 ≤ 180 字；2 分钟规格 ≤ 360 字。"
        "action/stage_direction 行不计入配读预算；\n"
        "⑤ 场景完整性（剧情一致性治本项）：每个场次的 summary 必须包含"
        "「时间地点人物 + 触发事件 + 情绪反应」三要素，且明确写出与上一场次的"
        "因果衔接（如「承接上场 X 的 Y，所以 Z」），场次之间不允许无因果跳转；\n"
        "⑥ 因果链自审：输出前逐场检查「上一场结果 → 本场触发 → 本场反应」是否成立，"
        "发现断裂（跳切、无动机转场、人物凭空出现）必须重写该场，而不是靠观众脑补。\n"
        f"{_VISUAL_CRAFT}"
        "\n\n"
        "【第二段 · 项目上下文】\n"
        f"{_project_context(state)}\n"
        f"- 剧名：{outline.get('title')}\n"
        f"- 全剧梗概：{outline.get('logline')}\n"
        f"- 人物清单（对白必须出自以下人物）：\n{character_lines}"
        + (f"\n- 前情摘要：\n{prev_lines}" if prev_lines else "")
        + "\n\n"
        "【第三段 · 工作区约定】\n"
        "你的产物由编排节点代写入 /project/script/ep{n}.md，你只需按契约输出 JSON。\n"
        "禁止：直接写数据库、调用渲染/计费工具。\n\n"
        "【第四段 · 预算与边界】\n"
        f"{_budget_and_limits(state)}\n\n"
        "【第五段 · 输出契约】\n"
        "契约字段填写口径（逐项照做，否则视为不合规）：\n"
        "- location：场景类型+时间+氛围完整描述（≤255 字），同场次多画面保持一致；\n"
        "- summary：以「承接：上一场的XX结果导致本场…」开头，再写触发事件与情绪反应；\n"
        "- lines[action]：具象动作+表情+身体姿态 +「道具：…」+「机位：…」，四项不得缺失；\n"
        "- lines[dialogue]：speaker 只能用人物清单里的原名；语气只写在 emotion 字段，"
        "content 内不得再加「（冷淡）」等语气括号，也不得以「角色名：」开头；\n"
        "- lines[stage_direction]：只用于换装/转场/时间跳跃等画外信息，必须写明变更内容；\n"
        "- lines[narration]：≤2 行，仅作画外音。\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释文字），结构如下：\n"
        f"{_EPISODE_CONTRACT}"
    )
    user = (
        f"请产出第 {episode_plan.get('episode_no')} 集《{episode_plan.get('title')}》的剧本。\n"
        f"本集计划：{episode_plan.get('synopsis')}\n"
        f"结尾钩子：{episode_plan.get('hook')}\n"
        f"{mode_hint}\n"
        "只输出符合契约的 JSON。"
    )
    return system, user


def episode_to_markdown(outline: dict[str, Any], episode: dict[str, Any]) -> str:
    """把单集剧本契约产物渲染为 Markdown 剧本文本（project_files 落盘用）。

    Args:
        outline: 大纲契约产物（取剧名）。
        episode: 单集剧本契约产物（EpisodeScript dict）。

    Returns:
        Markdown 文本（含场次头与行内容）。
    """
    head = (
        f"# 《{outline.get('title')}》第 {episode.get('episode_no')} 集：{episode.get('title')}\n\n"
        f"> 概要：{episode.get('summary')}\n>\n> 结尾钩子：{episode.get('hook')}\n"
    )
    body_lines: list[str] = []
    for scene in episode.get("scenes", []):
        tod = scene.get("time_of_day") or ""
        ie = (scene.get("int_ext") or "").upper()
        body_lines.append(
            f"\n## 场 {scene.get('scene_no')} {ie} {scene.get('location')} - {tod}\n"
            f"{scene.get('summary')}\n"
        )
        for line in scene.get("lines", []):
            lt = line.get("line_type")
            if lt == "dialogue":
                speaker = line.get("speaker") or "?"
                emotion = line.get("emotion")
                suffix = f"（{emotion}）" if emotion else ""
                body_lines.append(f"**{speaker}**{suffix}：{line.get('content')}")
            elif lt == "narration":
                body_lines.append(f"【旁白】{line.get('content')}")
            elif lt == "stage_direction":
                body_lines.append(f"〔{line.get('content')}〕")
            else:
                body_lines.append(f"*{line.get('content')}*")
    return head + "\n".join(body_lines) + "\n"


def structure_json_text(structure: dict[str, Any]) -> str:
    """把结构化剧本树序列化为 project_files 登记用的 JSON 文本。

    Args:
        structure: 结构化剧本树（episodes_to_structure 产物）。

    Returns:
        缩进 2、保留中文的 JSON 字符串。
    """
    return json.dumps(structure, ensure_ascii=False, indent=2)
