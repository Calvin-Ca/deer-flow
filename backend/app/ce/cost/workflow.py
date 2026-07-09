"""Application-level CE cost workflow backed by a LangGraph checkpointer."""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .nodes import _components_from_quotas, extract_quota_schemes, price_review_node, run_business_step, run_node, select_bill_node, select_quota_node
from .state import CostNodeName, normalize_feature_items, normalize_region, normalize_spec


class CostWorkflowState(TypedDict, total=False):
    task_id: str
    created_at: str
    updated_at: str
    status: str
    spec: str
    region: str
    period: str | None
    top_k: int
    items: list[dict[str, Any]]
    current_index: int
    events: list[dict[str, Any]]
    interrupt: dict[str, Any] | None
    pending: dict[str, Any] | None
    blocked_reason: dict[str, Any] | None
    result: dict[str, Any]
    step_strategies: dict[str, str] | None
    settle_target: str | None
    management_rate: float | None
    profit_rate: float | None
    risk_rate: float | None


_GRAPH_LOCK = threading.RLock()
_GRAPH = None
_GRAPH_CHECKPOINTER_ID: int | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(state)


def _append_event(state: dict[str, Any], node: str, status: str, payload: dict[str, Any] | None = None) -> None:
    event = {"ts": _now(), "node": node, "status": status}
    if payload:
        event["payload"] = payload
    state.setdefault("events", []).append(event)


def _build_result(state: dict[str, Any]) -> dict[str, Any]:
    priced_items: list[dict[str, Any]] = []
    for item in state.get("items", []):
        if not isinstance(item, dict):
            continue
        selection = item.get("selection") or {}
        price = item.get("price_compose") or item.get("quota_compose") or {}
        priced_items.append(
            {
                "description": item.get("description"),
                "selected_code": selection.get("selected_code"),
                "selection_source": selection.get("selection_source"),
                "price_status": price.get("status"),
                "price": price.get("result"),
                "quantity": item.get("quantity"),
                "unit_price": item.get("unit_price"),
            }
        )
    return {"items": priced_items}


def _strategy_for(state: dict[str, Any], step: str) -> str:
    strategies = state.get("step_strategies")
    if not isinstance(strategies, dict):
        return "tool"
    strategy = strategies.get(step)
    return strategy if strategy in {"agent", "tool", "llm"} else "tool"


def _block(state: dict[str, Any], node_result: dict[str, Any]) -> dict[str, Any]:
    state["status"] = "blocked"
    state["interrupt"] = None
    state["pending"] = None
    state["blocked_reason"] = node_result
    state["result"] = _build_result(state)
    _append_event(state, str(node_result.get("node") or "workflow"), "blocked", node_result)
    return state


def _selected_quotas(item: dict[str, Any]) -> list[dict[str, Any]]:
    """取当前采用的定额子目（选定方案的 quotas，否则回退 price_compose 全量）。

    功能：为 price_review 询价确定扫描范围——用户选过定额方案则用该方案的子目，否则用整套组价。
    参数：item —— 单条组价 item（含 quota_selection / price_compose）。
    返回：定额子目列表（含 resources）；无则空列表。
    """
    selection = item.get("quota_selection") or {}
    scheme = selection.get("selected_scheme")
    if isinstance(scheme, dict) and isinstance(scheme.get("quotas"), list) and scheme["quotas"]:
        return scheme["quotas"]
    compose = item.get("price_compose") or {}
    result = compose.get("result") if isinstance(compose, dict) else None
    if isinstance(result, dict) and isinstance(result.get("quotas"), list):
        return result["quotas"]
    return []


