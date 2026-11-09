"""Agent 输出契约模块（对齐技术方案 §5.3）。

以 pydantic 模型承载各 Agent 输出契约（JSON Schema 同源）：

- :class:`WriteTodos`：编剧计划清单（write_todos 规划工具输出）；
- :class:`Outline`：编剧大纲契约（剧名/梗概/人物/分集计划）；
- :class:`EpisodeScript`：单集剧本契约（集/场/行三级结构）；
- :class:`ComplianceReport`：审核 Agent 契约（verdict + findings）。

用途：
1. 节点内对 LLM 输出做 JSON Schema 校验（非法输出打回自修，≤2 次）；
2. 校验通过后的产物即为落库与 State 传递的规范结构。

约束对齐：line_type 4 值、emotion 8 值、time_of_day 6 值均与
00 契约 §6.7 及 scripts 域 CHECK 约束一致（DB 落库时 time_of_day
按 :data:`TIME_OF_DAY_DB_MAP` 收敛为 4 值）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---- 行类型 4 值（script_lines.line_type CHECK） ----
LineType = Literal["dialogue", "narration", "action", "stage_direction"]

# ---- 情绪标签 8 值（script_lines.emotion_tag CHECK） ----
EmotionTag = Literal["calm", "joy", "anger", "sadness", "fear", "surprise", "tension", "romance"]

# ---- 时段 6 值（编剧输出契约；落库时收敛为 DB 4 值） ----
TimeOfDay = Literal["dawn", "morning", "noon", "afternoon", "dusk", "night"]

# ---- 时段 6 值 -> DB 4 值（scenes.time_of_day CHECK: day/night/dawn/dusk）映射 ----
TIME_OF_DAY_DB_MAP: dict[str, str] = {
    "dawn": "dawn",
    "morning": "day",
    "noon": "day",
    "afternoon": "day",
    "dusk": "dusk",
    "night": "night",
}


class TodoItem(BaseModel):
    """编剧计划条目（write_todos 单项）。

    Attributes:
        step: 步骤序号（≥1）。
        title: 步骤标题（如"产出人物小传"）。
        detail: 步骤说明（可空）。
    """

    step: int = Field(ge=1, description="步骤序号")
    title: str = Field(min_length=1, max_length=100, description="步骤标题")
    detail: str | None = Field(default=None, max_length=300, description="步骤说明")


class WriteTodos(BaseModel):
    """编剧计划清单契约（大纲→人物→分集→对白的可视化计划）。

    Attributes:
        todos: 计划条目列表（≥2 条）。
    """

    todos: list[TodoItem] = Field(min_length=2, description="计划条目列表")


class Character(BaseModel):
    """人物小传（大纲契约子结构）。

    Attributes:
        name: 人物名。
        role: 角色定位 protagonist/antagonist/supporting/extra。
        appearance: 外形与着装描述（分镜/资产生成依据）。
        age_range: 年龄段描述。
        gender: 性别（可省略）。
        bio: 人物小传（可省略）。
    """

    name: str = Field(min_length=1, max_length=64, description="人物名")
    role: Literal["protagonist", "antagonist", "supporting", "extra"] = Field(description="角色定位")
    appearance: str = Field(min_length=1, max_length=500, description="外形描述")
    age_range: str = Field(min_length=1, max_length=32, description="年龄段")
    gender: Literal["male", "female", "other", "unspecified"] = Field(
        default="unspecified", description="性别"
    )
    bio: str | None = Field(default=None, max_length=500, description="人物小传")


class EpisodePlan(BaseModel):
    """分集计划（大纲契约子结构，每集一句话规划）。

    Attributes:
        episode_no: 集号（≥1）。
        title: 集标题。
        synopsis: 集内容简介。
        hook: 结尾钩子描述。
    """

    episode_no: int = Field(ge=1, description="集号")
    title: str = Field(min_length=1, max_length=60, description="集标题")
    synopsis: str = Field(min_length=1, max_length=500, description="集简介")
    hook: str = Field(min_length=1, max_length=200, description="结尾钩子")


class Outline(BaseModel):
    """编剧大纲输出契约（generate_outline 节点产物）。

    Attributes:
        title: 剧名。
        genre: 题材。
        logline: 一句话梗概（≤300 字）。
        characters: 人物小传列表（≥1）。
        episode_plans: 分集计划列表（≥1，与目标集数一致）。
        write_todos: 编剧计划清单。
    """

    title: str = Field(min_length=1, max_length=60, description="剧名")
    genre: str = Field(min_length=1, max_length=32, description="题材")
    logline: str = Field(min_length=1, max_length=300, description="一句话梗概")
    characters: list[Character] = Field(min_length=1, description="人物小传列表")
    episode_plans: list[EpisodePlan] = Field(min_length=1, description="分集计划列表")
    write_todos: WriteTodos = Field(description="编剧计划清单")

    @field_validator("episode_plans")
    @classmethod
    def episode_plans_no_unique(cls, v: list[EpisodePlan]) -> list[EpisodePlan]:
        """校验分集计划集号唯一且升序。

        Args:
            v: 分集计划列表。

        Returns:
            原列表（校验通过时）。

        Raises:
            ValueError: 集号重复或未按升序排列。
        """
        nos = [plan.episode_no for plan in v]
        if len(set(nos)) != len(nos):
            raise ValueError("episode_plans 存在重复集号")
        if nos != sorted(nos):
            raise ValueError("episode_plans 集号须按升序排列")
        return v


class ScriptLineContract(BaseModel):
    """剧本行契约（台词/旁白/动作/舞台提示）。

    Attributes:
        line_type: 行类型（4 值）。
        speaker: 说话人（dialogue 必填，其余可空）。
        content: 行内容（≤500 字）。
        emotion: 情绪标签（8 值或省略）。
    """

    line_type: LineType = Field(description="行类型")
    speaker: str | None = Field(default=None, max_length=64, description="说话人")
    content: str = Field(min_length=1, max_length=500, description="行内容")
    emotion: EmotionTag | None = Field(default=None, description="情绪标签")


class SceneContract(BaseModel):
    """场次契约（集内时间/地点连续单元）。

    Attributes:
        scene_no: 场号（集内唯一，≥1）。
        location: 地点。
        time_of_day: 时段（6 值，落库时映射为 4 值）。
        int_ext: 内外景 int/ext（可省略）。
        summary: 场摘要。
        lines: 剧本行列表（≥1）。
    """

    scene_no: int = Field(ge=1, description="场号")
    location: str = Field(min_length=1, max_length=255, description="地点")
    time_of_day: TimeOfDay = Field(description="时段")
    int_ext: Literal["int", "ext"] | None = Field(default=None, description="内外景")
    summary: str = Field(min_length=1, max_length=500, description="场摘要")
    lines: list[ScriptLineContract] = Field(min_length=1, description="剧本行列表")

    @field_validator("location")
    @classmethod
    def location_visual_complete(cls, v: str) -> str:
        """校验 location 是否包含足够的视觉信息（禁止裸地点）。

        AI 画面生成只能看见写出的场景描述；「咖啡厅」这类裸地点会让
        分镜/渲染阶段自由发挥，同一场戏生成成完全不同的空间。这里用
        高置信的短文本阈值强制 LLM 自修（提示词已要求「场景类型+时间+
        氛围」三要素）。

        Args:
            v: 原始 location 文本。

        Returns:
            去除首尾空白后的 location。

        Raises:
            ValueError: location 过短（疑似裸地点）。
        """
        text = str(v or "").strip()
        if len(text) < 6:
            raise ValueError(
                "location 必须是「场景类型+时间+氛围」完整描述（如"
                "「现代都市咖啡馆，午后，暖光透过落地窗，背景有少量顾客」），"
                "禁止只写裸地点（如「咖啡厅」）"
            )
        return text


class EpisodeScript(BaseModel):
    """单集剧本输出契约（generate_episodes 节点单次调用产物）。

    Attributes:
        episode_no: 集号（≥1）。
        title: 集标题。
        hook: 结尾钩子。
        summary: 集简介。
        scenes: 场次列表（≥1）。
    """

    episode_no: int = Field(ge=1, description="集号")
    title: str = Field(min_length=1, max_length=60, description="集标题")
    hook: str = Field(min_length=1, max_length=200, description="结尾钩子")
    summary: str = Field(min_length=1, max_length=500, description="集简介")
    scenes: list[SceneContract] = Field(min_length=1, description="场次列表")

    @field_validator("scenes")
    @classmethod
    def scene_no_unique(cls, v: list[SceneContract]) -> list[SceneContract]:
        """校验场号在集内唯一且升序。

        Args:
            v: 场次列表。

        Returns:
            原列表（校验通过时）。

        Raises:
            ValueError: 场号重复或未按升序排列。
        """
        nos = [scene.scene_no for scene in v]
        if len(set(nos)) != len(nos):
            raise ValueError("scenes 存在重复场号")
        if nos != sorted(nos):
            raise ValueError("scenes 场号须按升序排列")
        return v


# ---- 审核结论 3 值 ----
ComplianceVerdict = Literal["pass", "flag", "block"]

# ---- 违规类型 8 值（compliance_flags.flag_type CHECK） ----
FlagType = Literal[
    "politics", "porn", "minor", "violence", "infringement",
    "sensitive_word", "celebrity_face", "real_person_likeness",
]


class ComplianceFinding(BaseModel):
    """审核发现（单条风险项）。

    Attributes:
        flag_type: 违规类型（8 值）。
        level: 风险等级 low/medium/high/block。
        location: 位置定位（如 "第 1 集 场 2"）。
        description: 风险描述。
        suggestion: 弱化/修改建议。
    """

    flag_type: FlagType = Field(description="违规类型")
    level: Literal["low", "medium", "high", "block"] = Field(description="风险等级")
    location: str = Field(min_length=1, max_length=200, description="位置定位")
    description: str = Field(min_length=1, max_length=500, description="风险描述")
    suggestion: str = Field(min_length=1, max_length=500, description="修改建议")


class ComplianceReport(BaseModel):
    """审核 Agent 输出契约（compliance_review 节点产物）。

    verdict 语义：
    - ``pass``：无风险，放行；
    - ``flag``：存在中低风险，首轮自动弱化重审一次，末轮降级放行（记录 review）；
    - ``block``：高危违规，终止流程且不提供绕过。

    Attributes:
        verdict: 审核结论（3 值）。
        findings: 风险发现列表（pass 时可为空）。
        summary: 审核摘要一句话。
    """

    verdict: ComplianceVerdict = Field(description="审核结论")
    findings: list[ComplianceFinding] = Field(default_factory=list, description="风险发现列表")
    summary: str = Field(min_length=1, max_length=300, description="审核摘要")


def contract_error_text(exc: Exception) -> str:
    """把 pydantic 校验异常压缩为可回填提示词的错误文本。

    Args:
        exc: pydantic ValidationError 或 JSON 解析异常。

    Returns:
        单行错误描述（截断到 2000 字符，避免撑爆上下文）。
    """
    text = str(exc).replace("\n", "; ")
    return text[:2000]


def episodes_to_structure(outline: dict[str, Any], episodes: list[dict[str, Any]]) -> dict[str, Any]:
    """把大纲与逐集剧本合并为 scripts.structure 结构化 JSONB。

    Args:
        outline: 大纲契约产物（title/logline/characters/episode_plans）。
        episodes: 逐集剧本契约产物列表（EpisodeScript dict）。

    Returns:
        结构化剧本树 {title, logline, genre, characters, episodes: [...]}，
        episodes 元素含 title/hook/summary/scenes（行内保留原契约字段）。
    """
    return {
        "title": outline.get("title"),
        "logline": outline.get("logline"),
        "genre": outline.get("genre"),
        "characters": outline.get("characters", []),
        "episodes": [
            {
                "episode_no": ep.get("episode_no"),
                "title": ep.get("title"),
                "hook": ep.get("hook"),
                "summary": ep.get("summary"),
                "scenes": ep.get("scenes", []),
            }
            for ep in episodes
        ],
    }
