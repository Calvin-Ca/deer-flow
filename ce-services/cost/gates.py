"""门控规则（§6）+ interrupt payload 构造（§5.2/§5.3）+ 决策落值（原则 3）。

设计核心：**门控不逐步 gate**（原则 2）——每个 interrupt 节点先算「要不要真的停」，只有
低置信 / 多候选 / 缺价 / 缺参数才暂停；命中且高置信的自动过。是否跳闸的判断**全在这段代码里**，
不交弱模型（设计 §1.2 / §10：Qwen3-8B 不当编排器）。

payload 必须**结构化**（不是自由文本问句）：前端只从字段渲染，弱模型措辞乱也不影响展示（§8）。
"""
from __future__ import annotations

from typing import Any

# 用户在 confirm 闸可选的动作（§5.2）
CONFIRM_ACTIONS = ["approve", "select_alternative", "manual_override"]


def should_pause_coding(env: dict[str, Any], tau: float) -> bool:
    """编码闸门控（§6）：是否暂停等人工确认编码。

    参数：env —— ``list_match`` 信封；tau —— 置信阈值。
    返回：True=暂停。规则：``status==need_review`` / 选不出码 / ``confidence < τ`` / 多候选并列 → 暂停；
      唯一高置信（≥τ 且 alternatives 为空）→ 自动过。
    """
    if env.get("status") == "need_review":
        return True
    if not env.get("result", {}).get("code"):
        return True
    conf = env.get("provenance", {}).get("confidence")
    if conf is None or conf < tau:
        return True
    if env.get("provenance", {}).get("alternatives"):
        return True
    return False


def should_pause_quota(env: dict[str, Any]) -> bool:
    """定额闸门控（§6）：是否暂停等人工确认子目。

    参数：env —— ``pick_quota`` 信封。
    返回：True=暂停。规则：无子目 / 多子目并列 → 暂停；唯一子目 → 自动过。
      （本期定额无 LLM 置信，故以「子目数」为门控判据；多子目时人工选。）
    """
    quotas = env.get("result", {}).get("quotas", [])
    if not quotas:
        return True
    if len(quotas) > 1:
        return True
    return False


def should_pause_price(price: dict[str, Any]) -> bool:
    """信息价逐项例外闸门控（§6/§7）：单条材料价是否需人工录入。

    参数：price —— 材料的 price 块 ``{value, status, provenance}``。
    返回：True=暂停（缺价 / 状态非 ok）。命中信息价（status==ok 且有值）→ 自动过，绝不逐材料问（§7 反例）。
    """
    return price.get("status") != "ok" or price.get("value") is None


def confirm_payload(node: str, env: dict[str, Any], title: str) -> dict[str, Any]:
    """确认型 interrupt payload（§5.2）——编码 / 定额，候选+依据，可改。

    参数：node —— 节点名；env —— 原语信封；title —— 给用户看的标题。
    返回：``{gate_type:"confirm", node, title, proposal, evidence, alternatives, actions}``——
      proposal=原语结果、evidence=provenance（来源+置信+备选）、actions 见 ``CONFIRM_ACTIONS``。
    """
    prov = env.get("provenance", {})
    return {
        "gate_type": "confirm",
        "node": node,
        "title": title,
        "proposal": env.get("result", {}),
        "evidence": {
            "source_type": prov.get("source_type"),
            "source_ref": prov.get("source_ref"),
            "confidence": prov.get("confidence"),
        },
        "alternatives": prov.get("alternatives", []),
        "actions": CONFIRM_ACTIONS,
    }


def input_payload(node: str, title: str, fields: list[dict[str, Any]], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """录入型 interrupt payload（§5.3）——setup / 费率 / 缺价录入，填值或选来源。

    参数：node —— 节点名；title —— 标题；fields —— 字段定义列表
      （每项 ``{key, type, label, options?, default?, required?}``）；context —— 可选上下文（如缺价材料信息）。
    返回：``{gate_type:"input", node, title, fields, context}``——前端按字段类型渲染单选/数字框/可编辑表格。
    """
    payload: dict[str, Any] = {"gate_type": "input", "node": node, "title": title, "fields": fields}
    if context:
        payload["context"] = context
    return payload


def apply_confirm_decision(env: dict[str, Any], decision: dict[str, Any]) -> tuple[Any, dict[str, Any], str]:
    """把 confirm 闸 resume 回来的用户动作落成权威值（原则 3：钉值，不重跑 LLM）。

    参数：env —— 触发该闸的信封；decision —— 用户动作
      ``{action: approve|select_alternative|manual_override, value?, code?}``。
    返回：``(value, provenance, action)``：
      - approve —— 采纳原语 proposal 的主值（编码 result.code / 定额首子目），provenance 沿用信封；
      - select_alternative —— 采纳 ``decision.value``（须为 provenance.alternatives 内的 code），来源标 user 选定；
      - manual_override —— 采纳用户手填 ``decision.value``，provenance.source_type=user_input。
    非法 action / 越界 alternative → 回退按 approve 处理主值，并在 provenance 标注（不静默吞，交审计）。
    """
    action = (decision or {}).get("action", "approve")
    prov = dict(env.get("provenance", {}))
    result = env.get("result", {})
    main_value = result.get("code") if "code" in result else result.get("quotas", [{}])[0].get("子目号")

    if action == "manual_override":
        return decision.get("value"), {"source_type": "user_input", "source_ref": "用户录入", "confidence": None}, action

    if action == "select_alternative":
        alt = decision.get("value") or decision.get("code")
        alt_codes = {str(a.get("code")) for a in prov.get("alternatives", [])}
        if str(alt) in alt_codes:
            return alt, {**prov, "source_ref": f"{prov.get('source_ref')}（用户选定备选）"}, action
        # 越界备选：回退主值，标注交审计，不静默接受
        return main_value, {**prov, "note": f"select_alternative 越界 value={alt!r}，已回退主值"}, "approve"

    return main_value, prov, "approve"
