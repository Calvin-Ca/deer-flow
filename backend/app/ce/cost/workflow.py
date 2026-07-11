"""Application-level CE cost workflow backed by a LangGraph checkpointer."""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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


# ---- 显式 stage 状态机（STAGES 表 = 单条清单的组价 DAG）---------------------------
@dataclass(frozen=True)
class _Ctx:
    """一次 stage 执行的上下文聚合（免到处透传 state/item/index/base_payload）。"""

    state: dict[str, Any]
    item: dict[str, Any]
    index: int
    base_payload: dict[str, Any]


@dataclass(frozen=True)
class _Stage:
    """一个组价阶段的声明式描述。

    name        阶段 id（也是事件名）；
    output_key  该阶段写入 item 的键——游标 `_current_stage` 据此推导当前进度（第一个未写入的阶段）；
    run         产出节点结果的薄封装（内部调 run_business_step / *_node，逻辑与旧阶梯一致）；
    gate_node   非空 ⇒ 此阶段可触发 HITL 暂停，对应 pending.node，用 strict 状态策略
                （awaiting_input→暂停 / done→继续 / 其它→阻断）；为空 ⇒ 取数类阶段用 lenient 策略
                （仅 blocked→阻断，need_review 等非 done 也继续，从不因人工输入停下）；
    post        done 后的可选副作用（如单方案自动降级，直接写 quota_selection 让游标跳过 select_quota）；
    pause_with_gate_type  暂停时是否在 pending 里附带 gate_type（仅 settle 的 capability_gap 需要）。
    """

    name: str
    output_key: str
    run: Callable[[_Ctx], dict[str, Any]]
    gate_node: str | None = None
    post: Callable[[_Ctx, dict[str, Any]], None] | None = None
    pause_with_gate_type: bool = False


def _run_bill_match(ctx: _Ctx) -> dict[str, Any]:
    return run_business_step("bill_match", {**ctx.base_payload, **ctx.item}, strategy=_strategy_for(ctx.state, "bill_match"))


def _run_select_bill(ctx: _Ctx) -> dict[str, Any]:
    # description 供选定后的特征缺口检查（少特征提醒）；缺则引擎静默跳过该检查。
    return select_bill_node({**ctx.base_payload, "description": ctx.item.get("description"), "candidates": ctx.item.get("bill_match", {}).get("candidates", [])})


def _run_quota_compose(ctx: _Ctx) -> dict[str, Any]:
    selected_code = ctx.item.get("selection", {}).get("selected_code")
    return run_business_step("quota_compose", {**ctx.base_payload, "code": selected_code}, strategy=_strategy_for(ctx.state, "quota_compose"))


def _auto_single_scheme(ctx: _Ctx, result: dict[str, Any]) -> None:
    """quota_compose 的 post：单方案（服务端未返回多套可替代方案）直接采用，游标据此跳过 select_quota；多方案留给复核。"""
    schemes = extract_quota_schemes(result)
    if len(schemes) <= 1:
        ctx.item["quota_selection"] = {
            "node": "select_quota",
            "status": "done",
            "selection_source": "auto_single_scheme",
            "selected_scheme": schemes[0] if schemes else None,
        }


def _run_select_quota(ctx: _Ctx) -> dict[str, Any]:
    return select_quota_node({**ctx.base_payload, "schemes": extract_quota_schemes(ctx.item["price_compose"])})


def _run_price_review(ctx: _Ctx) -> dict[str, Any]:
    # 选定组价的工料机询价：信息价缺失（no_source）的人材机 → 人工补价（仅有缺价料才停）。
    return price_review_node({"quotas": _selected_quotas(ctx.item), "region": ctx.state["region"], "period": ctx.state.get("period")})


def _run_settle(ctx: _Ctx) -> dict[str, Any]:
    # 结算：选定方案+已询价的工料机 → compute 引擎算到 settle_target（默认综合单价）。
    # 目标层无确定性公式时 compute 返回 capability_gap，停下交模型按规则试算后 resume。
    target = ctx.state.get("settle_target") or "unit_rate"
    return run_node(
        "compute",
        {
            "target": target,
            "components": _components_from_quotas(_selected_quotas(ctx.item)),
            "management_rate": ctx.state.get("management_rate"),
            "profit_rate": ctx.state.get("profit_rate"),
            "risk_rate": ctx.state.get("risk_rate"),
            "quantity": ctx.item.get("quantity"),
        },
    )


