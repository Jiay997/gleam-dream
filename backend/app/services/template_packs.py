"""模板包与分镜模板内置库（V4.0 US-F2-09 / US-F4-10）。

两类模板：

1. **剧本组合模板包**（``SCRIPT_PACKS``）：题材 × 画风组合，携带默认单集字数、
   节奏倾向、结局走向与 1 句适用说明（AC-F2-09-1）。套用后写入
   ``script_template_refs`` 参数快照，模板下线不影响历史项目（AC-F2-09-3）。
2. **分镜模板**（``BUILTIN_STORYBOARD_TEMPLATES``）：对话戏 / 动作戏 / 转场，
   每条模板为「机位规则条目」（≤10 条、每条 ≤50 字），注入导演 Agent 策略
   上下文（AC-F4-10-1）；与硬性质检规则冲突时硬校验优先。

模板为**只读常量**：不下沉数据库，避免内置内容被误改；用户自定义分镜模板
落 ``storyboard_templates`` 表（上限 20 个，AC-F4-10-2）。
"""

from __future__ import annotations

from typing import Any, Final

#: 剧本模板包（≥8 个，覆盖主流短剧题材 × 画风组合）
SCRIPT_PACKS: Final[list[dict[str, Any]]] = [
    {
        "template_id": "urban_romance__live_action",
        "name": "都市甜宠 · 真人感",
        "genre": "urban_romance",
        "art_style": "live_action",
        "word_target": 1200,
        "episode_target": 10,
        "pacing": "balanced",
        "ending": "happy",
        "description": "都市情感高频爆点，真人质感适合投流素材复用。",
    },
    {
        "template_id": "urban_romance__anime",
        "name": "都市甜宠 · 动漫",
        "genre": "urban_romance",
        "art_style": "anime",
        "word_target": 1000,
        "episode_target": 12,
        "pacing": "fast",
        "ending": "happy",
        "description": "年轻向甜宠，动漫画风规避真人肖像授权成本。",
    },
    {
        "template_id": "suspense__live_action",
        "name": "悬疑推理 · 真人感",
        "genre": "suspense",
        "art_style": "live_action",
        "word_target": 1500,
        "episode_target": 12,
        "pacing": "balanced",
        "ending": "twist",
        "description": "强钩子悬疑，适合连载留存与评论互动。",
    },
    {
        "template_id": "suspense__chinese_style",
        "name": "悬疑推理 · 国风",
        "genre": "suspense",
        "art_style": "chinese_style",
        "word_target": 1400,
        "episode_target": 10,
        "pacing": "slow",
        "ending": "open",
        "description": "国风悬疑气质独特，适合差异化题材竞争。",
    },
    {
        "template_id": "counterattack__live_action",
        "name": "逆袭爽剧 · 真人感",
        "genre": "counterattack",
        "art_style": "live_action",
        "word_target": 1100,
        "episode_target": 15,
        "pacing": "fast",
        "ending": "happy",
        "description": "强反转快节奏，单集钩子密度最高，适合冲量。",
    },
    {
        "template_id": "rebirth__chinese_style",
        "name": "重生复仇 · 国风",
        "genre": "rebirth",
        "art_style": "chinese_style",
        "word_target": 1300,
        "episode_target": 12,
        "pacing": "fast",
        "ending": "happy",
        "description": "重生流复仇叙事，国风美术强化记忆点。",
    },
    {
        "template_id": "family_ethics__live_action",
        "name": "家庭伦理 · 真人感",
        "genre": "family_ethics",
        "art_style": "live_action",
        "word_target": 1600,
        "episode_target": 10,
        "pacing": "slow",
        "ending": "bittersweet",
        "description": "中老年向家庭冲突，情绪浓度高、完播率稳定。",
    },
    {
        "template_id": "fantasy__three_d",
        "name": "玄幻仙侠 · 3D",
        "genre": "fantasy",
        "art_style": "three_d",
        "word_target": 1200,
        "episode_target": 12,
        "pacing": "balanced",
        "ending": "open",
        "description": "玄幻世界观，3D 画风降低大场面实拍成本。",
    },
    {
        "template_id": "workplace__anime",
        "name": "职场奋斗 · 动漫",
        "genre": "workplace",
        "art_style": "anime",
        "word_target": 1000,
        "episode_target": 10,
        "pacing": "balanced",
        "ending": "happy",
        "description": "轻喜剧职场，动漫画风兼容夸张表演。",
    },
    {
        "template_id": "campus__anime",
        "name": "校园青春 · 动漫",
        "genre": "campus",
        "art_style": "anime",
        "word_target": 900,
        "episode_target": 10,
        "pacing": "balanced",
        "ending": "bittersweet",
        "description": "校园青春群像，情绪细腻、二创延展性强。",
    },
]

