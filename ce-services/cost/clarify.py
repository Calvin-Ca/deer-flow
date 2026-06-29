"""特征澄清层 —— LLM 抽取构件描述缺失的关键特征槽（CostAgent HITL 改 1 / PRD FR-P02、EH-04）。

设计：组价前若构件描述不足以唯一定位清单编码 + 定额子目（如只写「砌筑」缺砌块/砂浆强度），
应**先反问补全关键特征、回填后重走门控**（PRD §8.2「澄清结果回填后重走 §4.4 门控」），而非拿残缺
描述硬选码。本模块只负责「**缺什么**」的判定——给描述 + 召回候选提示，让 Qwen3-8B 直出结构化缺口；
「**要不要停闸问**」「**问几轮**」由 graph 的确定性门控决定（弱模型不驱动流程，§1.2）。

可靠性：Qwen3-8B 直出 JSON（非 function-calling，与 ``select_code`` 同信任级）。解析失败 / HTTP 异常
**降级为「不澄清」**（返回空），交既有 list_gate 确认闸兜底——绝不因抽取失败而卡死或杜撰特征。
"""
from __future__ import annotations

import json
from typing import Any

import requests

from common.llm import call_qwen3

# 单次澄清最多反问的特征项数（避免一次抛一长串问题压垮用户；按对选码/套定额最关键排序后截断）。
MAX_SLOTS_PER_ROUND = 2

SYSTEM_PROMPT = """\
你是建设工程造价的构件特征澄清助手。给定一段构件/做法描述（用于组价：选清单编码 + 套定额子目），\
判断它是否已含「唯一确定清单编码与定额子目」所需的关键特征；若不足，列出缺失的关键特征项，供向用户反问补全。

关键特征示例（随构件类型不同，仅作参考，不要照搬不相关项）：
- 混凝土构件：构件类型（柱/梁/板…）、混凝土强度等级（如 C30）、现浇/预制、断面尺寸或规格
- 砌体：砌块类型与强度（如 MU10 标准砖）、砂浆品种与等级（如 M5 水泥砂浆）、墙厚
- 钢筋：钢筋级别（如 HRB400）、规格

铁律（必须遵守）：
1. 只列「**真正缺失且对选码/套定额必要**」的特征——描述里已出现的绝不再问。
2. 不杜撰该构件不存在的特征（描述是砌体就别问混凝土强度；是钢筋就别问砂浆）。
3. 描述已足以唯一定位时，sufficient=true、missing 为空数组。
4. missing 按「对区分清单/定额最关键」排序，最多列 2 项。

输出要求：只返回合法 JSON 对象，不输出任何 JSON 以外的文字。\
"""


def build_user_message(description: str, candidate_hints: list[str] | None) -> str:
    """构件描述 + 召回候选名称提示 → 缺口抽取 user message。

    参数：description —— 构件/做法描述；candidate_hints —— 召回候选的名称（含主选 + 备选），
      用于让模型据「候选在哪些维度分歧」反推缺了哪个特征；可空（描述层面判断即可）。
    返回：拼好的 user message（含 JSON 输出契约 + ``/no_think`` 禁 thinking）。
    """
    lines = [f"构件描述：{description}"]
    hints = [h for h in (candidate_hints or []) if h]
    if hints:
        lines.append("")
        lines.append("召回候选（名称，供参考其分歧维度）：")
        lines += [f"  - {h}" for h in hints]
    lines += [
        "",
        "请判断描述是否足以唯一选码+套定额；不足则列缺失关键特征。只返回合法 JSON：",
        "",
        """\
{
  "sufficient": true或false,
  "missing": [
    {"key": "特征机读键（英文小写下划线，如 concrete_grade/section_size/mortar_grade）",
     "label": "给用户看的中文名称（如 混凝土强度等级）",
     "why": "为什么需要（如 候选含C25/C30多档，无此值无法定子目）"}
  ]
}""",
        "",
        "/no_think",
    ]
    return "\n".join(lines)


def extract_missing_features(
    description: str,
    candidate_hints: list[str] | None,
    llm_url: str,
    model_id: str,
) -> list[dict[str, Any]]:
    """LLM 抽取构件描述缺失的关键特征槽（供特征澄清闸反问）。

    参数：description —— 构件/做法描述；candidate_hints —— 召回候选名称提示（可空）；
      llm_url / model_id —— Qwen3 vLLM 配置。
    返回：缺口列表 ``[{key, label, why}]``（已规范化、最多 MAX_SLOTS_PER_ROUND 项，按关键度截断）；
      描述已充分 / LLM 不可靠（HTTP 异常 / 非法 JSON）/ 无合法缺口 → ``[]``——
      **降级为「不澄清」**，交既有 list_gate 确认闸兜底，绝不卡死或杜撰。
    """
    if not (description or "").strip():
        return []
    try:
        raw = call_qwen3(SYSTEM_PROMPT, build_user_message(description, candidate_hints), llm_url, model_id)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        return []  # 弱模型不可靠 → 不澄清，degrade 到现有行为

    if raw.get("sufficient"):
        return []
    out: list[dict[str, Any]] = []
    for m in (raw.get("missing") or [])[:MAX_SLOTS_PER_ROUND]:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or m.get("label") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "label": str(m.get("label") or key).strip(),
            "why": str(m.get("why") or "").strip(),
        })
    return out
