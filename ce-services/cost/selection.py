"""选码层 —— LLM 在 bill_match 候选内选清单编码（CostAgent 主线第一环）。

知识层只**召回候选**（Recall@10≈60%，正解多已进 top-k）；选 Top-1 归任务层（PRD §6）。本模块把
「构件描述 + 候选清单项」交 Qwen3-8B 选出最匹配的一项，产 ``{code, confidence, reason, need_review,
alternatives}``。

三条红线（钉在系统提示 + 代码兜底双保险）：
1. **只能从候选 code 里选**——LLM 仍可能造码，故**代码侧再校验 code ∈ candidates**，越界即作废并 need_review；
2. **不确定标 need_review**——低置信（< 阈值）/ 候选都不匹配 → 转 HITL，只建议不定稿；
3. **不造码**——越界 code 不静默接受。

LLM 直出 JSON（``common.llm.call_qwen3``，非 function-calling——Qwen3-8B function-calling 不可靠）。
"""
from __future__ import annotations

from typing import Any

from common.llm import call_qwen3

# 置信阈值：LLM 自评 confidence < 此值 → 强制 need_review（转人工复核）。
CONFIDENCE_THRESHOLD = 0.6

SYSTEM_PROMPT = """\
你是建设工程造价清单选码助手。给定一段构件/做法描述和一组**候选清单项**（来自清单库召回），从候选中选出最匹配的一项清单编码。

**铁律（必须遵守）**：
1. **只能从给定候选的 code 里选**，严禁编造候选列表中不存在的编码。
2. 候选都不匹配 / 信息不足 / 把握不大时，置 need_review=true，并在 reason 说明原因——宁可转人工复核，绝不硬选或编码。
3. confidence 如实反映把握（0~1 浮点），不确定就给低分。
4. alternatives 填次优的候选 code（也必须来自候选列表），无则空数组。
5. **浇筑方式必须对齐**：标「浇筑方式=预制」或「浇筑方式=装配」的候选是预制/装配构件。除非构件描述明确出现「预制」或「装配」字样，否则一律选现浇构件（即没有「浇筑方式=预制/装配」标注的对应项），严禁因名称字面相同（如"矩形柱""过梁"）而选中预制候选——房屋建筑默认现浇，预制需描述明示。

**输出要求**：只返回合法 JSON 对象，不输出任何 JSON 以外的文字。
"""


def build_user_message(description: str, candidates: list[dict]) -> str:
    """构件描述 + 候选清单项 → 选码 user message。

    参数：
        description —— 构件/做法自然语言描述。
        candidates —— 知识层 /bill/match 候选：``[{code, name, unit, feature, chapter, score, ...}]``。
    返回：
        拼好的 user message（候选编号列出 code/名称/单位/特征/章节；末尾附 /no_think 禁 thinking）。
    """
    lines: list[str] = [f"构件描述：{description}", "", f"候选清单项（共 {len(candidates)} 个，只能从中选）："]
    for i, c in enumerate(candidates, 1):
        parts = [f"{i}. code={c.get('code', '')}", f"名称={c.get('name', '')}"]
        # 浇筑方式：预制/装配候选与现浇同名同码段易混（如预制矩形柱 vs 现浇钢筋混凝土柱），
        # 必须显式喂给 LLM 区分——否则只看名称会被"矩形柱"等字面精确命中诱选预制码（见选码评测）。
        cast = (c.get("cast_type") or "").strip()
        if cast:
            parts.append(f"浇筑方式={cast}")
        if c.get("unit"):
            parts.append(f"单位={c['unit']}")
        if c.get("feature"):
            parts.append(f"特征={c['feature']}")
        if c.get("chapter"):
            parts.append(f"章节={c['chapter']}")
        lines.append("  " + " | ".join(parts))
    lines += [
        "",
        "请从以上候选中选出最匹配构件描述的一项，输出合法 JSON，不输出任何 JSON 以外的文字：",
        "",
        """\
{
  "code": "选中的清单编码（必须是上面候选之一的 code；选不准则填 null）",
  "confidence": 0.0到1.0之间的浮点数,
  "reason": "选码理由；若 need_review，说明为何转人工",
  "need_review": true或false,
  "alternatives": ["次优候选 code（来自候选列表），无则空数组"]
}""",
        "",
        "/no_think",
    ]
    return "\n".join(lines)


def _coerce_confidence(raw: Any) -> float:
    """把 LLM 返回的 confidence 容错转为 [0,1] 浮点；非法 → 0.0（从而触发 need_review）。"""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, val))


def select_code(
    description: str,
    candidates: list[dict],
    llm_url: str,
    model_id: str,
) -> dict[str, Any]:
    """LLM 在候选内选清单编码 + 确定性红线兜底。

    参数：
        description —— 构件/做法描述；candidates —— /bill/match 候选列表；
        llm_url / model_id —— Qwen3 vLLM 配置。
    返回：
        ``{code, confidence, reason, need_review, alternatives}``：
        - 候选为空 → 直接 need_review（code=None），不调 LLM；
        - LLM 选的 code 不在候选（造码/越界）→ 作废为 code=None + need_review，reason 标注；
        - confidence < CONFIDENCE_THRESHOLD → 强制 need_review；
        - alternatives 仅保留候选内的 code。
        LLM 非 2xx 经 requests.HTTPError、非法 JSON 经 json.JSONDecodeError 上抛（由 router 映射状态码）。
    """
    candidate_codes = {str(c.get("code", "")).strip() for c in candidates if c.get("code")}

    # 候选为空：无可选，直接转人工，不喂空候选给 LLM 诱发编码。
    if not candidate_codes:
        return {
            "code": None,
            "confidence": 0.0,
            "reason": "知识库未召回任何清单候选，无法选码，转人工复核。",
            "need_review": True,
            "alternatives": [],
        }

    user_msg = build_user_message(description, candidates)
    raw = call_qwen3(SYSTEM_PROMPT, user_msg, llm_url, model_id)

    code = raw.get("code")
    code = str(code).strip() if code not in (None, "") else None
    confidence = _coerce_confidence(raw.get("confidence"))
    reason = str(raw.get("reason", "")).strip()
    need_review = bool(raw.get("need_review", False))
    # alternatives 只保留确实在候选里的 code（剔除 LLM 顺手编的）。
    alternatives = [a for a in (raw.get("alternatives") or [])
                    if str(a).strip() in candidate_codes and str(a).strip() != code]

    # ── 确定性红线兜底 ──
    # 越界 code（造码 / 选了候选外的）：作废、转人工，不静默接受。
    if code is not None and code not in candidate_codes:
        reason = f"[红线] LLM 选了候选外的 code={code!r}（疑造码），已作废转人工。原 reason：{reason}"
        code = None
        need_review = True
    # 选不出 code → 必转人工。
    if code is None:
        need_review = True
    # 低置信 → 转人工（即便选了码也只建议不定稿）。
    if confidence < CONFIDENCE_THRESHOLD:
        need_review = True

    return {
        "code": code,
        "confidence": confidence,
        "reason": reason,
        "need_review": need_review,
        "alternatives": alternatives,
    }