#: 模板包索引（按 template_id 快速查找）
SCRIPT_PACK_INDEX: Final[dict[str, dict[str, Any]]] = {
    pack["template_id"]: pack for pack in SCRIPT_PACKS
}

#: 套用模板时可回填并可能被手改（偏离）的字段
TEMPLATE_MUTABLE_FIELDS: Final[tuple[str, ...]] = (
    "genre",
    "art_style",
    "word_target",
    "episode_target",
    "pacing",
    "ending",
)

#: 内置分镜模板（对话戏 / 动作戏 / 转场），rules ≤10 条且每条 ≤50 字
BUILTIN_STORYBOARD_TEMPLATES: Final[list[dict[str, Any]]] = [
    {
        "template_id": "builtin_dialogue",
        "name": "对话戏",
        "kind": "dialogue",
        "description": "双人以上对话的标准机位与越轴规避。",
        "rules": [
            "对话以过肩中近景为主，正反打交替，避免同侧跳切。",
            "每段对话首镜用中景建立空间关系，再切近景。",
            "情绪转折点切特写，时长不超过 2000ms。",
            "三人以上对话用宽景交代站位，再分组正反打。",
            "保持 180 度轴线，越轴需插入过渡空镜。",
            "台词长度超过 6 秒时插入听者反应镜头。",
        ],
    },
    {
        "template_id": "builtin_action",
        "name": "动作戏",
        "kind": "action",
        "description": "打斗与追逐的高信息密度机位组合。",
        "rules": [
            "动作段落镜头均值控制在 2000-3000ms，逐镜加速。",
            "击打瞬间用特写或插入镜头，避免全景拖慢节奏。",
            "追逐步入使用跟拍与手持，制造临场感。",
            "动作方向保持一致，避免左右反转造成错乱。",
            "每个动作组合后给一个短暂停顿反应镜头。",
            "高潮前用一次快速推镜强化冲击。",
        ],
    },
    {
        "template_id": "builtin_transition",
        "name": "转场",
        "kind": "transition",
        "description": "场次切换与时间跳跃的过渡处理。",
        "rules": [
            "同场景时间跳跃用空镜或道具特写过渡。",
            "跨场景切换优先用动作接动作的匹配剪辑。",
            "情绪切换处允许一次 500ms 的黑场留白。",
            "集末钩子镜头保持 2500ms 以上不给字幕遮挡。",
        ],
    },
]

#: 分镜模板索引
BUILTIN_STORYBOARD_INDEX: Final[dict[str, dict[str, Any]]] = {
    tpl["template_id"]: tpl for tpl in BUILTIN_STORYBOARD_TEMPLATES
}

#: 分镜模板规则条目上限（AC-F4-10-1）
MAX_TEMPLATE_RULES: Final[int] = 10

#: 单条规则字数上限（AC-F4-10-1）
MAX_RULE_LENGTH: Final[int] = 50

#: 用户自定义分镜模板上限（AC-F4-10-2）
MAX_CUSTOM_TEMPLATES: Final[int] = 20