# 单条清单的组价阶段序列（有序）= 显式 DAG；游标 = 第一个 output_key 未写入的阶段。
STAGES: list[_Stage] = [
    _Stage("bill_match", "bill_match", _run_bill_match),
    _Stage("select_bill", "selection", _run_select_bill, gate_node="select_bill"),
    _Stage("quota_compose", "price_compose", _run_quota_compose, post=_auto_single_scheme),
    _Stage("select_quota", "quota_selection", _run_select_quota, gate_node="select_quota"),
    _Stage("price_review", "price_review", _run_price_review, gate_node="price_review"),
    _Stage("settle", "settle", _run_settle, gate_node="settle", pause_with_gate_type=True),
]


def _current_stage(item: dict[str, Any]) -> _Stage | None:
    """游标：STAGES 中第一个 output_key 尚未写入 item 的阶段；全部写入 → None（该 item 已完成，可推进下一条）。"""
    for stage in STAGES:
        if stage.output_key not in item:
            return stage
    return None


def _pause(state: dict[str, Any], stage: _Stage, result: dict[str, Any], index: int) -> dict[str, Any]:
    """把一个 awaiting_input 的 stage 结果落成统一的 HITL 暂停态（pending/interrupt/result）。"""
    gate = result.get("interrupt") or {}
    pending: dict[str, Any] = {"node": stage.gate_node, "index": index}
    if stage.pause_with_gate_type:
        pending["gate_type"] = gate.get("gate_type")
    state["status"] = "awaiting_input"
    state["pending"] = pending
    state["interrupt"] = gate
    state["result"] = _build_result(state)
    return state


def _advance_item(state: dict[str, Any], current_index: int) -> dict[str, Any]:
    """一条清单全阶段完成 → 游标推进到下一条。"""
    state["current_index"] = current_index + 1
    state["updated_at"] = _now()
    _append_event(state, "workflow", "advance_item", {"from_index": current_index, "to_index": current_index + 1})
    return state


