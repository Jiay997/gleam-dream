"""视频动态性服务模块（生成 prompt 三要素 / 运动 QC / 尾帧续接）。

背景：分镜视频长期呈"PPT 放映"效果，根因有三：

1. 生成 prompt 只写外貌与场景，没有人物动作、镜头运动、时序变化；
2. 渲染链路实际回退到 stub 图片推拉，而非真实图生视频（I2V）；
3. QC 只做人脸伪向量与时长校验，没有任何画面运动验收。

本模块提供三组能力，供分镜生成、渲染执行器与质检环节复用：

- :func:`build_motion_prompt`：把人物动作、运镜、时序变化三类要素写入
  生成 prompt；QC 判定静态后按轮次逐级强化动作幅度；
- :func:`analyze_video_motion`：ffmpeg 抽样帧 + 灰度帧差统计，输出
  首-中-尾差异、平均差异与最长静止窗口，用于自动判定静态镜头；
- :func:`extract_last_frame` / :func:`image_to_data_uri`：上一镜尾帧
  续接（保持连贯）与 DashScope I2V data URI 编码。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

logger = logging.getLogger("gleamdream.motion")

#: 单镜头时长合规区间（毫秒）：短剧单镜 3–5 秒
SHOT_DURATION_MIN_MS: int = 3000
SHOT_DURATION_MAX_MS: int = 5000
#: 运动 QC 抽样帧率（帧/秒）；4fps 可覆盖 250ms 粒度
MOTION_SAMPLE_FPS: int = 4
#: 运动 QC 判定阈值（灰度 0–255 的平均绝对差）
MOTION_MIN_MEAN_DIFF: float = 4.0
MOTION_MIN_KEY_FRAME_DIFF: float = 2.5
#: 相邻抽样帧差异低于该值视为"静止"；连续静止超过 2 秒判定不合格
MOTION_STATIC_DIFF: float = 1.5
MOTION_MAX_STATIC_MS: int = 2000
#: 三帧验收存证的默认落盘目录（backend/.run/review）
_EVIDENCE_DIR: Path = Path(__file__).resolve().parents[2] / ".run" / "review"

#: 运镜枚举 → prompt 镜头语言（2026-11 修复景别跳变）：
#: 旧短语「跟随人物移动 / 逐步靠近人物」没有给出幅度上限，I2V 模型会在
#: 4 秒内把全景推成特写、并跟着人物把背景换掉（shot 6/7 实测）。这里统一
#: 改为"幅度受控"表述，让运镜只提供轻微镜头感，不改变景别与地点。
CAMERA_MOVE_PHRASES: dict[str, str] = {
    "static": "机位固定不动（static），仅保留极轻微呼吸感，画面变化主要来自人物动作与光线",
    "pan": "镜头小幅水平摇移（pan），幅度克制，人物始终留在画面同一区域",
    "tilt": "镜头小幅垂直摇移（tilt），幅度克制，不改变人物在画面中的大小",
    "dolly_in": (
        "镜头极缓慢推近（dolly in），推近幅度不超过画面的 8%，景别保持不变，"
        "视野不得扩大：不露出首帧之外的门窗、街道或其他房间"
    ),
    "dolly_out": (
        "镜头极缓慢拉远（dolly out），拉远幅度不超过画面的 8%，景别保持不变，"
        "视野不得扩大：不露出首帧之外的门窗、街道或其他房间"
    ),
    "tracking": (
        "镜头小幅横移跟随（tracking），跟拍幅度不超过人物半步，景别与背景保持不变，"
        "不横向扫出首帧以外的空间"
    ),
    "orbit": (
        "镜头小幅环绕（orbit），环绕角度不超过 15 度，人物大小与背景保持不变，"
        "不环绕出首帧以外的空间"
    ),
    "handheld": "手持镜头轻微晃动（handheld），抖动幅度克制，景别稳定",
    "crane": "镜头小幅升降（crane），升降幅度克制，不改变景别与地点",
}

#: 景别锁定句：按 shot_size 固定镜头内的构图，禁止 I2V 在镜头内推拉变景别
#: 或切换主体/地点。这是"镜头内景别跳变"的核心约束。
_FRAMING_LOCKS: dict[str, str] = {
    "extreme_wide": (
        "全程保持远景构图，人物在画面中的大小与位置不变，不推近、不切特写，"
        "场景背景与首帧一致"
    ),
    "wide": (
        "全程保持全景构图，人物全身可见且大小不变，不推成中景/近景/特写，"
        "场景背景与首帧一致"
    ),
    "medium": (
        "全程保持中景构图，人物腰部以上，不推成面部特写，场景背景与首帧一致"
    ),
    "close_up": (
        "全程保持近景构图，人物头部到胸口，不拉远成全身，不切换到其他人物"
    ),
    "extreme_close_up": (
        "全程保持面部特写构图，不拉远、不切换到其他人或地点"
    ),
    "over_shoulder": (
        "全程保持过肩构图，前景肩背位置与首帧一致，不切换到正面机位"
    ),
}
#: 景别兜底（shot_size 缺失/未识别时按中景锁定）
_DEFAULT_FRAMING_LOCK = _FRAMING_LOCKS["medium"]

#: 导演批注（机位/运镜）在动作行里的残留标记。分镜层已把它解析成
#: camera_move，原文再进画面 prompt 会让模型自行"跟随/推近"，需清洗。
_DIRECTOR_ANNOTATION_RE = re.compile(r"[，,；;。]?\s*(?:机位|相机|运镜)\s*[：:][^。；，]*")

#: 动作动词词表：用于判断 prompt/action_desc 是否真的描述了"动作"
ACTION_VERBS: tuple[str, ...] = (
    "抬手", "转身", "走近", "后退", "低头", "抬头", "伸手", "握住", "推开",
    "拉门", "坐下", "起身", "奔跑", "回头", "微笑", "皱眉", "落泪", "拥抱",
    "递给", "接过", "跑", "走", "站起", "拿起", "放下", "看向", "说", "喊",
    "哭", "笑", "颤抖", "攥紧", "翻开", "合上", "点燃", "熄灭", "靠近",
    "离开", "蹲下", "扶住", "摇头", "点头", "侧身", "俯身", "迈步", "挥手",
    "倒下", "抱住", "擦", "写", "喝", "吃", "穿过", "停下", "弯腰", "捡起",
    "打开", "关上", "递", "接", "抱", "拉", "推", "拽", "拍", "敲",
)

#: 无动作描述时的中性兜底（2026-11 修复：不再按情绪编造具体动作）。
#: 历史实现会按 emotion 注入「猛然抬头/后退半步/迈出一步」等动作，与剧本
#: 写的「坐着说话」直接冲突，导致台词与肢体对不上；这里只描述细微变化，
#: 具体动作必须由剧本动作行提供。
_NEUTRAL_ACTION = "人物或画面主体保持剧本描述的姿态，随呼吸、视线与光线产生细微变化"

#: QC 重试时的逐轮强化指令（第 1/2/3 次重试各取一条）。
#: 2026-11 修复：强化词只描述「幅度/节奏/连续性」，不指定具体动作，
#: 避免重试轮把剧本里的动作改写成另一套剧情。
_STRENGTHEN_PHRASES: tuple[str, ...] = (
    "动作幅度略微加大，肢体与衣摆随动作自然摆动，保持画面连续运动",
    "动作节奏更清晰，人物重心有可见变化，避免任何静止定格",
    "画面持续运动：人物按剧本描述完成动作，运镜同步推进，不出现定格帧",
)

#: 承载"可读文字"的细节词：I2V 遇到这些描述会强行渲染具体文字并写错
#: （验收实测转账列表「-200000」重复六行、合同条款与中文全部乱码）。
#: 命中时在动作句后追加"内容虚化"提示，减少模型写字冲动。
_TEXT_CONTENT_MARKERS: tuple[str, ...] = (
    "转账记录", "记录", "合同", "条款", "签名", "账单", "名单", "字幕",
    "字条", "便签", "文件", "金额", "数字", "表格", "屏幕", "界面", "聊天",
    "账本", "报表", "二维码", "价签",
)

_TEMPORAL_TEMPLATE = (
    "动作在画面内完整发生：起始蓄势→中段动作展开→结尾收势，"
    "人物表情与姿态持续变化，画面无静止定格"
)
_QUALITY_SUFFIX = (
    "人物面部与服装保持前后一致，动作自然连贯，避免肢体畸变与定格画面，"
    "整段画面保持与首帧完全一致的真实摄影质感与色调，镜头内不切换画风，"
    "不出现插画/二次元/3D 混搭，画面中不出现任何文字、字幕、水印或logo，"
    # 2026-11 修复（景别跳变）：I2V 常在镜头内推成特写/换背景，这里显式
    # 锁定构图与地点，配合景别锁定句做双重约束。
    "整段镜头保持首帧的景别、机位与场景背景：不推成特写、不拉成远景，"
    "不切换人物或地点，背景陈设与光线方向保持不变，"
    # 2026-11 QA 修复（镜内时段/人群突变）：验收实测首帧昏黄傍晚 → 尾帧
    # 白天明亮并凭空出现路人，这里把时段、光线与背景人数一并锁死。
    "整段光线与时段保持不变（不出现日夜切换、不突然变亮或变暗），"
    "背景人群数量与陈设保持不变，不新增路人或围观者，"
    # 2026-11 QA 修复（小字号文字乱码）：仅靠"不要出现文字"的否定指令时，
    # I2V 仍会去画招牌/合同/转账列表并把字写错（-200000 重复、条款乱码）。
    # 改为正面指令——这些平面保持空白/纹理，不给模型"写字"的任务。
    "纸张、合同、手机屏幕、招牌与包装表面保持空白或仅有轻微材质纹理，"
    "不渲染任何可辨认的字符、数字、表格线或界面图标"
)

#: 旧版质量尾巴的起始标记（历史 final_prompt 已包含），重建时先剥离再追加，
#: 避免同一段约束在 prompt 里出现两次、稀释景别锁定。
_QUALITY_MARKER = "人物面部与服装保持前后一致"


def _strip_director_annotations(text: str) -> str:
    """清洗动作文本中残留的「机位：跟随」类导演批注。

    Args:
        text: 动作描述或基础 prompt。

    Returns:
        清洗后的文本（保留「道具：…」等叙事标注）。
    """
    cleaned = _DIRECTOR_ANNOTATION_RE.sub("", str(text or ""))
    return cleaned.strip(" ，,；;。")


def _replace_prompt_segment(text: str, label: str, content: str) -> str:
    """把 prompt 中已有的「label：…」段替换为新内容。

    历史 final_prompt 会把旧版运镜短语写死在里面，仅追加新段落会与旧指令
    冲突；这里做原地替换，保证以最新受控短语为准。

    Args:
        text: 基础 prompt。
        label: 段标签（如「镜头运动」）。
        content: 新的段内容（不含标签）。

    Returns:
        替换后的 prompt；不存在该段时原样返回。
    """
    pattern = re.compile(rf"{label}：.*?(?=。|$)")
    if not pattern.search(text):
        return text
    return pattern.sub(f"{label}：{content}", text, count=1)


def _strip_quality_suffix(text: str) -> str:
    """剥离基础 prompt 尾部的旧版质量约束段。

    Args:
        text: 基础 prompt。

    Returns:
        剥离后的 prompt；不含旧段时原样返回。
    """
    index = text.find(_QUALITY_MARKER)
    if index < 0:
        return text
    return text[:index].rstrip(" 。")


def has_action_verb(text: str | None) -> bool:
    """判断文本是否包含具体动作动词。

    Args:
        text: 待检测文本（prompt 或 action_desc）。

    Returns:
        命中任一动作动词返回 True。
    """
    blob = str(text or "")
    return any(verb in blob for verb in ACTION_VERBS)


def camera_phrase(camera_move: str | None) -> str:
    """获取运镜枚举对应的镜头语言描述。

    Args:
        camera_move: 运镜枚举（可空）。

    Returns:
        镜头语言短语；未知/空值按 static 处理（固定机位）。
    """
    return CAMERA_MOVE_PHRASES.get(str(camera_move or "static"), CAMERA_MOVE_PHRASES["static"])


def framing_lock(shot_size: str | None) -> str:
    """获取景别锁定句（镜头内保持同一构图与背景）。

    Args:
        shot_size: 镜头景别枚举（extreme_wide/wide/medium/close_up/
            extreme_close_up/over_shoulder，可空）。

    Returns:
        对应景别的锁定句；缺失/未知按中景兜底。
    """
    return _FRAMING_LOCKS.get(str(shot_size or ""), _DEFAULT_FRAMING_LOCK)


def _sanitize(text: str) -> str:
    """清洗 prompt 中的宫格诱发词（延迟导入避免模块加载顺序问题）。

    Args:
        text: 待清洗文本。

    Returns:
        清洗后的文本；reference_guard 不可用时原样返回。
    """
    try:
        from app.services.reference_guard import sanitize_prompt_terms

        return sanitize_prompt_terms(text)
    except Exception:  # noqa: BLE001 - 清洗失败不影响生成主流程
        return text


def build_motion_prompt(
    shot: Any,
    base_prompt: str | None = None,
    *,
    round_no: int = 0,
) -> str:
    """构造包含"人物动作 + 镜头运动 + 时序变化"三要素的视频生成 prompt。

    2026-10-29 缺陷一修复：对基础 prompt 与动作描述统一做宫格诱发词清洗
    （multi-view / expression sheet / 三视图 / 九宫格等词改写为单视角
    等价描述），防止历史 prompt 或 LLM 描述诱发多宫格画面。

    Args:
        shot: 镜头实例（读取 action_desc / emotion_tag / dialogue /
            camera_move / final_prompt / image_prompt；可空）。
        base_prompt: 视觉基础 prompt（缺省取镜头 final_prompt/image_prompt）。
        round_no: QC 轮次（0 为首轮；>0 表示静态判定后的强化重试轮）。

    Returns:
        三要素齐全的生成 prompt（≤1600 字）。
    """
    base = _sanitize(str(base_prompt or "").strip())
    # 2026-11 修复（景别跳变）：历史 final_prompt 中残留「机位：跟随」批注、
    # 旧版运镜短语与旧质量尾巴，会让模型自行推近/换背景；统一清洗后重建。
    base = _strip_quality_suffix(_strip_director_annotations(base))
    action = _strip_director_annotations(
        _sanitize(str(getattr(shot, "action_desc", None) or "").strip())
    )
    camera = camera_phrase(getattr(shot, "camera_move", None))
    framing = framing_lock(getattr(shot, "shot_size", None))

    if shot is not None and not base:
        base = _sanitize(str(
            getattr(shot, "final_prompt", None)
            or getattr(shot, "image_prompt", None)
            or ""
        ).strip())
        base = _strip_quality_suffix(_strip_director_annotations(base))
    # 2026-11 修复（动作不编造）：没有动作描述时只用中性兜底；有描述但缺动词
    # 时保留原文并追加「按原文自然展开」，不再按情绪注入具体动作。
    if not action:
        action = _NEUTRAL_ACTION
    elif not has_action_verb(action):
        logger.info("镜头动作描述缺动词，按原文自然展开：%s", action[:40])
        action = f"{action}，动作围绕上述画面描述自然展开"
    # 2026-11 QA 修复（可读文字乱码）：动作行若点名"转账记录/合同条款/屏幕"
    # 等可读内容，模型会硬画文字并写错；在动作句后补一句"内容虚化"，
    # 让屏幕/纸张只保留材质与光感。根治方案为后期贴片合成（见验收报告）。
    if any(marker in action for marker in _TEXT_CONTENT_MARKERS):
        action = (
            f"{action}（画面中的纸张与屏幕内容以轻微虚化与反光呈现，"
            "不显示可读文字、数字或表格线）"
        )

    # 2026-11 修复（音画同步）：有台词的镜头必须让角色开口，否则配音出现时
    # 人物闭口，观众会感知为“只有字幕、没有人声”。不追求逐字口型，只要求
    # 开口动作与台词节奏、情绪一致。
    dialogue = str(getattr(shot, "dialogue", None) or "").strip()
    sync_segment = ""
    if dialogue:
        emotion = str(getattr(shot, "emotion_tag", None) or "").strip()
        mood = f"，语气{emotion}" if emotion else ""
        sync_segment = f"台词口型：人物开口说话，口型随台词节奏自然变化{mood}"

    # 旧版运镜/景别段原地替换为新版受控表述，避免新旧指令互相矛盾
    base = _replace_prompt_segment(base, "镜头运动", camera)
    base = _replace_prompt_segment(base, "景别锁定", framing)

    temporal = _TEMPORAL_TEMPLATE
    segments = [base] if base else []
    # 已含三要素的 prompt（如人工编辑过的 final_prompt）不重复拼接，保持幂等
    if "人物动作：" not in base:
        segments.append(f"人物动作：{action}")
    if "镜头运动：" not in base:
        segments.append(f"镜头运动：{camera}")
    if "景别锁定：" not in base:
        segments.append(f"景别锁定：{framing}")
    if "时序变化：" not in base:
        segments.append(f"时序变化：{temporal}")
    if sync_segment:
        segments.append(sync_segment)
    if round_no > 0:
        phrase = _STRENGTHEN_PHRASES[min(round_no - 1, len(_STRENGTHEN_PHRASES) - 1)]
        segments.insert(0, f"【强化动态·第{round_no}次重试】{phrase}")
    if _QUALITY_SUFFIX not in base:
        segments.append(_QUALITY_SUFFIX)
    prompt = "。".join(part.strip("。") for part in segments if part.strip())
    # 新增景别锁定段需要额外空间；1600 字仍低于视频网关的 prompt 上限
    return prompt[:1600]


def _extract_frame_at(src: Path, out: Path, ts: float, duration_s: float) -> bool:
    """在指定时间戳抽取单帧落盘，失败时按回退链重试（O7 修复）。

    历史问题：尾帧时间戳取 ``duration - 0.05``，但容器 duration 元数据常
    大于最后一帧的呈现时间戳（PTS），``-ss`` 输入定位越过最后一帧后 ffmpeg
    解不出任何帧，导致三帧存证缺 last 帧（实测 26/36 张）。回退链：精确定位
    失败后改用 ``-sseof`` 从文件尾部向前定位（标准取尾帧方式，对极短视频与
    时长元数据偏差均容错），最终回落到视频中点兜底。

    Args:
        src: 视频文件绝对路径。
        out: 输出 JPEG 路径；已存在的残缺文件会先清理。
        ts: 目标时间戳（秒）。
        duration_s: 视频总时长（秒，ffprobe 实测值）。

    Returns:
        抽帧成功返回 True；回退链全部失败返回 False。

    Raises:
        无：单次 ffmpeg 失败（返回码非 0）按该次尝试失败处理；超时异常
            向调用方传播（外层统一兜底）。
    """
    attempts: list[list[str]] = [["-ss", f"{max(0.0, ts):.3f}"]]
    # 尾帧/中帧定位失败时追加 -sseof 回退（首帧 ts=0 不适用，取到会是尾帧）
    if ts >= duration_s / 2:
        attempts.append(["-sseof", "-0.3"])
        attempts.append(["-sseof", "-1.0"])
        attempts.append(["-ss", f"{duration_s / 2:.3f}"])
    for seek in attempts:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", *seek, "-i", str(src),
             "-frames:v", "1", "-q:v", "3", str(out)],
            check=False, capture_output=True, timeout=30,
        )
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True
        # 清理空文件/残缺输出，避免下一次尝试或后续判定读到脏文件
        if out.exists():
            try:
                out.unlink()
            except OSError:
                pass
    return False


def extract_evidence_frames(
    video_path: str | Path,
    *,
    prefix: str,
    evidence_dir: str | Path | None = None,
) -> list[str]:
    """抽取视频首、中、尾三帧并存盘，作为动态性验收的存证帧。

    Args:
        video_path: 视频文件绝对路径。
        prefix: 存证文件名前缀（如 ``shot012``），生成
            ``{prefix}_first.jpg/{prefix}_mid.jpg/{prefix}_last.jpg``。
        evidence_dir: 存证目录；缺省为 ``backend/.run/review``。

    Returns:
        成功落盘的帧文件绝对路径列表（最多 3 个，按 首/中/尾 排序）；
        ffmpeg 不可用或抽帧失败时返回空列表。
    """
    src = Path(video_path)
    target_dir = Path(evidence_dir) if evidence_dir else _EVIDENCE_DIR
    if not src.exists() or not shutil.which("ffmpeg"):
        return []
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            check=False, capture_output=True, timeout=30,
        )
        duration_s = float(probe.stdout.decode().strip() or 0)
    except (subprocess.SubprocessError, OSError, ValueError):
        duration_s = 0.0
    if duration_s <= 0:
        return []
    mid_s = duration_s / 2
    last_s = max(0.0, duration_s - 0.05)
    stamps = [("first", 0.0), ("mid", mid_s), ("last", last_s)]
    saved: list[str] = []
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, ts in stamps:
            out = target_dir / f"{prefix}_{name}.jpg"
            # O7 修复：带回退链抽帧——尾帧/中帧定位越过最后一帧 PTS 时
            # 自动降级到 -sseof 尾部定位，保证三帧存证完整（36/36 张）。
            if _extract_frame_at(src, out, ts, duration_s):
                saved.append(str(out))
    except (subprocess.SubprocessError, OSError):
        # 单帧异常不作废已成功落盘的帧（首帧成功、尾帧超时时仍返回首帧）
        return saved
    return saved


def analyze_video_motion(
    video_path: str | Path,
    *,
    sample_fps: int = MOTION_SAMPLE_FPS,
    evidence_prefix: str | None = None,
) -> dict[str, Any]:
    """抽样视频帧并统计画面运动指标。

    以 ``sample_fps`` 抽帧（灰度 160x90），计算相邻帧平均绝对差、首-中-尾
    关键帧差异与最长连续静止窗口。任一硬指标不达标即 ``passed=False``。

    Args:
        video_path: 视频文件绝对路径。
        sample_fps: 抽样帧率（默认 4fps，即 250ms 粒度）。
        evidence_prefix: 三帧存证文件名前缀；传入时把首/中/尾帧按原分辨率
            落盘到 ``backend/.run/review``，供人工复核与验收留痕。

    Returns:
        指标字典：``frame_count/mean_diff/min_diff/max_diff/first_mid_diff/
        mid_last_diff/longest_static_ms/passed/reasons/evidence_frames``；
        抽帧失败时 ``passed=False`` 且 reasons 携带原因。
    """
    path = Path(video_path)
    result: dict[str, Any] = {
        "sample_fps": sample_fps,
        "frame_count": 0,
        "mean_diff": 0.0,
        "min_diff": 0.0,
        "max_diff": 0.0,
        "first_mid_diff": 0.0,
        "mid_last_diff": 0.0,
        "longest_static_ms": 0,
        "passed": False,
        "reasons": [],
    }
    if not path.exists() or not path.is_file():
        result["reasons"].append("视频文件不存在")
        return result
    with tempfile.TemporaryDirectory(prefix="gd_motion_") as tmpdir:
        frame_pattern = str(Path(tmpdir) / "f_%03d.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-i", str(path),
                    "-vf", f"fps={sample_fps},scale=160:90",
                    "-q:v", "3", frame_pattern,
                ],
                check=True,
                timeout=90,
                capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            result["reasons"].append(f"抽帧失败: {type(exc).__name__}")
            return result
        frame_files = sorted(Path(tmpdir).glob("f_*.jpg"))
        if len(frame_files) < 3:
            result["reasons"].append(f"抽样帧不足（{len(frame_files)} 帧）")
            result["frame_count"] = len(frame_files)
            return result
        images = [Image.open(item).convert("L") for item in frame_files]
        diffs: list[float] = []
        for prev, curr in zip(images, images[1:]):
            diffs.append(float(ImageStat.Stat(ImageChops.difference(prev, curr)).mean[0]))
        mid = len(images) // 2
        first_mid = float(
            ImageStat.Stat(ImageChops.difference(images[0], images[mid])).mean[0]
        )
        mid_last = float(
            ImageStat.Stat(ImageChops.difference(images[mid], images[-1])).mean[0]
        )

        longest_run, current_run = 0, 0
        step_ms = 1000.0 / max(1, sample_fps)
        for diff in diffs:
            if diff < MOTION_STATIC_DIFF:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 0
        longest_static_ms = int(round(longest_run * step_ms))

    mean_diff = sum(diffs) / len(diffs)
    result.update(
        {
            "frame_count": len(images),
            "mean_diff": round(mean_diff, 2),
            "min_diff": round(min(diffs), 2),
            "max_diff": round(max(diffs), 2),
            "first_mid_diff": round(first_mid, 2),
            "mid_last_diff": round(mid_last, 2),
            "longest_static_ms": longest_static_ms,
        }
    )
    reasons: list[str] = []
    if mean_diff < MOTION_MIN_MEAN_DIFF:
        reasons.append(f"平均帧差 {mean_diff:.2f} < {MOTION_MIN_MEAN_DIFF}")
    if min(first_mid, mid_last) < MOTION_MIN_KEY_FRAME_DIFF:
        reasons.append(
            f"首中尾关键帧差异不足（{first_mid:.2f}/{mid_last:.2f}）"
        )
    if longest_static_ms > MOTION_MAX_STATIC_MS:
        reasons.append(f"连续静止 {longest_static_ms}ms > {MOTION_MAX_STATIC_MS}ms")
    result["reasons"] = reasons
    result["passed"] = not reasons
    if evidence_prefix:
        evidence = extract_evidence_frames(path, prefix=evidence_prefix)
        result["evidence_frames"] = evidence
        if evidence:
            logger.info("三帧验收存证已落盘 %s -> %s", path.name, evidence)
    return result


def extract_last_frame(video_path: str | Path, out_path: str | Path) -> bool:
    """抽取视频最后一帧并保存为 JPEG。

    Args:
        video_path: 源视频路径。
        out_path: 输出图片路径（父目录自动创建）。

    Returns:
        成功返回 True；源文件缺失/ffmpeg 失败返回 False。
    """
    source = Path(video_path)
    target = Path(out_path)
    if not source.exists() or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration_seconds(source)
    seek = max(0.0, duration - 0.25) if duration > 0 else 0.0
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-ss", f"{seek:.3f}", "-i", str(source),
                "-frames:v", "1", "-q:v", "2", str(target),
            ],
            check=True,
            timeout=60,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("尾帧抽取失败 %s: %s", source.name, exc)
        return False
    return target.exists() and target.stat().st_size > 0


def image_to_data_uri(image_path: str | Path, *, max_side: int = 1280) -> str | None:
    """把本地图片转为 DashScope I2V 可接受的 JPEG data URI。

    Args:
        image_path: 图片绝对路径（PNG/JPEG/WebP）。
        max_side: 长边上限（像素）；DashScope 建议 360~2000，默认 1280。

    Returns:
        ``data:image/jpeg;base64,...``；图片缺失或不可解码返回 None。
    """
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / float(longest)
                rgb = rgb.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=90)
    except (OSError, ValueError) as exc:
        logger.warning("首帧转 data URI 失败 %s: %s", path.name, exc)
        return None
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def _probe_duration_seconds(path: Path) -> float:
    """用 ffprobe 读取视频时长（秒）。

    Args:
        path: 视频路径。

    Returns:
        时长秒数；读取失败返回 0。
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        data = json.loads(proc.stdout or "{}")
        return float((data.get("format") or {}).get("duration") or 0)
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError):
        return 0.0
