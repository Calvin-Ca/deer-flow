"""可中断组价状态机（langgraph）—— HITL 全 13 步图（设计 §3.1）。

链路：``setup → list_match → list_gate →（有码?）compose → quota_gate → price_gate →
       rates_gate → params_gate → rollup → done``。
- **compute / gate 双拆**：原语调用（list_match / compose，含 LLM 与知识层取数）放「compute 节点」，
  暂停闸放「gate 节点」。因 langgraph 的 ``interrupt()`` 在 resume 时**会从节点头部重跑**——把昂贵且
  非确定性的 LLM 调用单独放上游 compute 节点（节点间有 checkpoint，跑且仅跑一次），gate 节点只读 state +
  interrupt（幂等、便宜），避免 resume 重跑 LLM 漂移（原则 3）。后段费率/参数/汇总均为**确定性算钱**
  （``compute_unit_price`` / ``rollup_cost``，无 LLM），故 interrupt 与计算同节点、resume 重跑无漂移。
- 每个节点完成都往 ``state["events"]`` 追加一条 provenance 事件（前端依据卡数据源），无论是否暂停。
- 是否跳闸的判断全在 ``gates`` 代码里，弱模型不驱动流程（§1.2 / §10）。

后段三节点（§8 综合单价费率 / §10⑪§12 项目级费用 / §13 末尾 review）：费率/参数走录入闸（缺政策数则停、
不杜撰），rollup 始终暂停做总造价复核（§6「末尾 review 始终暂停」）。
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from common.config import HITL_CONFIDENCE_TAU, LLM_MODEL_ID, LLM_URL
from cost import gates, provenance
from cost.pricing import RollupInput, UnitPriceInput, compute_unit_price, rollup_cost
from cost.state import (
    CostTaskState,
    audit_entry,
    lock_value,
    override_entry,
    provenance_event,
)

logger = logging.getLogger("ce-services.cost.hitl")

TOP_K = 10


def _item(state: CostTaskState) -> dict[str, Any]:
    """取本期单构件 item（items[0]）；缺则按 feature 初始化一个空 item。"""
    items = state.get("items") or [{"feature": state.get("feature")}]
    return dict(items[0])


# ── 节点 ──

def setup_node(state: CostTaskState) -> dict[str, Any]:
    """setup 闸（§3①/§6）：必填项（spec/region）缺失才暂停收集，齐了直接过。

    参数：state —— 当前任务状态。
    返回：状态增量。spec/region 缺失 → ``interrupt(input_payload)`` 收集
      spec/region/period/price_source；齐全（start 已带）→ 不暂停、直接置 running（§6「已有默认值自动过」）。
    """
    missing = [f for f in ("spec_version", "region") if not state.get(f)]
    if missing:
        data = interrupt(
            gates.input_payload(
                "setup",
                "请确认组价口径",
                [
                    {"key": "spec_version", "type": "enum", "label": "国标版本",
                     "options": ["2013", "2024"], "required": True},
                    {"key": "region", "type": "text", "label": "地区", "default": "深圳"},
                    {"key": "period", "type": "month", "label": "信息价期号（年月）", "required": False},
                    {"key": "price_source", "type": "enum", "label": "信息价来源",
                     "options": ["local", "online", "manual"], "default": "local"},
                ],
            )
        )
        return {
            "spec_version": data.get("spec_version") or state.get("spec_version"),
            "region": data.get("region") or state.get("region") or "深圳",
            "period": data.get("period") or state.get("period"),
            "price_source": data.get("price_source") or state.get("price_source") or "local",
            "status": "running",
        }
    return {
        "region": state.get("region") or "深圳",
        "price_source": state.get("price_source") or "local",
        "status": "running",
    }


def list_match_node(state: CostTaskState) -> dict[str, Any]:
    """编码 compute（§3②）：bill_match 召回 + select_code 选码（含 LLM），结果入 item，**不暂停**。

    参数：state。返回：item.code 挂上 list_match 信封 + 一条 provenance 事件。
    LLM 在此唯一一次调用（与 gate 拆开，resume 不重跑）。
    """
    item = _item(state)
    env = provenance.list_match(item.get("feature"), state["spec_version"], TOP_K, LLM_URL, LLM_MODEL_ID)
    item["code"] = {"envelope": env, "value": env["result"].get("code"), "locked": False}
    return {"items": [item], "events": [provenance_event(env, paused=False)]}


def list_gate_node(state: CostTaskState) -> dict[str, Any]:
    """编码确认闸（§3②/§6）：高置信唯一码自动过，否则暂停等确认；resume 后钉码（原则 3）。

    参数：state。返回：item.code 钉成 ``lock_value`` + 审计/override 记录。
    """
    item = _item(state)
    env = item["code"]["envelope"]
    pause = gates.should_pause_coding(env, HITL_CONFIDENCE_TAU)
    if pause:
        decision = interrupt(gates.confirm_payload("list_coding", env, "请确认清单编码"))
        value, prov, action = gates.apply_confirm_decision(env, decision)
        by = "user"
    else:
        value, prov, action = env["result"].get("code"), env["provenance"], "auto_pass"
        by = "model"

    item["code"] = lock_value(value, prov, by=by)
    out: dict[str, Any] = {
        "items": [item],
        "audit_log": [audit_entry("list_coding", action, {"code": value}, by=by)],
    }
    if by == "user" and action in ("manual_override", "select_alternative"):
        out["overrides"] = [override_entry("code", 0, value, by="user")]
    if not value:
        out["status"] = "blocked"  # 选不出码且用户也未给值 → 阻塞，不硬编（§10）
    return out


def compose_node(state: CostTaskState) -> dict[str, Any]:
    """组价取数 compute（§3④⑥）：对已钉 code 调 price_compose，拆定额块 + 信息价材料块入 item，**不暂停**。

    参数：state。返回：item 挂 quota 信封与 materials；spec 未就绪（501）降级为空块、status 标未就绪（§10 透传）。
    """
    item = _item(state)
    code = (item.get("code") or {}).get("value")
    region, spec = state["region"], state["spec_version"]
    try:
        bundle = provenance.from_price_compose(region, code, spec)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 501:
            item["quota"] = {"status": "未就绪"}
            item["materials"] = []
            return {
                "items": [item],
                "status": "running",
                "events": [{"step": "from_price_compose", "status": "未就绪",
                            "provenance": {"source_ref": f"spec={spec} 组价数据未就绪"}, "paused": False}],
            }
        raise

    item["quota"] = {"envelope": bundle["quota_envelope"], "locked": False}
    item["materials"] = bundle["materials"]
    return {
        "items": [item],
        "events": [provenance_event(bundle["quota_envelope"], paused=False)],
    }


def quota_gate_node(state: CostTaskState) -> dict[str, Any]:
    """定额确认闸（§3④/§6）：唯一子目自动过，多子目/无子目暂停；resume 钉子目。

    参数：state。返回：item.quota 钉值 + 审计。无定额块（如未就绪）→ 透传跳过。
    """
    item = _item(state)
    quota = item.get("quota") or {}
    env = quota.get("envelope")
    if env is None:  # compose 未就绪/无定额块，跳过本闸
        return {"status": state.get("status", "running")}

    if gates.should_pause_quota(env):
        decision = interrupt(gates.confirm_payload("quota", env, "请确认套用定额子目"))
        value, prov, action = gates.apply_confirm_decision(env, decision)
        by = "user"
    else:
        value = env["result"]["quotas"][0].get("子目号")
        prov, action, by = env["provenance"], "auto_pass", "model"

    item["quota"] = lock_value(value, prov, by=by)
    # 保留所选子目的定额基价（人材机净价）供 §8 综合单价计算；手填/越界子目无对应基价 → None（下游 missing_base 透传）。
    quotas = env.get("result", {}).get("quotas", [])
    item["quota_basis"] = next((q for q in quotas if str(q.get("子目号")) == str(value)), None)
    return {
        "items": [item],
        "audit_log": [audit_entry("quota", action, {"子目号": value}, by=by)],
    }


def price_gate_node(state: CostTaskState) -> dict[str, Any]:
    """信息价逐项例外闸（§3⑥/§7）：命中价自动过，缺价/状态非 ok 的逐项暂停录入。

    参数：state。返回：item.materials 逐条补价 + 缺价项的 override/审计。
    多个缺价材料 → 节点内多次 interrupt（resume 逐个回填，langgraph 按序匹配）；命中的绝不问（§7 反例）。
    """
    item = _item(state)
    materials = item.get("materials") or []
    overrides: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    for idx, m in enumerate(materials):
        price = m.get("price", {})
        if gates.should_pause_price(price):
            data = interrupt(
                gates.input_payload(
                    f"price_item:{idx}",
                    f"信息价缺失，请录入「{m.get('std') or m.get('raw')}」单价",
                    [{"key": "value", "type": "number", "label": "单价（元）", "required": True}],
                    context={"raw": m.get("raw"), "std": m.get("std"), "unit": m.get("unit"),
                             "category": m.get("category"), "spec": m.get("spec"),
                             "consumption": m.get("consumption"), "price_status": price.get("status")},
                )
            )
            val = (data or {}).get("value")
            m["price"] = {
                "value": val,
                "status": "user_input",
                "provenance": {"source_type": "user_input", "source_ref": "用户录入"},
            }
            overrides.append(override_entry("price", idx, val, by="user"))
            audits.append(audit_entry("price_query", "manual_override", {"material": m.get("std"), "value": val}, by="user"))

    item["materials"] = materials
    out: dict[str, Any] = {"items": [item]}
    if overrides:
        out["overrides"] = overrides
        out["audit_log"] = audits
    else:
        out["audit_log"] = [audit_entry("price_query", "auto_pass", {"materials": len(materials)})]
    return out


def _unit_price_for(basis: dict[str, Any] | None, rates: dict[str, Any]) -> dict[str, Any]:
    """按定额基价 + 费率算单条综合单价（确定性，复用 ``compute_unit_price``）。

    参数：basis —— 所选定额子目的人材机净价 ``{labor_cost, material_cost, machine_cost}`` 或 None；
      rates —— 费率块（管理费/利润/风险率 + 取费基数）。
    返回：``compute_unit_price`` 结果；缺基价（手填子目/未就绪）→ ``{"status":"missing_base"}``（不杜撰基价）。
    口径：以定额基价为人材机费，综合单价**不含税**（税金在 §13 rollup 一次性计，GB 50500 §2.0.9）。
    """
    if not basis or any(basis.get(k) is None for k in ("labor_cost", "material_cost", "machine_cost")):
        return {"status": "missing_base"}
    inp = UnitPriceInput(
        labor_cost=float(basis["labor_cost"]),
        material_cost=float(basis["material_cost"]),
        machine_cost=float(basis["machine_cost"]),
        management_fee_rate=rates["management_fee_rate"],
        profit_rate=rates["profit_rate"],
        risk_rate=rates.get("risk_rate", 0.0),
        fee_base=rates["fee_base"],
        tax_rate=None,  # 综合单价不含税，税金在 rollup
    )
    return compute_unit_price(inp)


def rates_gate_node(state: CostTaskState) -> dict[str, Any]:
    """综合单价费率录入闸（§3⑧/§6）：有费率自动过、缺政策数（管理费/利润/取费基数）停闸录入；钉率后算综合单价。

    参数：state。返回：state.rates 钉值 + item.unit_price（compute_unit_price 结果）+ 审计/override。
    费率是政策数（库内无），缺则停（gates.should_pause_rates）；齐则自动过。算钱确定性、不入 LLM（resume 重跑无漂移）。
    """
    rates = state.get("rates")
    paused = gates.should_pause_rates(rates)
    if paused:
        rates = interrupt(
            gates.input_payload(
                "rates",
                "请录入综合单价费率（库内无管理费/利润，须按工程类别给定）",
                [
                    {"key": "management_fee_rate", "type": "number", "label": "管理费率（%）", "required": True},
                    {"key": "profit_rate", "type": "number", "label": "利润率（%）", "required": True},
                    {"key": "risk_rate", "type": "number", "label": "风险费率（%）", "default": 0},
                    {"key": "fee_base", "type": "enum", "label": "取费基数（labor=人工费 / labor_machine=人工+机械 / lmm=人材机）",
                     "options": ["labor", "labor_machine", "lmm"], "required": True},
                ],
            )
        )

    item = _item(state)
    unit_price = _unit_price_for(item.get("quota_basis"), rates)
    item["unit_price"] = unit_price
    by = "user" if paused else "model"
    out: dict[str, Any] = {
        "rates": rates,
        "items": [item],
        "audit_log": [audit_entry("unit_price", "input" if paused else "auto_pass",
                                  {"rates": rates, "unit_price_status": unit_price.get("status", "ok")}, by=by)],
        "events": [{"step": "compute_unit_price", "status": unit_price.get("status", "ok"),
                    "provenance": unit_price.get("provenance"), "result": unit_price, "paused": paused}],
    }
    if paused:
        out["overrides"] = [override_entry("rates", 0, rates, by="user")]
    return out


def params_gate_node(state: CostTaskState) -> dict[str, Any]:
    """项目级费用录入闸（§3⑩⑪⑫/§6）：有参数自动过、缺税金率停闸录入措施/其他/规费/税金率。

    参数：state。返回：state.params 钉值 + 审计/override。税金率是政策数须显式给（gates.should_pause_params），
      措施/其他/规费可缺省 0。本节点只采集、不算钱（汇总在 rollup）。
    """
    params = state.get("params")
    paused = gates.should_pause_params(params)
    if paused:
        params = interrupt(
            gates.input_payload(
                "params",
                "请录入项目级费用（措施/其他/规费）与税金率",
                [
                    {"key": "measure_fee", "type": "number", "label": "措施项目费（元）", "default": 0},
                    {"key": "other_fee", "type": "number", "label": "其他项目费（元）", "default": 0},
                    {"key": "fee_levy", "type": "number", "label": "规费（元）", "default": 0},
                    {"key": "tax_rate", "type": "number", "label": "税金率（%）", "required": True},
                ],
            )
        )
    by = "user" if paused else "model"
    out: dict[str, Any] = {
        "params": params,
        "audit_log": [audit_entry("project_params", "input" if paused else "auto_pass", {"params": params}, by=by)],
    }
    if paused:
        out["overrides"] = [override_entry("params", 0, params, by="user")]
    return out


def _compute_rollup(state: CostTaskState) -> dict[str, Any]:
    """汇总各 item 综合合价 + 项目级费用 → 总造价（确定性，复用 ``rollup_cost``）。

    参数：state（含 items[].unit_price 与 params）。返回：rollup_cost 结果（含 missing 计数提示）。
    分部分项合价 = Σ 各 item 综合合价（total_price）；缺综合单价（missing_base）的 item 计入 missing、不计金额（不杜撰）。
    """
    subtotal = 0.0
    missing = 0
    for it in state.get("items") or []:
        up = it.get("unit_price") or {}
        total = up.get("total_price")
        if total is not None:
            subtotal += total
        else:
            missing += 1
    params = state.get("params") or {}
    result = rollup_cost(RollupInput(
        subtotal=subtotal,
        measure_fee=float(params.get("measure_fee") or 0),
        other_fee=float(params.get("other_fee") or 0),
        fee_levy=float(params.get("fee_levy") or 0),
        tax_rate=params.get("tax_rate"),
    ))
    result["missing_unit_price_items"] = missing
    return result


def rollup_node(state: CostTaskState) -> dict[str, Any]:
    """末尾 review（§3⑬/§6）：确定性汇总总造价后**始终暂停**复核，resume(approve) → done。

    参数：state。返回：state.rollup（总造价明细）+ status=done + 审计。
    §6「末尾 review 始终暂停」：总造价定稿前必看，故无条件 interrupt（汇总确定性、resume 重跑无漂移）。
    """
    rollup = _compute_rollup(state)
    interrupt({
        "gate_type": "review",
        "node": "rollup",
        "title": "请复核总造价",
        "rollup": rollup,
        "actions": ["approve"],
    })
    return {
        "rollup": rollup,
        "status": "done",
        "audit_log": [audit_entry("rollup", "final_review", {"total": rollup.get("total"),
                                  "pre_tax_total": rollup.get("pre_tax_total")}, by="user")],
        "events": [{"step": "rollup", "status": "ok", "provenance": rollup.get("provenance"),
                    "result": rollup, "paused": True}],
    }


def done_node(state: CostTaskState) -> dict[str, Any]:
    """收尾（§3⑬）：终态置 done；选不出码的 blocked 分支直达此节点（跳过算钱）时保留 blocked，不掩盖缺口。"""
    return {"status": "blocked" if state.get("status") == "blocked" else "done"}


def _has_code(state: CostTaskState) -> str:
    """list_gate 后路由：已钉到码 → 继续 compose；否则（blocked）直接收尾。"""
    item = _item(state)
    return "compose" if (item.get("code") or {}).get("value") else "done"


def build_graph(checkpointer: Any):
    """组装并编译可中断组价图。

    参数：checkpointer —— langgraph checkpointer（SqliteSaver），按 thread_id 持久化、支撑暂停恢复。
    返回：编译后的图（``invoke``/``get_state`` 用 ``config={"configurable":{"thread_id":task_id}}``）。
    """
    g = StateGraph(CostTaskState)
    g.add_node("setup", setup_node)
    g.add_node("list_match", list_match_node)
    g.add_node("list_gate", list_gate_node)
    g.add_node("compose", compose_node)
    g.add_node("quota_gate", quota_gate_node)
    g.add_node("price_gate", price_gate_node)
    g.add_node("rates_gate", rates_gate_node)
    g.add_node("params_gate", params_gate_node)
    g.add_node("rollup", rollup_node)
    g.add_node("done", done_node)

    g.add_edge(START, "setup")
    g.add_edge("setup", "list_match")
    g.add_edge("list_match", "list_gate")
    g.add_conditional_edges("list_gate", _has_code, {"compose": "compose", "done": "done"})
    g.add_edge("compose", "quota_gate")
    g.add_edge("quota_gate", "price_gate")
    g.add_edge("price_gate", "rates_gate")
    g.add_edge("rates_gate", "params_gate")
    g.add_edge("params_gate", "rollup")
    g.add_edge("rollup", "done")
    g.add_edge("done", END)

    return g.compile(checkpointer=checkpointer)
