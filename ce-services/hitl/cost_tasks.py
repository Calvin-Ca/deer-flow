"""组价领域的 HITL 任务源 —— 11 个 builder（探测+建任务）+ 11 个 handler（落值）。

这是「组价」这个领域插进通用 HITL 机制的插件：import 本模块即把 22 个函数注册进 registry，
四层机制（Policy/Manager/UI/Resume）随即能驱动组价停人。别的领域（合规/风险审批…）另起一个
``xxx_tasks.py`` 同样注册即可，机制层与本文件都不用改。
"""
from __future__ import annotations

from .helpers import _mark_resolved, _mk, _resolved, _user_prov
from .models import HITLReason, HumanAction, HumanTaskType, Scope
from .registry import response_handler, task_builder

# 阈值（政策数走 state 覆盖，这里给保守默认）
TAU_CODE = 0.75            # 选码置信阈值
ANOMALY_DEVIATION = 0.30   # 综合单价偏离历史均值阈值


# ══════════════════════════════════════════════════════════════════════════
# builders（探测 + 建任务）
# ══════════════════════════════════════════════════════════════════════════

# —————————————————————— 清单匹配 ——————————————————————

@task_builder(HumanTaskType.FILL_MISSING_INFO, "bill_match", Scope.ITEM)
def _b_missing_info(state, item):
    if not item:
        return None
    missing = item.get("missing_features") or []
    if not missing:
        return None
    return _mk(
        state, item, step="bill_match", task_type=HumanTaskType.FILL_MISSING_INFO,
        reason=HITLReason.MISSING_INFO, title="请补充构件关键特征",
        description="缺这些信息无法唯一选码/套定额",
        allowed_actions=[HumanAction.RESPOND, HumanAction.REJECT],
        context={"清单名称": item.get("code_name"), "已有特征": item.get("feature"),
                 "why": [{"label": m.get("label"), "why": m.get("why")} for m in missing]},
        fields=[{"key": m["key"], "type": "text", "label": m.get("label") or m["key"],
                 "required": True} for m in missing],
    )


@task_builder(HumanTaskType.SELECT_CODE, "bill_match", Scope.ITEM)
def _b_select_code(state, item):
    if not item:
        return None
    if (item.get("code") or {}).get("value"):
        return None  # 已定码
    env = item.get("code_env")
    if not env:
        return None
    result = env.get("result", {}) or {}
    prov = env.get("provenance", {}) or {}
    main, conf = result.get("code"), prov.get("confidence")
    alts = prov.get("alternatives", []) or []
    tau = state.get("tau_code", TAU_CODE)
    # 高置信唯一码自动过；否则消歧
    if main and env.get("status") == "ok" and conf is not None and conf >= tau and not alts:
        return None
    candidates = ([{"code": main, "name": result.get("name"), "score": conf, "reason": "主候选"}]
                  if main else [])
    candidates += [{"code": a.get("code"), "name": a.get("name"),
                    "score": a.get("score"), "reason": a.get("reason")} for a in alts]
    return _mk(
        state, item, step="bill_match", task_type=HumanTaskType.SELECT_CODE,
        reason=HITLReason.LOW_CONFIDENCE if main else HITLReason.MISSING_INFO,
        title="请选择最合适的清单编码",
        allowed_actions=[HumanAction.SELECT, HumanAction.EDIT, HumanAction.REJECT],
        context={"项目特征": item.get("feature")},
        candidates=candidates, suggested_answer={"code": main} if main else None,
    )


# —————————————————————— 套定额 ——————————————————————

@task_builder(HumanTaskType.SELECT_QUOTA, "quota", Scope.ITEM)
def _b_select_quota(state, item):
    if not item:
        return None
    if (item.get("quota") or {}).get("value"):
        return None  # 已定子目
    env = (item.get("quota") or {}).get("envelope")
    if not env:
        return None
    quotas = (env.get("result", {}) or {}).get("quotas", []) or []
    if len(quotas) <= 1:
        return None  # 无子目→交 manual_quota_basis；唯一子目→compute 节点自动钉，无需人
    return _mk(
        state, item, step="quota", task_type=HumanTaskType.SELECT_QUOTA,
        reason=HITLReason.LOW_CONFIDENCE, title="请选择最合适的定额子目",
        allowed_actions=[HumanAction.SELECT, HumanAction.EDIT, HumanAction.REJECT],
        context={"清单": (item.get("code") or {}).get("value"), "项目特征": item.get("feature")},
        candidates=[{"code": q.get("子目号"), "name": q.get("name"),
                     "labor_cost": q.get("labor_cost"), "material_cost": q.get("material_cost"),
                     "machine_cost": q.get("machine_cost")} for q in quotas],
    )