def _workflow_step(input_state: CostWorkflowState) -> CostWorkflowState:
    """Advance the graph by exactly one workflow node.

    LangGraph checkpoints after every graph step. Keeping this function to one
    domain-node transition makes resume/replay inspectable without giving the
    lead agent procedural control over the workflow.
    """
    state: dict[str, Any] = _snapshot(dict(input_state))
    status = state.get("status")
    if status in {"awaiting_input", "blocked", "done"}:
        return state

    items = state.get("items", [])
    if not isinstance(items, list) or not items:
        state["status"] = "awaiting_input"
        state["interrupt"] = {
            "gate_type": "input",
            "node": "workflow_start",
            "question": "请提供要组价的构件或做法描述。",
            "required_fields": ["feature"],
        }
        state["result"] = {"items": []}
        state["updated_at"] = _now()
        _append_event(state, "workflow_start", "awaiting_input")
        return state

    current_index = int(state.get("current_index") or 0)
    if current_index >= len(items):
        state["status"] = "done"
        state["interrupt"] = None
        state["pending"] = None
        state["result"] = _build_result(state)
        state["updated_at"] = _now()
        _append_event(state, "workflow", "done")
        return state

    item = items[current_index]
    if not isinstance(item, dict):
        return _block(state, {"node": "workflow", "status": "blocked", "error": f"item {current_index} is not an object"})

    base_payload = {
        "spec": state["spec"],
        "region": state["region"],
        "period": state.get("period"),
        "top_k": state.get("top_k", 10),
    }

    if "bill_match" not in item:
        result = run_business_step("bill_match", {**base_payload, **item}, strategy=_strategy_for(state, "bill_match"))
        item["bill_match"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "bill_match", result.get("status", "unknown"), {"index": current_index})
        if result.get("status") == "blocked":
            return _block(state, result)
        return state

    if "selection" not in item:
        result = select_bill_node(
            {
                **base_payload,
                "candidates": item.get("bill_match", {}).get("candidates", []),
            }
        )
        item["selection"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "select_bill", result.get("status", "unknown"), {"index": current_index})
        if result.get("status") == "awaiting_input":
            state["status"] = "awaiting_input"
            state["pending"] = {"node": "select_bill", "index": current_index}
            state["interrupt"] = result.get("interrupt")
            state["result"] = _build_result(state)
            return state
        if result.get("status") != "done":
            return _block(state, result)
        return state

    selected_code = item.get("selection", {}).get("selected_code")
    if selected_code and "price_compose" not in item:
        result = run_business_step("quota_compose", {**base_payload, "code": selected_code}, strategy=_strategy_for(state, "quota_compose"))
        item["price_compose"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "quota_compose", result.get("status", "unknown"), {"index": current_index})
        if result.get("status") == "blocked":
            return _block(state, result)
        # 单方案（服务端未返回多套可替代方案）直接采用，不额外中断；多方案留给下一步复核。
        schemes = extract_quota_schemes(result)
        if len(schemes) <= 1:
            item["quota_selection"] = {
                "node": "select_quota",
                "status": "done",
                "selection_source": "auto_single_scheme",
                "selected_scheme": schemes[0] if schemes else None,
            }
            state["items"] = items
        return state

    if "price_compose" in item and "quota_selection" not in item:
        # 存在多套可替代定额方案 → 复核选一套（仅低置信/多方案相近才停）。
        result = select_quota_node({**base_payload, "schemes": extract_quota_schemes(item["price_compose"])})
        item["quota_selection"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "select_quota", result.get("status", "unknown"), {"index": current_index})
        if result.get("status") == "awaiting_input":
            state["status"] = "awaiting_input"
            state["pending"] = {"node": "select_quota", "index": current_index}
            state["interrupt"] = result.get("interrupt")
            state["result"] = _build_result(state)
            return state
        if result.get("status") != "done":
            return _block(state, result)
        return state

    if "quota_selection" in item and "price_review" not in item:
        # 选定组价的工料机询价：信息价缺失（no_source）的人材机 → 人工补价（仅有缺价料才停）。
        result = price_review_node({"quotas": _selected_quotas(item), "region": state["region"], "period": state.get("period")})
        item["price_review"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "price_review", result.get("status", "unknown"), {"index": current_index})
        if result.get("status") == "awaiting_input":
            state["status"] = "awaiting_input"
            state["pending"] = {"node": "price_review", "index": current_index}
            state["interrupt"] = result.get("interrupt")
            state["result"] = _build_result(state)
            return state
        if result.get("status") != "done":
            return _block(state, result)
        return state

    if "price_review" in item and "settle" not in item:
        # 结算：选定方案+已询价的工料机 → compute 引擎算到 settle_target（默认综合单价）。
        # 目标层无确定性公式时 compute 返回 capability_gap，停下交模型按规则试算后 resume。
        target = state.get("settle_target") or "unit_rate"
        result = run_node(
            "compute",
            {
                "target": target,
                "components": _components_from_quotas(_selected_quotas(item)),
                "management_rate": state.get("management_rate"),
                "profit_rate": state.get("profit_rate"),
                "risk_rate": state.get("risk_rate"),
                "quantity": item.get("quantity"),
            },
        )
        item["settle"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "settle", result.get("status", "unknown"), {"index": current_index, "target": target})
        if result.get("status") == "awaiting_input":
            gate = result.get("interrupt") or {}
            state["status"] = "awaiting_input"
            state["pending"] = {"node": "settle", "index": current_index, "gate_type": gate.get("gate_type")}
            state["interrupt"] = gate
            state["result"] = _build_result(state)
            return state
        if result.get("status") != "done":
            return _block(state, result)
        return state

    if "unit_price" not in item and isinstance(item.get("components"), list):
        result = run_business_step("calc", {"operation": "unit_price", "components": item["components"]}, strategy=_strategy_for(state, "calc"))
        item["unit_price"] = result
        state["items"] = items
        state["updated_at"] = _now()
        _append_event(state, "calc", result.get("status", "unknown"), {"index": current_index})
        return state

    state["current_index"] = current_index + 1
    state["updated_at"] = _now()
    _append_event(state, "workflow", "advance_item", {"from_index": current_index, "to_index": current_index + 1})
    return state


