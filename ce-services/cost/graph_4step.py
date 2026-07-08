"""可中断组价状态机 · **4 大步骤版**（清单匹配 / 套定额 / 询价 / 计算）—— 基于统一 HITL 机制 ``hitl.py``。

与旧 13 节点图（``graph.py``）的区别：
- 图层只剩 **4 大步骤**，每步内的「小步骤」由 ``hitl.py`` 的 builder/handler 承载（预留接口=注册即扩展）。
- 每步 = ``compute 节点（取数/算价，无 interrupt）→ hitl 节点（evaluate_hitl 命中则 interrupt）→ apply 节点
  （apply_human_response 落值）``，apply 后按「本步还有没有待办」决定回环本步或进下一步。
- **compute/gate 双拆保留**：LLM/取数在 compute 节点（resume 不重跑）；hitl 节点只读 state + interrupt（幂等）。
- **自动过下沉 compute**：高置信码 / 唯一子目在 compute 节点确定性钉值（不 interrupt）；hitl 只处理"要人"的。
- **触发原因统一**：所有停闸由 ``hitl.evaluate_hitl`` 按 HITLReason 判定，弱模型不驱动流程。

链路（多构件外层循环 + 项目级收尾，与旧图行为对齐）：
    setup
      →〔每件: 清单匹配(compute→hitl⇄apply, 缺特征回环) →〔有码〕套定额(compose→hitl⇄apply)
              → 询价(hitl⇄apply) → 计算·件(工程量 hitl⇄apply)〕→ advance
      →〔全办完 & 有可算件〕计算·项目(费率/税金 hitl⇄apply) → 汇总compute → 终审(hitl⇄apply) → done
      →〔无可算件〕no_pricing（不产虚构总价，P0）

保留公共 API：``build_graph(checkpointer)`` / ``recompute_rollup(...)``（session.py 依赖）。
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from langgraph.graph import END, START, StateGraph

import hitl  # 顶级包（cost 同级），HITL 通用机制
from hitl import HumanResponse, HumanTask, Scope

from common.config import COST_DEFAULT_SPEC, HITL_TAU_HIGH, LLM_MODEL_ID, LLM_URL
from cost import clarify, gates, provenance, quota_selection
from cost.pricing import (
    HierarchyItem,
    HierarchyRollupInput,
    UnitPriceInput,
    compute_unit_price,
    rollup_hierarchy,
)
from cost.state import (
    DEFAULT_SINGLE_WORK,
    DEFAULT_UNIT_WORK,
    CostTaskState,
    audit_entry,
    lock_value,
    override_entry,
    provenance_event,
)

logger = logging.getLogger("ce-services.cost.hitl4")

TOP_K = 10
MAX_CLARIFY_ROUNDS = 2


# ══════════════════════════════════════════════════════════════════════════
# item 读写辅助（多构件外层循环）
# ══════════════════════════════════════════════════════════════════════════


def _item(state: CostTaskState) -> dict[str, Any]:
    items = state.get("items") or [{"feature": state.get("feature")}]
    idx = min(state.get("current_item", 0), len(items) - 1)
    return dict(items[idx])


def _put_item(state: CostTaskState, item: dict[str, Any]) -> list[dict[str, Any]]:
    items = [dict(it) for it in (state.get("items") or [])]
    idx = state.get("current_item", 0)
    if 0 <= idx < len(items):
        items[idx] = item
    else:
        items.append(item)
    return items


def _ensure_item_id(item: dict[str, Any], idx: int) -> None:
    """hitl.py 的 item 级任务按 item_id 定位；无则用下标补一个稳定 id。"""
    if not item.get("item_id"):
        item["item_id"] = f"item-{idx}"


# ══════════════════════════════════════════════════════════════════════════
# 通用 HITL 节点包装 + apply 翻译层（把 hitl 结果翻成 CostTaskState 增量/reducer）
# ══════════════════════════════════════════════════════════════════════════


def _hitl(step, scope=None):
    """生成某步某作用域的 hitl 闸节点（薄包装 hitl.hitl_gate_node）。"""

    def node(state: CostTaskState) -> dict[str, Any]:
        return hitl.hitl_gate_node(state, step=step, scope=scope)

    node.__name__ = f"hitl_{step}_{scope.value if scope else 'all'}"
    return node


def apply_node(state: CostTaskState) -> dict[str, Any]:
    """通用落值节点：把 pending_human_task + hitl_result 经 hitl.apply_human_response 落回 state，

    再翻成 CostTaskState 的增量（reducer 通道 audit_log/escalations 只回新增；items/rates/params 覆盖回传；
    清空 pending/hitl_result）。独立于 hitl 节点 → 不受 interrupt resume 重跑影响。
    """
    raw = state.get("pending_human_task")
    resp = state.get("hitl_result")
    if not raw or resp is None:
        return {"pending_human_task": None, "hitl_result": None}

    task = HumanTask.model_validate(raw)
    response = HumanResponse.model_validate(resp)

    # 在工作副本上跑，便于 diff reducer 通道
    work: dict[str, Any] = dict(state)
    work["audit_log"] = list(state.get("audit_log") or [])
    work["escalations"] = list(state.get("escalations") or [])
    n_audit, n_esc = len(work["audit_log"]), len(work["escalations"])

    hitl.apply_human_response(work, task, response)

    idx = state.get("current_item", 0)
    ev = {"step": task.task_type.value, "status": "paused", "auto_pass": False,
          "paused": True, "reason": task.reason.value,
          "result": {"action": response.action.value, "item": task.item_id}}
    delta: dict[str, Any] = {
        "pending_human_task": None, "hitl_result": None,
        "items": work.get("items"),
        "audit_log": work["audit_log"][n_audit:],   # reducer add：仅新增
        "events": [ev],
    }
    if len(work["escalations"]) > n_esc:
        delta["escalations"] = work["escalations"][n_esc:]
    for k in ("rates", "params", "resolved_tasks", "final_approved", "needs_rematch"):
        if k in work:
            delta[k] = work[k]
    # 用户改动落 override 轨迹（select/edit/respond/manual）
    if response.action.value in ("select", "edit", "respond"):
        val = response.selected or (response.data if response.data else None)
        delta["overrides"] = [override_entry(task.task_type.value, idx, val, by="user")]
    return delta


def _step_pending(step, scope=None):
    """路由判据：本步（本作用域）是否还有待办任务。"""

    def route(state: CostTaskState) -> str:
        return "loop" if hitl.evaluate_hitl(state, step=step, scope=scope) is not None else "next"

    return route


# ══════════════════════════════════════════════════════════════════════════
# 步骤 1 —— 清单匹配（compute: 召回+选码+探测缺特征+自动过高置信码）
# ══════════════════════════════════════════════════════════════════════════


def bill_match_compute(state: CostTaskState) -> dict[str, Any]:
    """清单匹配 compute：bill_match 召回 + select_code 选码（含 LLM）+ 缺特征探测 + 高置信自动过。**不暂停**。"""
    item = _item(state)
    idx = state.get("current_item", 0)
    _ensure_item_id(item, idx)
    env = provenance.list_match(item.get("feature"), state["spec_version"], TOP_K)
    item["code_env"] = env
    item["code_name"] = env["result"].get("name")

    # 缺特征探测（need_review 且澄清未超轮次时抽缺口）
    if env["status"] == provenance.STATUS_NEED_REVIEW and (item.get("clarify_rounds") or 0) < MAX_CLARIFY_ROUNDS:
        hints = [env["result"].get("name")] + [a.get("name") for a in env["provenance"].get("alternatives", [])]
        extracted = clarify.extract_missing_features(item.get("feature"), hints, LLM_URL, LLM_MODEL_ID)
        feature_now = str(item.get("feature") or "")
        item["missing_features"] = [
            m for m in extracted
            if f"{m.get('label') or m.get('key')}=" not in feature_now and f"{m.get('key')}=" not in feature_now
        ]
    else:
        item["missing_features"] = []

    # 无缺特征 & 高置信 → compute 里确定性自动过钉码（hitl 的 select_code builder 据 code.value 直接跳过）
    if not item["missing_features"] and not gates.should_pause_coding(env, HITL_TAU_HIGH):
        item["code"] = lock_value(env["result"].get("code"), env["provenance"], by="model")

    return {"items": _put_item(state, item), "needs_rematch": False,
            "events": [provenance_event(env, paused=False)]}


def _route_bill_match(state: CostTaskState) -> str:
    """清单匹配 apply 后路由：补特征需重匹配 / 本步还有待办 / 已定码进套定额 / 无码跳过该件。"""
    if state.get("needs_rematch"):
        return "rematch"
    if hitl.evaluate_hitl(state, step="bill_match") is not None:
        return "loop"
    return "has_code" if (_item(state).get("code") or {}).get("value") else "skip"


# ══════════════════════════════════════════════════════════════════════════
# 步骤 2 —— 套定额（compute: price_compose 取数 + 唯一/高置信子目自动过）
# ══════════════════════════════════════════════════════════════════════════


def quota_compute(state: CostTaskState) -> dict[str, Any]:
    """套定额 compute：对已钉 code 调 price_compose，拆定额信封 + 信息价材料块；唯一/模型高置信子目自动过。**不暂停**。"""
    item = _item(state)
    code = (item.get("code") or {}).get("value")
    region, spec = state["region"], state["spec_version"]
    try:
        bundle = provenance.from_price_compose(region, code, spec)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 501:  # 该版本组价未就绪 → 空块透传（不阻断）
            item["quota"] = {"status": "未就绪"}
            item["materials"] = []
            return {"items": _put_item(state, item),
                    "events": [{"step": "from_price_compose", "status": "未就绪",
                                "provenance": {"source_ref": f"spec={spec} 组价数据未就绪"}, "paused": False}]}
        if status == 404:  # 选错码/无此清单项 → 空 quotas 走缺定额闸（可补录/放弃/改码）
            detail = (exc.response.text if exc.response is not None else str(exc))[:120]
            item["quota"] = {"envelope": {"step": "pick_quota", "status": provenance.STATUS_NO_SOURCE,
                                          "result": {"quotas": []},
                                          "provenance": {"source_type": "quota_lib",
                                                         "source_ref": f"取数 404：{code} 在 spec={spec} 无组价数据（{detail}）"}}}
            item["materials"] = []
            return {"items": _put_item(state, item),
                    "events": [{"step": "from_price_compose", "status": "no_source",
                                "provenance": {"source_ref": f"编码 {code} 在 spec={spec} 无组价数据（404，可能选码有误）"},
                                "paused": False}]}
        raise

    env = bundle["quota_envelope"]
    item["quota"] = {"envelope": env}
    item["materials"] = bundle["materials"]
    quotas = env.get("result", {}).get("quotas", []) or []

    # 自动过：唯一子目直接钉；多子目跑套定额模型，高置信则钉，否则留给 select_quota hitl。
    picked = None
    if len(quotas) == 1:
        picked = quotas[0]
        prov = env["provenance"]
    elif len(quotas) > 1:
        sel = quota_selection.select_quota(item.get("feature"), code, quotas)
        if sel.get("子目号") and not sel.get("need_review"):
            picked = next((q for q in quotas if str(q.get("子目号")) == str(sel["子目号"])), None)
            prov = {**env["provenance"], "confidence": sel.get("confidence"), "selector": "quota_model"}
    if picked is not None:
        item["quota"]["value"] = picked.get("子目号")
        item["quota"]["provenance"] = prov
        item["quota_basis"] = picked
        item["quota_decided"] = True

    return {"items": _put_item(state, item), "events": [provenance_event(env, paused=False)]}


# ══════════════════════════════════════════════════════════════════════════
# 步骤 4 —— 计算（compute: 逐件综合单价 + 两级汇总）
# ══════════════════════════════════════════════════════════════════════════


def _unit_price_for(basis: dict[str, Any] | None, rates: dict[str, Any], quantity) -> dict[str, Any]:
    """按定额基价 + 费率 + 工程量算综合单价/综合合价（确定性，复用 compute_unit_price）；缺基价→missing_base。"""
    if not basis or any(basis.get(k) is None for k in ("labor_cost", "material_cost", "machine_cost")):
        return {"status": "missing_base"}
    inp = UnitPriceInput(
        labor_cost=float(basis["labor_cost"]), material_cost=float(basis["material_cost"]),
        machine_cost=float(basis["machine_cost"]), management_fee_rate=rates["management_fee_rate"],
        profit_rate=rates["profit_rate"], risk_rate=rates.get("risk_rate", 0.0),
        fee_base=rates["fee_base"], quantity=float(quantity), tax_rate=None,
    )
    return compute_unit_price(inp)


def _compute_rollup(state: CostTaskState) -> dict[str, Any]:
    """两级汇总各 item 综合合价 + 项目级费用 → 总造价（确定性，复用 rollup_hierarchy）。"""
    params = state.get("params") or {}
    rows = [
        HierarchyItem(
            single_work=(it.get("single_work") or DEFAULT_SINGLE_WORK),
            unit_work=(it.get("unit_work") or DEFAULT_UNIT_WORK),
            total_price=(it.get("unit_price") or {}).get("total_price"),
            feature=it.get("feature"),
        )
        for it in (state.get("items") or [])
    ]
    return rollup_hierarchy(HierarchyRollupInput(
        items=rows, measure_fee=float(params.get("measure_fee") or 0),
        other_fee=float(params.get("other_fee") or 0), fee_levy=float(params.get("fee_levy") or 0),
        tax_rate=params.get("tax_rate"),
    ))


def recompute_rollup(
    state: CostTaskState | dict[str, Any], *,
    params_override: dict[str, Any] | None = None,
    rates_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """what-if 重算总造价（保留旧 API：session.re_rollup 依赖）。套用覆盖费率/项目费用确定性重算，不碰图/LLM。"""
    working = dict(state)
    if rates_override:
        rates = {**(working.get("rates") or {}), **rates_override}
        items = [dict(it) for it in (working.get("items") or [])]
        for it in items:
            quantity, basis = it.get("quantity"), it.get("quota_basis")
            it["unit_price"] = ({"status": "missing_quantity"} if basis is not None and quantity is None
                                else _unit_price_for(basis, rates, quantity))
        working["items"], working["rates"] = items, rates
    if params_override:
        working["params"] = {**(working.get("params") or {}), **params_override}
    return {"rollup": _compute_rollup(working), "rates": working.get("rates"), "params": working.get("params")}


def compute_produce(state: CostTaskState) -> dict[str, Any]:
    """计算 compute：费率/税金已齐后，逐件算综合单价 + 两级汇总 → state.rollup。逐层发事件，**不暂停**。"""
    rates = state.get("rates") or {}
    items = [dict(it) for it in (state.get("items") or [])]
    events: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        quantity, basis = it.get("quantity"), it.get("quota_basis")
        up = ({"status": "missing_quantity"} if basis is not None and quantity is None
              else _unit_price_for(basis, rates, quantity))
        it["unit_price"] = up
        events.append({"step": f"compute_unit_price[{i}]", "status": up.get("status", "ok"),
                       "provenance": up.get("provenance"), "result": up, "paused": False})
    working = dict(state)
    working["items"] = items
    rollup = _compute_rollup(working)
    for s in rollup.get("single_works", []):
        for u in s.get("unit_works", []):
            events.append({"step": f"unit_rollup:{u['name']}", "status": "ok", "result": u, "paused": False})
        events.append({"step": f"single_rollup:{s['name']}", "status": "ok", "result": s, "paused": False})
    events.append({"step": "rollup", "status": "ok", "provenance": rollup.get("provenance"),
                   "result": rollup, "paused": False})
    return {"items": items, "rollup": rollup, "status": "running", "events": events}


# ══════════════════════════════════════════════════════════════════════════
# setup / advance / 终态节点
# ══════════════════════════════════════════════════════════════════════════


def setup_node(state: CostTaskState) -> dict[str, Any]:
    """setup（§4.0 口径归一）：缺版本不反问，默认深圳·2013；初始化 4 步版 HITL 通道。"""
    spec = state.get("spec_version")
    events: list[dict[str, Any]] = []
    if not spec:
        spec = COST_DEFAULT_SPEC
        events.append({"step": "caliber", "paused": False, "status": "defaulted",
                       "provenance": {"source_type": "policy",
                                      "source_ref": f"版本未指明 → 默认口径 深圳·{spec}（不反问）"},
                       "detail": {"spec_version": spec, "region": state.get("region") or "深圳",
                                  "spec_source": "default"}})
    return {
        "spec_version": spec, "region": state.get("region") or "深圳",
        "price_source": state.get("price_source") or "local", "status": "running",
        "current_item": state.get("current_item", 0), "resolved_tasks": state.get("resolved_tasks") or [],
        "pending_human_task": None, "hitl_result": None,
        **({"events": events} if events else {}),
    }


def advance_node(state: CostTaskState) -> dict[str, Any]:
    """多构件推进：current_item+1。"""
    return {"current_item": (state.get("current_item", 0) + 1)}


def _route_advance(state: CostTaskState) -> str:
    """advance 后路由：还有未办件→回清单匹配；全办完&有可算件→项目收尾；无可算件→无价终止。"""
    items = state.get("items") or []
    if state.get("current_item", 0) < len(items):
        return "next_item"
    return "project" if gates.has_priceable_item(items) else "no_pricing"


def _unpriceable_reason(item: dict[str, Any]) -> str:
    if not (item.get("code") or {}).get("value"):
        return "未选出清单编码"
    if (item.get("quota") or {}).get("status") == "未就绪":
        return "该版本组价数据未就绪（如 2013）"
    return "无定额映射且未补录人材机基价"


def no_pricing_node(state: CostTaskState) -> dict[str, Any]:
    """无可算价终态（P0）：全部构件都无定额基价 → blocked，不产虚构总价。"""
    unresolved = [
        {"item": i, "code": (it.get("code") or {}).get("value"), "feature": it.get("feature"),
         "reason": _unpriceable_reason(it)}
        for i, it in enumerate(state.get("items") or [])
        if not gates.basis_complete((it or {}).get("quota_basis"))
    ]
    causes = {u["reason"] for u in unresolved}
    detail = next(iter(causes)) if len(causes) == 1 else (
        "存在未选码 / 版本未就绪 / 缺定额映射等构件" if causes else "无可算价构件")
    reason = f"无法组价到总价（{detail}）"
    return {"status": "blocked",
            "rollup": {"blocked_reason": reason, "unpriceable_items": unresolved},
            "audit_log": [audit_entry("no_pricing", "blocked", {"unpriceable": unresolved}, by="model")],
            "events": [{"step": "no_pricing", "status": "blocked",
                        "provenance": {"source_type": "quota_lib", "source_ref": "缺定额映射，未算总价（不虚构）"},
                        "result": {"blocked_reason": reason, "unpriceable_items": unresolved}, "paused": False}]}


def done_node(state: CostTaskState) -> dict[str, Any]:
    """收尾：有任一构件算出综合合价 → done；全无 → blocked。"""
    items = state.get("items") or []
    any_ok = any((it.get("unit_price") or {}).get("total_price") is not None for it in items)
    return {"status": "done" if any_ok else "blocked"}


# ══════════════════════════════════════════════════════════════════════════
# 组图
# ══════════════════════════════════════════════════════════════════════════


def build_graph(checkpointer: Any):
    """组装并编译 4 步版可中断组价图（保留旧签名，session.py 无需改导入路径即可切换）。"""
    g = StateGraph(CostTaskState)

    # —— 节点 ——
    g.add_node("setup", setup_node)
    # 步骤1 清单匹配
    g.add_node("bill_match_compute", bill_match_compute)
    g.add_node("bill_match_hitl", _hitl("bill_match"))
    g.add_node("bill_match_apply", apply_node)
    # 步骤2 套定额
    g.add_node("quota_compute", quota_compute)
    g.add_node("quota_hitl", _hitl("quota"))
    g.add_node("quota_apply", apply_node)
    # 步骤3 询价
    g.add_node("pricing_hitl", _hitl("pricing"))
    g.add_node("pricing_apply", apply_node)
    # 步骤4 计算·件（工程量，ITEM 作用域）
    g.add_node("quantity_hitl", _hitl("compute", Scope.ITEM))
    g.add_node("quantity_apply", apply_node)
    g.add_node("advance", advance_node)
    # 步骤4 计算·项目（费率/税金，PROJECT 作用域）
    g.add_node("project_hitl", _hitl("compute", Scope.PROJECT))
    g.add_node("project_apply", apply_node)
    g.add_node("compute_produce", compute_produce)
    # 步骤4 终审（PROJECT 作用域，产出后 final_approval 才命中）
    g.add_node("review_hitl", _hitl("compute", Scope.PROJECT))
    g.add_node("review_apply", apply_node)
    # 终态
    g.add_node("no_pricing", no_pricing_node)
    g.add_node("done", done_node)

    # —— 边 ——
    g.add_edge(START, "setup")
    g.add_edge("setup", "bill_match_compute")

    # 步骤1：compute → hitl → apply →（重匹配回环 / 本步待办 / 有码进套定额 / 无码跳过）
    g.add_edge("bill_match_compute", "bill_match_hitl")
    g.add_edge("bill_match_hitl", "bill_match_apply")
    g.add_conditional_edges("bill_match_apply", _route_bill_match, {
        "rematch": "bill_match_compute", "loop": "bill_match_hitl",
        "has_code": "quota_compute", "skip": "advance"})

    # 步骤2：compute → hitl ⇄ apply → 询价
    g.add_edge("quota_compute", "quota_hitl")
    g.add_edge("quota_hitl", "quota_apply")
    g.add_conditional_edges("quota_apply", _step_pending("quota"),
                            {"loop": "quota_hitl", "next": "pricing_hitl"})

    # 步骤3：hitl ⇄ apply → 计算·件
    g.add_edge("pricing_hitl", "pricing_apply")
    g.add_conditional_edges("pricing_apply", _step_pending("pricing"),
                            {"loop": "pricing_hitl", "next": "quantity_hitl"})

    # 步骤4·件：工程量 hitl ⇄ apply → advance
    g.add_edge("quantity_hitl", "quantity_apply")
    g.add_conditional_edges("quantity_apply", _step_pending("compute", Scope.ITEM),
                            {"loop": "quantity_hitl", "next": "advance"})

    # 外层循环：还有件→回清单匹配；全办完→项目收尾 / 无价终止
    g.add_conditional_edges("advance", _route_advance, {
        "next_item": "bill_match_compute", "project": "project_hitl", "no_pricing": "no_pricing"})

    # 步骤4·项目：费率/税金 hitl ⇄ apply → 汇总 compute
    g.add_edge("project_hitl", "project_apply")
    g.add_conditional_edges("project_apply", _step_pending("compute", Scope.PROJECT),
                            {"loop": "project_hitl", "next": "compute_produce"})

    # 汇总产出 → 终审 hitl ⇄ apply → done（产出后 project 作用域只剩 final_approval 命中）
    g.add_edge("compute_produce", "review_hitl")
    g.add_edge("review_hitl", "review_apply")
    g.add_conditional_edges("review_apply", _step_pending("compute", Scope.PROJECT),
                            {"loop": "review_hitl", "next": "done"})

    g.add_edge("no_pricing", END)
    g.add_edge("done", END)

    return g.compile(checkpointer=checkpointer)