@task_builder(HumanTaskType.MANUAL_QUOTA_BASIS, "quota", Scope.ITEM)
def _b_manual_quota(state, item):
    if not item or item.get("quota_decided"):
        return None
    if (item.get("quota") or {}).get("value") or item.get("quota_basis") is not None:
        return None
    env = (item.get("quota") or {}).get("envelope")
    if not env:
        return None
    quotas = (env.get("result", {}) or {}).get("quotas", []) or []
    if quotas:
        return None  # 有映射→select_quota 处理
    return _mk(
        state, item, step="quota", task_type=HumanTaskType.MANUAL_QUOTA_BASIS,
        reason=HITLReason.MISSING_INFO, title="知识库无对应定额子目，请手工补录人材机基价（或三项留空放弃）",
        allowed_actions=[HumanAction.RESPOND, HumanAction.REJECT],
        context={"清单": (item.get("code") or {}).get("value"), "构件": item.get("feature")},
        fields=[
            {"key": "quota_code", "type": "text", "label": "定额子目号（可选）", "required": False},
            {"key": "labor_cost", "type": "number", "label": "定额人工费基价", "required": False},
            {"key": "material_cost", "type": "number", "label": "定额材料费基价", "required": False},
            {"key": "machine_cost", "type": "number", "label": "定额机械费基价", "required": False},
        ],
    )


@task_builder(HumanTaskType.CONFIRM_CONVERSION, "quota", Scope.ITEM)
def _b_conversion(state, item):
    # 预留接口：定额换算方案就绪且未确认时触发
    if not item:
        return None
    conv = item.get("conversion")
    if not conv or item.get("conversion_confirmed"):
        return None
    return _mk(
        state, item, step="quota", task_type=HumanTaskType.CONFIRM_CONVERSION,
        reason=HITLReason.RULE_CONFIRMATION, title="请确认定额换算方案",
        allowed_actions=[HumanAction.APPROVE, HumanAction.EDIT, HumanAction.REJECT],
        context={"原定额": conv.get("from"), "项目实际": conv.get("to"), "拟换算": conv.get("plan")},
        suggested_answer=conv,
    )


# —————————————————————— 询价 ——————————————————————

def _price_missing(price: dict) -> bool:
    st = price.get("status")
    return st == "no_source" or (st == "ok" and price.get("value") is None)


def _mkey(m: dict) -> str:
    return str(m.get("std") or m.get("raw"))


@task_builder(HumanTaskType.FILL_MISSING_PRICE, "pricing", Scope.ITEM)
def _b_missing_price(state, item):
    if not item or item.get("price_decided"):
        return None
    mats = item.get("materials") or []
    missing = [m for m in mats if _price_missing(m.get("price", {}) or {})]
    if not missing:
        return None
    return _mk(
        state, item, step="pricing", task_type=HumanTaskType.FILL_MISSING_PRICE,
        reason=HITLReason.MISSING_INFO, title="以下材料无信息价，请录入单价",
        allowed_actions=[HumanAction.RESPOND, HumanAction.EDIT, HumanAction.REJECT],
        batch_key="missing_price",  # 整单：所有缺价材料聚成一批
        context={"构件": item.get("feature")},
        fields=[{"key": _mkey(m), "type": "number",
                 "label": f"{_mkey(m)} 单价(元/{m.get('unit') or ''})", "required": True}
                for m in missing],
    )


# —————————————————————— 计算 ——————————————————————

@task_builder(HumanTaskType.FILL_QUANTITY, "compute", Scope.ITEM)
def _b_quantity(state, item):
    if not item:
        return None
    if item.get("quota_basis") is None:  # 无基价，问 Q 无意义
        return None
    if item.get("quantity") is not None:
        return None
    return _mk(
        state, item, step="compute", task_type=HumanTaskType.FILL_QUANTITY,
        reason=HITLReason.MISSING_INFO, title="请录入工程量（清单数量）",
        allowed_actions=[HumanAction.RESPOND, HumanAction.REJECT],
        context={"构件": item.get("feature"), "清单": (item.get("code") or {}).get("value"),
                 "单位": (item.get("quota_basis") or {}).get("单位")},
        fields=[{"key": "quantity", "type": "number", "label": "工程量 Q", "required": True}],
    )


