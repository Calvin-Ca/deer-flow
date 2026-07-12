"""组价工作流——**纯装配层**（2026-07-12 打薄定案：业务全部下沉 ``stages.py``）。

本模块只做实例化与装配，不含任何组价业务：
- LangGraph 图的构建/缓存与 checkpointer 绑定（任务状态跨进程持久化）；
- 游标解释器：按 ``stages.STAGES`` 声明表逐阶段推进（第一个 output_key 未写入的阶段），
  每图步恰好推进一个阶段（checkpoint 可回放）；
- HITL 暂停/阻断/事件/结果投影的统一落位；
- resume 的通用分发：按 pending.node 找到 Stage，索引校验后把决策交给 ``stage.resume``
  （业务），按其统一出参协议（waiting/applied）收尾。

要改某一步的业务（payload 装配、能力件选择、tool→agent 升级），去 ``stages.py``；
本文件应当只在「流程机制」变化时才被触碰。
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .nodes import run_node
from .stages import STAGES, Stage, build_result
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


def _block(state: dict[str, Any], node_result: dict[str, Any]) -> dict[str, Any]:
    state["status"] = "blocked"
    state["interrupt"] = None
    state["pending"] = None
    state["blocked_reason"] = node_result
    state["result"] = build_result(state.get("items", []))
    _append_event(state, str(node_result.get("node") or "workflow"), "blocked", node_result)
    return state


def _current_stage(item: dict[str, Any]) -> Stage | None:
    """游标：STAGES 中第一个 output_key 尚未写入 item 的阶段；全部写入 → None（该 item 已完成）。"""
    for stage in STAGES:
        if stage.output_key not in item:
            return stage
    return None


def _pause(state: dict[str, Any], stage: Stage, result: dict[str, Any], index: int) -> dict[str, Any]:
    """把一个 awaiting_input 的 stage 结果落成统一的 HITL 暂停态（pending/interrupt/result）。"""
    gate = result.get("interrupt") or {}
    pending: dict[str, Any] = {"node": stage.gate_node, "index": index}
    if stage.pause_with_gate_type:
        pending["gate_type"] = gate.get("gate_type")
    state["status"] = "awaiting_input"
    state["pending"] = pending
    state["interrupt"] = gate
    state["result"] = build_result(state.get("items", []))
    return state


def _advance_item(state: dict[str, Any], current_index: int) -> dict[str, Any]:
    """一条清单全阶段完成 → 游标推进到下一条。"""
    state["current_index"] = current_index + 1
    state["updated_at"] = _now()
    _append_event(state, "workflow", "advance_item", {"from_index": current_index, "to_index": current_index + 1})
    return state


def _workflow_step(input_state: CostWorkflowState) -> CostWorkflowState:
    """Advance the graph by exactly one workflow stage.

    Control flow is driven by the declarative ``stages.STAGES`` table plus this
    single interpreter. LangGraph checkpoints after every graph step, and this
    function performs at most one stage transition per call, so resume/replay
    stays inspectable without giving the lead agent procedural control.
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
        state["result"] = build_result(items)
        state["updated_at"] = _now()
        _append_event(state, "workflow", "done")
        return state

    item = items[current_index]
    if not isinstance(item, dict):
        return _block(state, {"node": "workflow", "status": "blocked", "error": f"item {current_index} is not an object"})

    stage = _current_stage(item)
    if stage is None:
        return _advance_item(state, current_index)

    result = stage.run(state, item)
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


# resume 分发表：gate 阶段名 → Stage（与正向 STAGES 同源，天然对齐不漂移）。
_RESUME_HANDLERS: dict[str, Stage] = {stage.gate_node: stage for stage in STAGES if stage.gate_node is not None}


def _resume_invalid_index(state: dict[str, Any], node: str, task_id: str) -> dict[str, Any]:
    blocked = _block(state, {"node": node, "status": "blocked", "error": "invalid pending index"})
    return _get_graph().invoke(blocked, config=_thread_config(task_id))


def resume_workflow(task_id: str, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resume a paused workflow by updating the checkpointed graph state.

    通用分发：按 pending.node 找到 Stage（与正向 STAGES 同一张表），索引校验后把决策交给
    ``stage.resume``（业务在 stages.py），按统一出参协议收尾——``waiting`` → 更新/保留中断
    继续等待；``applied`` → 结果落位 output_key + 记事件 + 标 running 重新 invoke 图。
    """
    state = _graph_state(task_id)
    if not state:
        return {"status": "not_found", "task_id": task_id}
    if state.get("status") != "awaiting_input":
        return state

    pending = state.get("pending") or {}
    decision = decision or {}
    stage = _RESUME_HANDLERS.get(pending.get("node"))
    if stage is not None and stage.resume is not None:
        items = state.get("items", [])
        index = pending.get("index")
        if not isinstance(index, int) or index >= len(items):
            return _resume_invalid_index(state, stage.gate_node or stage.name, task_id)

        outcome = stage.resume(state, items[index], decision)
        if outcome.get("outcome") == "waiting":
            if outcome.get("interrupt") is not None:
                state["interrupt"] = outcome["interrupt"]
            state["updated_at"] = _now()
            _get_graph().update_state(_thread_config(task_id), state)
            return state
        items[index][stage.output_key] = outcome["result"]
        _append_event(state, stage.name, outcome.get("event", "human_input"), {"index": index, **outcome.get("event_payload", {})})

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
