"""可中断组价状态机（langgraph）—— HITL 图骨架（设计 §3.1 前四节点 + 收尾）。

链路：``setup → list_match → list_gate →（有码?）compose → quota_gate → price_gate → done``。
- **compute / gate 双拆**：原语调用（list_match / compose，含 LLM 与知识层取数）放「compute 节点」，
  暂停闸放「gate 节点」。因 langgraph 的 ``interrupt()`` 在 resume 时**会从节点头部重跑**——把昂贵且
  非确定性的 LLM 调用单独放上游 compute 节点（节点间有 checkpoint，跑且仅跑一次），gate 节点只读 state +
  interrupt（幂等、便宜），避免 resume 重跑 LLM 漂移（原则 3）。
- 每个节点完成都往 ``state["events"]`` 追加一条 provenance 事件（前端依据卡数据源），无论是否暂停。
- 是否跳闸的判断全在 ``gates`` 代码里，弱模型不驱动流程（§1.2 / §10）。

本期是骨架：``done`` 之前预留综合单价 / 措施 / 规费 / 末尾 review 节点挂点（§3.1 后续节点）。
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from common.config import HITL_CONFIDENCE_TAU, LLM_MODEL_ID, LLM_URL
from cost import gates, provenance
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
                             "price_status": price.get("status")},
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


def done_node(state: CostTaskState) -> dict[str, Any]:
    """收尾（§3⑬挂点）：置 done。本期不接综合单价/rollup，留作后续节点挂点。"""
    return {"status": "done"}


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
    g.add_node("done", done_node)

    g.add_edge(START, "setup")
    g.add_edge("setup", "list_match")
    g.add_edge("list_match", "list_gate")
    g.add_conditional_edges("list_gate", _has_code, {"compose": "compose", "done": "done"})
    g.add_edge("compose", "quota_gate")
    g.add_edge("quota_gate", "price_gate")
    g.add_edge("price_gate", "done")
    g.add_edge("done", END)

    return g.compile(checkpointer=checkpointer)