def _rates_complete(rates: dict | None) -> bool:
    return bool(rates) and all(rates.get(k) is not None
                               for k in ("management_fee_rate", "profit_rate", "fee_base"))


@task_builder(HumanTaskType.SET_RATES, "compute", Scope.PROJECT)
def _b_rates(state, _item):
    if _rates_complete(state.get("rates")):
        return None
    return _mk(
        state, None, step="compute", task_type=HumanTaskType.SET_RATES,
        reason=HITLReason.RULE_CONFIRMATION, scope=Scope.PROJECT,
        title="请设定综合单价费率（项目级，一次管全单）",
        description="管理费率/利润率/取费基数为政策/策略数，库内无、不设默认",
        allowed_actions=[HumanAction.APPROVE, HumanAction.EDIT],
        fields=[
            {"key": "management_fee_rate", "type": "number", "label": "企业管理费率 %", "required": True},
            {"key": "profit_rate", "type": "number", "label": "利润率 %", "required": True},
            {"key": "risk_rate", "type": "number", "label": "风险费率 %（默认 0）", "required": False},
            {"key": "fee_base", "type": "select", "label": "取费基数",
             "options": ["labor", "labor_machine", "lmm"], "required": True},
        ],
    )


@task_builder(HumanTaskType.SET_PROJECT_PARAMS, "compute", Scope.PROJECT)
def _b_params(state, _item):
    params = state.get("params")
    if params and params.get("tax_rate") is not None:
        return None
    return _mk(
        state, None, step="compute", task_type=HumanTaskType.SET_PROJECT_PARAMS,
        reason=HITLReason.RULE_CONFIRMATION, scope=Scope.PROJECT,
        title="请设定项目级费用参数（税金/措施/其他/规费）",
        allowed_actions=[HumanAction.APPROVE, HumanAction.EDIT],
        fields=[
            {"key": "tax_rate", "type": "number", "label": "增值税率 %", "required": True},
            {"key": "measure_fee", "type": "number", "label": "措施项目费", "required": False},
            {"key": "other_fee", "type": "number", "label": "其他项目费", "required": False},
            {"key": "fee_levy", "type": "number", "label": "规费", "required": False},
        ],
    )


@task_builder(HumanTaskType.REVIEW_ANOMALY, "compute", Scope.ITEM)
def _b_anomaly(state, item):
    # 预留接口：综合单价偏离历史均值超阈值时触发（需 state.history_mean 或 item 自带 baseline）
    if not item or item.get("anomaly_approved"):
        return None
    up = item.get("unit_price") or {}
    cur = up.get("unit_price")
    mean = item.get("baseline_unit_price") or state.get("history_mean")
    if cur is None or not mean:
        return None
    dev = (cur - mean) / mean
    if dev <= state.get("anomaly_deviation", ANOMALY_DEVIATION):
        return None
    return _mk(
        state, item, step="compute", task_type=HumanTaskType.REVIEW_ANOMALY,
        reason=HITLReason.ANOMALY_REVIEW, title="综合单价异常，请审核",
        allowed_actions=[HumanAction.APPROVE, HumanAction.EDIT, HumanAction.REJECT, HumanAction.ESCALATE],
        context={"构件": item.get("feature"), "当前综合单价": cur, "历史均值": mean,
                 "偏差": f"{dev * 100:.0f}%"},
    )


@task_builder(HumanTaskType.FINAL_APPROVAL, "compute", Scope.PROJECT)
def _b_final(state, _item):
    if state.get("rollup") is None:
        return None
    if "final_approved" in state or _resolved(state, HumanTaskType.FINAL_APPROVAL):
        return None
    rollup = state["rollup"]
    return _mk(
        state, None, step="compute", task_type=HumanTaskType.FINAL_APPROVAL,
        reason=HITLReason.RISKY_ACTION, scope=Scope.PROJECT,
        title="请确认最终组价结果", description="确认后将生成正式成果（外部写动作）",
        allowed_actions=[HumanAction.APPROVE, HumanAction.EDIT, HumanAction.REJECT],
        context={"总造价": rollup.get("total"), "税前造价": rollup.get("pre_tax_total"),
                 "分部分项": (rollup.get("breakdown") or {}).get("分部分项费")},
    )