def _route_after_step(state: CostWorkflowState) -> str:
    return "continue" if state.get("status") == "running" else "end"


def _get_graph():
    from deerflow.runtime.checkpointer import get_checkpointer

    global _GRAPH, _GRAPH_CHECKPOINTER_ID
    checkpointer = get_checkpointer()
    checkpointer_id = id(checkpointer)
    with _GRAPH_LOCK:
        if _GRAPH is not None and _GRAPH_CHECKPOINTER_ID == checkpointer_id:
            return _GRAPH

        builder = StateGraph(CostWorkflowState)
        builder.add_node("workflow_step", _workflow_step)
        builder.add_edge(START, "workflow_step")
        builder.add_conditional_edges("workflow_step", _route_after_step, {"continue": "workflow_step", "end": END})
        _GRAPH = builder.compile(checkpointer=checkpointer)
        _GRAPH_CHECKPOINTER_ID = checkpointer_id
        return _GRAPH


def _thread_config(task_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": task_id}}


def _graph_state(task_id: str) -> dict[str, Any]:
    snapshot = _get_graph().get_state(_thread_config(task_id))
    values = getattr(snapshot, "values", None) or {}
    return _snapshot(values)


def start_workflow(
    *,
    feature: str | None = None,
    features: list[dict[str, Any]] | None = None,
    spec: str | None = None,
    region: str | None = None,
    period: str | None = None,
    quantity: float | None = None,
    top_k: int = 10,
    step_strategies: dict[str, str] | None = None,
    settle_target: str | None = None,
    management_rate: float | None = None,
    profit_rate: float | None = None,
    risk_rate: float | None = None,
) -> dict[str, Any]:
    """Start a cost workflow and persist state through LangGraph checkpointer."""
    task_id = f"cost-{uuid.uuid4().hex[:12]}"
    state: CostWorkflowState = {
        "task_id": task_id,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "running",
        "spec": normalize_spec(spec),
        "region": normalize_region(region),
        "period": period,
        "top_k": top_k,
        "items": normalize_feature_items(feature=feature, features=features, quantity=quantity),
        "current_index": 0,
        "events": [],
        "interrupt": None,
        "pending": None,
        "step_strategies": step_strategies,
        "settle_target": settle_target,
        "management_rate": management_rate,
        "profit_rate": profit_rate,
        "risk_rate": risk_rate,
    }
    return _get_graph().invoke(state, config=_thread_config(task_id))


def resume_workflow(task_id: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resume a paused workflow by updating the checked-point graph state."""
    state = _graph_state(task_id)
    if not state:
        return {"status": "not_found", "task_id": task_id}
    if state.get("status") != "awaiting_input":
        return state

    pending = state.get("pending") or {}
    decision = decision or {}
    node = pending.get("node")
    index = pending.get("index")

    if node == "select_bill":
        if not isinstance(index, int) or index >= len(state.get("items", [])):
            state = _block(state, {"node": "select_bill", "status": "blocked", "error": "invalid pending index"})
            return _get_graph().invoke(state, config=_thread_config(task_id))

        selected_code = decision.get("selected_code") or decision.get("code") or decision.get("manual_input")
        if not isinstance(selected_code, str) or not selected_code.strip():
            # 未回传编码 → 用原节点重新生成同一 review 中断，继续等待。
            regen = select_bill_node({"candidates": state["items"][index].get("bill_match", {}).get("candidates", [])})
            state["interrupt"] = regen.get("interrupt")
            state["updated_at"] = _now()
            _get_graph().update_state(_thread_config(task_id), state)
            return state

        state["items"][index]["selection"] = select_bill_node(
            {
                "selected_code": selected_code.strip(),
                "candidates": state["items"][index].get("bill_match", {}).get("candidates", []),
                "reason": decision.get("reason"),
            }
        )
        _append_event(state, "select_bill", "human_selected", {"index": index, "selected_code": selected_code.strip()})

    elif node == "select_quota":
        if not isinstance(index, int) or index >= len(state.get("items", [])):
            state = _block(state, {"node": "select_quota", "status": "blocked", "error": "invalid pending index"})
            return _get_graph().invoke(state, config=_thread_config(task_id))

        schemes = extract_quota_schemes(state["items"][index].get("price_compose", {}))
        selected_scheme = decision.get("selected_scheme") or decision.get("scheme_id")
        manual_input = decision.get("manual_input")
        has_selection = (isinstance(selected_scheme, str) and selected_scheme.strip()) or (isinstance(manual_input, str) and manual_input.strip())
        if not has_selection:
            # 未回传方案 → 用原节点重新生成同一 review 中断，继续等待。
            regen = select_quota_node({"schemes": schemes})
            state["interrupt"] = regen.get("interrupt")
            state["updated_at"] = _now()
            _get_graph().update_state(_thread_config(task_id), state)
            return state

        state["items"][index]["quota_selection"] = select_quota_node(
            {
                "schemes": schemes,
                "selected_scheme": selected_scheme,
                "manual_input": manual_input,
                "reason": decision.get("reason"),
            }
        )
        _append_event(state, "select_quota", "human_selected", {"index": index, "selected_scheme": selected_scheme})

    elif node == "price_review":
        if not isinstance(index, int) or index >= len(state.get("items", [])):
            state = _block(state, {"node": "price_review", "status": "blocked", "error": "invalid pending index"})
            return _get_graph().invoke(state, config=_thread_config(task_id))

        item = state["items"][index]
        base = {"quotas": _selected_quotas(item), "region": state["region"], "period": state.get("period")}
        if not decision.get("prices"):
            # 未回传询价结果 → 用原节点重新生成同一询价中断，继续等待。
            regen = price_review_node(base)
            state["interrupt"] = regen.get("interrupt")
            state["updated_at"] = _now()
            _get_graph().update_state(_thread_config(task_id), state)
            return state

        state["items"][index]["price_review"] = price_review_node({**base, "prices": decision["prices"]})
        _append_event(state, "price_review", "human_priced", {"index": index})

    elif node == "settle":
        if not isinstance(index, int) or index >= len(state.get("items", [])):
            state = _block(state, {"node": "settle", "status": "blocked", "error": "invalid pending index"})
            return _get_graph().invoke(state, config=_thread_config(task_id))

        manual = decision.get("manual_result")
        if not isinstance(manual, dict) or not isinstance(manual.get("value"), (int, float)):
            # 未回传模型/人工试算结果 → 保持 capability_gap 中断继续等待。
            state["updated_at"] = _now()
            _get_graph().update_state(_thread_config(task_id), state)
            return state

        # 模型/人工按用户规则试算的结果：强制标注「需人工复核、非定稿」+ 保留逐步 breakdown 展示。
        prev = state["items"][index].get("settle") or {}
        target = prev.get("target") or decision.get("target") or "结果"
        state["items"][index]["settle"] = {
            "node": "settle",
            "status": "done",
            "target": target,
            "value": manual.get("value"),
            "breakdown": manual.get("breakdown") or [{"item": target, "amount": manual.get("value")}],
            "rule": manual.get("rule"),
            "source": manual.get("source") or "llm_estimate",
            "verdict": "需人工复核",
            "is_final": False,
        }
        _append_event(state, "settle", "human_rule_estimate", {"index": index, "target": target})

    state["status"] = "running"
    state["interrupt"] = None
    state["pending"] = None
    state["updated_at"] = _now()
    return _get_graph().invoke(state, config=_thread_config(task_id))


def get_workflow_state(task_id: str) -> dict[str, Any]:
    state = _graph_state(task_id)
    if not state:
        return {"status": "not_found", "task_id": task_id}
    return state


def run_workflow_node(node: CostNodeName | str, payload: dict[str, Any]) -> dict[str, Any]:
    return run_node(node, payload)
