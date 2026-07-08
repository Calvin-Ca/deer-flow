"""LangGraph 适配器 —— 独立组价图路径用的两个 HITL 节点。

harness 内联路径走 ③HumanTaskUI 的 ask_clarification 通道（turn 边界式），不用这里；
本文件仅供 ``cost.graph_4step`` 那条独立编译图（自带 checkpointer + interrupt）复用。
"""
from __future__ import annotations

from typing import Any

from .models import HumanResponse, HumanTask, Scope, Step


def hitl_gate_node(state: dict[str, Any], step: Step | None = None,
                   scope: Scope | None = None) -> dict[str, Any]:
    """通用 HITL 闸节点：evaluate 命中则 interrupt 抛 HumanTask、等人回。

    与 compute 节点分离（取数/算价在前置 compute 节点做），保证本节点幂等、resume 重跑不漂移。
    """
    from .pipeline import policy_engine  # 懒加载，避免包初始化期循环
    task = policy_engine.evaluate(state, step=step, scope=scope)
    if task is None:
        return {"pending_human_task": None}
    from langgraph.types import interrupt  # 懒加载
    payload = task.model_dump()
    payload["interaction"] = task.interaction
    resp = interrupt(payload)
    return {"pending_human_task": task.model_dump(), "hitl_result": resp}


def apply_hitl_result_node(state: dict[str, Any]) -> dict[str, Any]:
    """接在 hitl_gate_node 后：把人的响应落回 state（独立节点，不受 interrupt 重跑影响）。"""
    from .pipeline import resume_handler  # 懒加载
    raw = state.get("pending_human_task")
    resp = state.get("hitl_result")
    if not raw or resp is None:
        return {}
    task = HumanTask.model_validate(raw)
    response = HumanResponse.model_validate(resp)
    resume_handler.apply(state, task, response)
    return {k: state.get(k) for k in
            ("items", "rates", "params", "resolved_tasks", "needs_rematch",
             "final_approved", "audit_log", "escalations")
            if k in state}
