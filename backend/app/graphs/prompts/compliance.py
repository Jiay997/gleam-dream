"""审核 Agent（Compliance）提示词模块（五段式，中文）。

:func:`build_compliance_prompt` 构造内容安全审核提示词，对结构化剧本
做三重合规扫描（PRD NFR-C1 剧本前置审核）：涉政 / 色情 / 未成年人 /
暴力 / 侵权 / 敏感词 / 真人肖像。

判定语义（与契约 ComplianceReport 对齐）：
- ``pass``：无风险；
- ``flag``：存在可弱化的中低风险（首轮自动弱化重审一次）；
- ``block``：高危违规，终止流程且不提供绕过入口（PRD NFR-C2）。
"""

from __future__ import annotations

import json
from typing import Any

_COMPLIANCE_CONTRACT = """{
  "verdict": "pass|flag|block",
  "findings": [
    {
      "flag_type": "politics|porn|minor|violence|infringement|sensitive_word|celebrity_face|real_person_likeness",
      "level": "low|medium|high|block",
      "location": "位置，如：第 1 集 场 2 第 3 行",
      "description": "风险描述（<=500字）",
      "suggestion": "弱化/修改建议（<=500字）"
    }
  ],
  "summary": "审核摘要一句话（<=300字）"
}"""


def _script_text(structure: dict[str, Any]) -> str:
    """把结构化剧本压平为审核用纯文本（按集-场-行编号，便于定位）。

    Args:
        structure: 结构化剧本树（episodes_to_structure 产物）。

    Returns:
        逐行带「第 x 集 场 y 行 z」前缀的剧本文本。
    """
    lines: list[str] = []
    for ep in structure.get("episodes", []):
        ep_no = ep.get("episode_no")
        lines.append(f"=== 第 {ep_no} 集《{ep.get('title')}》 ===")
        for scene in ep.get("scenes", []):
            lines.append(
                f"-- 场 {scene.get('scene_no')} {scene.get('location')}"
                f"（{scene.get('time_of_day')}）--"
            )
            for idx, line in enumerate(scene.get("lines", []), start=1):
                speaker = line.get("speaker") or line.get("line_type")
                lines.append(f"行{idx} [{speaker}] {line.get('content')}")
    return "\n".join(lines)


def build_compliance_prompt(structure: dict[str, Any]) -> tuple[str, str]:
    """构造内容安全审核提示词（系统 + 用户）。

    Args:
        structure: 结构化剧本树（含 episodes/scenes/lines）。

    Returns:
        (system_prompt, user_prompt) 二元组。
    """
    system = (
        "【第一段 · 角色与目标】\n"
        "你是「浮光造梦」的内容安全审核 Agent（compliance），一名资深内容风控专家。\n"
        "你的目标是对竖屏短剧剧本做前置合规扫描，输出可执行的判定与定位。\n"
        "判定标准：\n"
        "- pass：无任何风险项；\n"
        "- flag：存在可通过改写弱化的中低风险（表达尺度、擦边描写等），须给出具体建议；\n"
        "- block：涉政、色情、未成年人相关、真实公众人物影射等高危违规，须终止。\n"
        "成功标准：① 判定与发现严格一致（pass 不得有 findings；block 须有 level=block 发现）；"
        "② 每条发现可定位到「第 x 集 场 y 行 z」。\n\n"
        "【第二段 · 项目上下文】\n"
        "- 审核对象：AI 生成的竖屏短剧剧本（前置三重审核的第一重）；\n"
        "- 输出将被记录到 moderation_records 表用于监管追溯（NFR-C5）。\n\n"
        "【第三段 · 工作区约定】\n"
        "你是只读审核者：仅分析用户提供的剧本文本，不产出剧本内容、不调用任何工具。\n\n"
        "【第四段 · 预算与边界】\n"
        "- 本任务为确定性节点编排下的纯文本审核，不涉及积分操作；\n"
        "- 禁止：越权改写剧本内容、给出绕过审核的替代方案；\n"
        "- 拿不准时保守升级（flag 而非 pass，高危一律 block）。\n\n"
        "【第五段 · 输出契约】\n"
        "严格输出 JSON（不要 Markdown 围栏、不要解释文字），结构如下：\n"
        f"{_COMPLIANCE_CONTRACT}"
    )
    user = (
        "请审核以下剧本全文：\n\n"
        f"{_script_text(structure)}\n\n"
        "只输出符合契约的 JSON。"
    )
    return system, user


def findings_to_json(findings: list[dict[str, Any]] | None) -> str:
    """把审核发现序列化为 JSON 文本（回填弱化重审提示词用）。

    Args:
        findings: 审核发现列表（可为 None）。

    Returns:
        紧凑 JSON 字符串（空列表返回 "[]"）。
    """
    return json.dumps(findings or [], ensure_ascii=False)