# ══════════════════════════════════════════════════════════════════════════
# handlers（落值回 state）
# ══════════════════════════════════════════════════════════════════════════


@response_handler(HumanTaskType.FILL_MISSING_INFO)
def _h_missing_info(state, item, task, resp):
    data = resp.data or {}
    added = "，".join(f"{k}={v}" for k, v in data.items() if v not in (None, ""))
    item["feature"] = (item.get("feature") or "") + (("，" + added) if added else "")
    item["missing_features"] = []
    item["clarify_rounds"] = (item.get("clarify_rounds") or 0) + 1
    state["needs_rematch"] = bool(added)  # 回环重匹配


@response_handler(HumanTaskType.SELECT_CODE)
def _h_select_code(state, item, task, resp):
    val = resp.selected or (resp.data or {}).get("code")
    item["code"] = {"value": val, "provenance": _user_prov("用户选定/录入清单码")}


@response_handler(HumanTaskType.SELECT_QUOTA)
def _h_select_quota(state, item, task, resp):
    val = resp.selected or (resp.data or {}).get("子目号")
    env = (item.get("quota") or {}).get("envelope") or {}
    quotas = (env.get("result", {}) or {}).get("quotas", []) or []
    item["quota"] = {"value": val, "provenance": _user_prov("用户选定定额子目"), "envelope": env}
    item["quota_basis"] = next((q for q in quotas if str(q.get("子目号")) == str(val)), None)
    item["quota_decided"] = True


@response_handler(HumanTaskType.MANUAL_QUOTA_BASIS)
def _h_manual_quota(state, item, task, resp):
    d = resp.data or {}
    costs = ("labor_cost", "material_cost", "machine_cost")
    item["quota_decided"] = True
    if any(d.get(k) is None for k in costs):
        # 放弃补录：诚实标缺口，不造基价（下游 no_pricing）
        item["quota_basis"] = None
        item["quota"] = {"value": None, "provenance": _user_prov("无映射定额（用户未补录）")}
        return
    basis = {"子目号": d.get("quota_code") or "用户录入", "name": "用户补录定额基价",
             "labor_cost": d["labor_cost"], "material_cost": d["material_cost"],
             "machine_cost": d["machine_cost"], "source_ref": "用户补录定额基价"}
    item["quota_basis"] = basis
    item["quota"] = {"value": basis["子目号"], "provenance": _user_prov(basis["source_ref"])}


@response_handler(HumanTaskType.CONFIRM_CONVERSION)
def _h_conversion(state, item, task, resp):
    if resp.action is HumanAction.EDIT:
        item["conversion"] = resp.data or item.get("conversion")
    item["conversion_confirmed"] = True


@response_handler(HumanTaskType.FILL_MISSING_PRICE)
def _h_missing_price(state, item, task, resp):
    d = resp.data or {}
    for m in item.get("materials") or []:
        key = _mkey(m)
        if d.get(key) is not None:
            m["price"] = {"value": float(d[key]), "status": "ok",
                          "provenance": _user_prov("用户录入信息价")}
    item["price_decided"] = True


@response_handler(HumanTaskType.FILL_QUANTITY)
def _h_quantity(state, item, task, resp):
    q = (resp.data or {}).get("quantity")
    item["quantity"] = float(q) if q is not None else None


@response_handler(HumanTaskType.SET_RATES)
def _h_rates(state, item, task, resp):
    state["rates"] = dict(resp.data or {})
    _mark_resolved(state, HumanTaskType.SET_RATES)


@response_handler(HumanTaskType.SET_PROJECT_PARAMS)
def _h_params(state, item, task, resp):
    state["params"] = dict(resp.data or {})
    _mark_resolved(state, HumanTaskType.SET_PROJECT_PARAMS)


@response_handler(HumanTaskType.REVIEW_ANOMALY)
def _h_anomaly(state, item, task, resp):
    if resp.action is HumanAction.APPROVE:
        item["anomaly_approved"] = True
    elif resp.action is HumanAction.EDIT:
        item.setdefault("price_overrides", {}).update(resp.data or {})
        item["needs_recompute"] = True
        item["anomaly_approved"] = True


@response_handler(HumanTaskType.FINAL_APPROVAL)
def _h_final(state, item, task, resp):
    state["final_approved"] = resp.action is HumanAction.APPROVE
    if resp.action is HumanAction.EDIT:
        state["final_edits"] = resp.data
    _mark_resolved(state, HumanTaskType.FINAL_APPROVAL)
