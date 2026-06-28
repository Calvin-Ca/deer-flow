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

# §8 综合单价费率：管理费率/利润率/取费基数为政策数（库内无），必须由人工给定方可算综合单价。
RATE_REQUIRED = ("management_fee_rate", "profit_rate", "fee_base")
# §12 税金率：政策数，须显式录入（措施/其他/规费可缺省 0，唯税率不杜撰、不默认）。
PARAM_REQUIRED = ("tax_rate",)


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
    返回：True=暂停。规则：``no_source``（缺信息价）→ 停；命中（``ok`` 且有值）→ 过；
      ``from_quota_base``（人工/机械/百分比，价在定额基价）→ 过，**不逐条误问**（§7 反例）。
      边界：状态 ok 但值缺失（命中但单价解析失败）→ 仍停，避免带空价过闸。
    """
    status = price.get("status")
    return status == "no_source" or (status == "ok" and price.get("value") is None)


def should_pause_rates(rates: dict[str, Any] | None) -> bool:
    """综合单价费率闸门控（§6/§8）：是否暂停等人工录入费率。

    参数：rates —— 费率块 ``{management_fee_rate, profit_rate, risk_rate?, fee_base, tax_rate?}`` 或 None。
    返回：True=暂停。规则（§6「已有项目默认值自动过、首次无默认值暂停」）：缺费率块或管理费率/利润率/取费基数
      任一为 None → 停（这三项是政策数、库内无，绝不杜撰）；齐全 → 自动过。
    """
    if not rates:
        return True
    return any(rates.get(k) is None for k in RATE_REQUIRED)


def should_pause_params(params: dict[str, Any] | None) -> bool:
    """项目级费用闸门控（§6/§10⑪/§12）：是否暂停等人工录入措施/其他/规费/税金率。

    参数：params —— ``{measure_fee?, other_fee?, fee_levy?, tax_rate}`` 或 None。
    返回：True=暂停。规则：缺 params 块或税金率为 None → 停（税率是政策数、不替用户定）；
      措施/其他/规费可缺省 0、不单独触发停。齐（税率已给）→ 自动过。
    """
    if not params:
        return True
    return any(params.get(k) is None for k in PARAM_REQUIRED)


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
    if "code" in result:
        main_value = result.get("code")
    else:
        # 定额信封：可能空子目（选错码无定额映射），approve 时取首子目，空则 None（不崩、交下游/审计）。
        quotas = result.get("quotas") or []
        main_value = quotas[0].get("子目号") if quotas else None

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