#: 用户导出预设上限（AC-F7-01-3）
MAX_EXPORT_PRESETS: Final[int] = 10

#: 资产标签上限（AC-F3-11-2）
MAX_ASSET_LABELS: Final[int] = 10

#: 单标签字数上限（AC-F3-11-2）
MAX_LABEL_LENGTH: Final[int] = 12

#: 画布操作历史保留步数（AC-F4-09-1）
MAX_OP_HISTORY: Final[int] = 100

#: 每批次免费插队次数（AC-F5-01-3）
MAX_QUEUE_JUMPS: Final[int] = 3

#: 单镜头静帧重出上限（AC-F4-03-3）
MAX_REDRAFTS: Final[int] = 3

#: 剧本版本数上限（AC-F2-11-3）
MAX_SCRIPT_VERSIONS: Final[int] = 30


def list_script_packs(include_inactive: bool = False) -> list[dict[str, Any]]:
    """返回剧本组合模板包列表。

    Args:
        include_inactive: 是否包含已下线模板（默认不含）。

    Returns:
        模板包字典列表（字段含 template_id / name / genre / art_style /
        word_target / episode_target / pacing / ending / description /
        is_active）。
    """
    packs: list[dict[str, Any]] = []
    for pack in SCRIPT_PACKS:
        is_active = bool(pack.get("is_active", True))
        if not is_active and not include_inactive:
            continue
        packs.append({**pack, "is_active": is_active})
    return packs


def get_script_pack(template_id: str) -> dict[str, Any] | None:
    """按 ID 取剧本模板包。

    Args:
        template_id: 模板包 ID。

    Returns:
        模板包字典；不存在返回 None。
    """
    pack = SCRIPT_PACK_INDEX.get(template_id)
    if pack is None:
        return None
    return {**pack, "is_active": bool(pack.get("is_active", True))}


def compute_deviated_fields(
    applied: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    """比较模板套用参数与项目当前参数，返回被手改（偏离预设）的字段名。

    Args:
        applied: 套用时的参数快照。
        current: 项目当前参数值。

    Returns:
        偏离字段名列表（顺序同 :data:`TEMPLATE_MUTABLE_FIELDS`）。
    """
    deviated: list[str] = []
    for field in TEMPLATE_MUTABLE_FIELDS:
        if field not in applied:
            continue
        if applied.get(field) != current.get(field):
            deviated.append(field)
    return deviated


def list_storyboard_templates() -> list[dict[str, Any]]:
    """返回内置分镜模板列表。

    Returns:
        内置模板字典列表（含 rules 条目数组）。
    """
    return [{**tpl, "is_builtin": True} for tpl in BUILTIN_STORYBOARD_TEMPLATES]


def get_storyboard_template(template_id: str) -> dict[str, Any] | None:
    """按 ID 取内置分镜模板。

    Args:
        template_id: 模板 ID。

    Returns:
        内置模板字典；不存在返回 None。
    """
    tpl = BUILTIN_STORYBOARD_INDEX.get(template_id)
    return dict(tpl) if tpl else None


def validate_template_rules(rules: list[str]) -> list[str]:
    """校验分镜模板规则条目（数量 / 单条字数 / 空条目）。

    Args:
        rules: 规则条目列表。

    Returns:
        校验通过的清洗后规则列表。

    Raises:
        ValueError: 条目数 >10、单条 >50 字或存在空条目。
    """
    cleaned = [str(rule).strip() for rule in rules]
    if not cleaned:
        raise ValueError("模板至少需要 1 条机位规则")
    if len(cleaned) > MAX_TEMPLATE_RULES:
        raise ValueError(f"机位规则最多 {MAX_TEMPLATE_RULES} 条")
    for rule in cleaned:
        if not rule:
            raise ValueError("机位规则不能为空")
        if len(rule) > MAX_RULE_LENGTH:
            raise ValueError(f"单条机位规则不超过 {MAX_RULE_LENGTH} 字")
    return cleaned