def _workflow_step(input_state: CostWorkflowState) -> CostWorkflowState:
    """Advance the graph by exactly one workflow stage.

    Control flow is driven by the declarative ``STAGES`` table (an explicit DAG)
    plus a single interpreter, rather than an implicit ladder of key-presence
    checks. LangGraph checkpoints after every graph step, and this function still
    performs at most one stage transition per call, so resume/replay stays
    inspectable without giving the lead agent procedural control over the workflow.
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

    stage = _current_stage(item)
    if stage is None:
        return _advance_item(state, current_index)

    base_payload = {
        "spec": state["spec"],
        "region": state["region"],
        "period": state.get("period"),
        "top_k": state.get("top_k", 10),
    }
    ctx = _Ctx(state=state, item=item, index=current_index, base_payload=base_payload)
    result = stage.run(ctx)
    item[stage.output_key] = result
    state["items"] = items
    state["updated_at"] = _now()
    _append_event(state, stage.name, result.get("status", "unknown"), {"index": current_index})

    node_status = result.get("status")
    if stage.gate_node is not None:
        # 可触发 HITL 的阶段：awaiting_input→暂停 / done→继续 / 其它→阻断
        if node_status == "awaiting_input":
            return _pause(state, stage, result, current_index)
        if node_status != "done":
            return _block(state, result)
    elif node_status == "blocked":
        # 取数类阶段：只在服务不可达时阻断，need_review 等非 done 状态也继续
        return _block(state, result)

    if stage.post is not None:
        stage.post(ctx, result)
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


# ---- resume 侧 HITL gate 处理（表驱动，与正向 STAGES.gate_node 对齐）------------------
# 每个 handler 三种出路：① 返回一个 dict（无效 index 阻断 / 缺回传继续等待）→ 直接作为 resume 结果返回；
# ② 返回 None（已应用人工决策）→ 落到共用尾巴「标 running + 重新 invoke 图」。
def _resume_invalid_index(state: dict[str, Any], node: str, task_id: str) -> dict[str, Any]:
    blocked = _block(state, {"node": node, "status": "blocked", "error": "invalid pending index"})
    return _get_graph().invoke(blocked, config=_thread_config(task_id))


def _resume_still_waiting(state: dict[str, Any], interrupt: dict[str, Any] | None, task_id: str) -> dict[str, Any]:
    state["interrupt"] = interrupt
    state["updated_at"] = _now()
    _get_graph().update_state(_thread_config(task_id), state)
    return state


def _resume_select_bill(state: dict[str, Any], index: Any, decision: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    items = state.get("items", [])
    if not isinstance(index, int) or index >= len(items):
        return _resume_invalid_index(state, "select_bill", task_id)

    selected_code = decision.get("selected_code") or decision.get("code") or decision.get("manual_input")
    if not isinstance(selected_code, str) or not selected_code.strip():
        # 未回传编码 → 用原节点重新生成同一 review 中断，继续等待。
        regen = select_bill_node({"candidates": items[index].get("bill_match", {}).get("candidates", [])})
        return _resume_still_waiting(state, regen.get("interrupt"), task_id)

    _candidates = items[index].get("bill_match", {}).get("candidates", [])
    items[index]["selection"] = select_bill_node(
        {
            "selected_code": selected_code.strip(),
            "candidates": _candidates,
            "reason": decision.get("reason"),
        }
    )
    _append_event(state, "select_bill", "human_selected", {"index": index, "selected_code": selected_code.strip()})
    # 主动学习闭环·采集端（⑥）：人工在闸上给的正确码入库，供未来相似构件检索作 few-shot。
    # 采集失败绝不拖垮组价——整段吞异常。
    try:
        from .exemplars import record_bill_correction
        record_bill_correction(
            items[index].get("description"),
            selected_code.strip(),
            candidates=_candidates,
            region=state.get("region"),
            spec=state.get("spec"),
        )
    except Exception:  # noqa: BLE001
        pass
    return None


def _resume_select_quota(state: dict[str, Any], index: Any, decision: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    items = state.get("items", [])
    if not isinstance(index, int) or index >= len(items):
        return _resume_invalid_index(state, "select_quota", task_id)

    schemes = extract_quota_schemes(items[index].get("price_compose", {}))
    selected_scheme = decision.get("selected_scheme") or decision.get("scheme_id")
    manual_input = decision.get("manual_input")
    has_selection = (isinstance(selected_scheme, str) and selected_scheme.strip()) or (isinstance(manual_input, str) and manual_input.strip())
    if not has_selection:
        # 未回传方案 → 用原节点重新生成同一 review 中断，继续等待。
        regen = select_quota_node({"schemes": schemes})
        return _resume_still_waiting(state, regen.get("interrupt"), task_id)

    items[index]["quota_selection"] = select_quota_node(
        {
            "schemes": schemes,
            "selected_scheme": selected_scheme,
            "manual_input": manual_input,
            "reason": decision.get("reason"),
        }
    )
    _append_event(state, "select_quota", "human_selected", {"index": index, "selected_scheme": selected_scheme})
    return None


def _resume_price_review(state: dict[str, Any], index: Any, decision: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    items = state.get("items", [])
    if not isinstance(index, int) or index >= len(items):
        return _resume_invalid_index(state, "price_review", task_id)

    item = items[index]
    base = {"quotas": _selected_quotas(item), "region": state["region"], "period": state.get("period")}
    if not decision.get("prices"):
        # 未回传询价结果 → 用原节点重新生成同一询价中断，继续等待。
        regen = price_review_node(base)
        return _resume_still_waiting(state, regen.get("interrupt"), task_id)

    items[index]["price_review"] = price_review_node({**base, "prices": decision["prices"]})
    _append_event(state, "price_review", "human_priced", {"index": index})
    return None


def _resume_settle(state: dict[str, Any], index: Any, decision: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    items = state.get("items", [])
    if not isinstance(index, int) or index >= len(items):
        return _resume_invalid_index(state, "settle", task_id)

    manual = decision.get("manual_result")
    if not isinstance(manual, dict) or not isinstance(manual.get("value"), (int, float)):
        # 未回传模型/人工试算结果 → 保持 capability_gap 中断继续等待。
        state["updated_at"] = _now()
        _get_graph().update_state(_thread_config(task_id), state)
        return state

    # 模型/人工按用户规则试算的结果：强制标注「需人工复核、非定稿」+ 保留逐步 breakdown 展示。
    prev = items[index].get("settle") or {}
    target = prev.get("target") or decision.get("target") or "结果"
    items[index]["settle"] = {
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
    return None


_RESUME_HANDLERS: dict[str, Callable[[dict[str, Any], Any, dict[str, Any], str], dict[str, Any] | None]] = {
    "select_bill": _resume_select_bill,
    "select_quota": _resume_select_quota,
    "price_review": _resume_price_review,
    "settle": _resume_settle,
}


def resume_workflow(task_id: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resume a paused workflow by updating the checked-point graph state.

    HITL gate handling is table-driven (``_RESUME_HANDLERS``) and mirrors the forward
    ``STAGES`` table: each handler validates the pending index, re-emits the same
    interrupt when the human input is missing, or applies the decision and falls
    through to the shared "mark running + re-invoke graph" tail.
    """
    state = _graph_state(task_id)
    if not state:
        return {"status": "not_found", "task_id": task_id}
    if state.get("status") != "awaiting_input":
        return state

    pending = state.get("pending") or {}
    decision = decision or {}
    handler = _RESUME_HANDLERS.get(pending.get("node"))
    if handler is not None:
        outcome = handler(state, pending.get("index"), decision, task_id)
        if outcome is not None:
            return outcome

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
