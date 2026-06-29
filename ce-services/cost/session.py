"""HITL 会话门面 —— 可中断组价图的 start / resume / state 三动作。

把 langgraph 图包成可被 HTTP 路由薄调的会话原语：
- ``start`` —— 起一个 task_id、跑到首个闸（interrupt）或 done，返回闸 payload + 累积 events；
- ``resume`` —— 注入用户决策 ``Command(resume=...)``、续到下个闸或 done；
- ``get_state`` —— 从 checkpointer 读持久化状态（已钉编码 / override / audit_log）。

图 + checkpointer 在模块加载时**建一次单例**（SqliteSaver 文件级持久化，进程重启状态仍在，原则 4）。
``thread_id == task_id``：langgraph 据此把同一任务的暂停-恢复串起来。
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from common.config import HITL_CHECKPOINT_DB
from cost.graph import build_graph

# 单例：SqliteSaver 持久化连接（check_same_thread=False —— uvicorn 多线程下复用同一连接）+ 编译图。
_conn = sqlite3.connect(HITL_CHECKPOINT_DB, check_same_thread=False)
_checkpointer = SqliteSaver(_conn)
_graph = build_graph(_checkpointer)


def _config(task_id: str) -> dict[str, Any]:
    """构造 langgraph 调用 config（按 task_id 路由 checkpointer 线程）。"""
    return {"configurable": {"thread_id": task_id}}


def _format(task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """把 ``invoke`` 返回值整形成会话响应。

    参数：task_id；result —— graph.invoke 返回的状态快照（暂停时含 ``__interrupt__``）。
    返回：``{task_id, status, interrupt, events, items, overrides, audit_log, rates, params, rollup}``：
      暂停 → status=awaiting_input、interrupt=闸 payload；否则取 state.status（done/blocked）、interrupt=None。
      rollup 在末尾 review/done 后非空（总造价明细）；rates/params 为已确认的费率与项目级费用。
    """
    interrupts = result.get("__interrupt__")
    if interrupts:
        payload = interrupts[0].value
        status = "awaiting_input"
    else:
        payload = None
        status = result.get("status", "done")
    return {
        "task_id": task_id,
        "status": status,
        "interrupt": payload,
        "events": result.get("events", []),
        "items": result.get("items", []),
        "overrides": result.get("overrides", []),
        "audit_log": result.get("audit_log", []),
        "rates": result.get("rates"),
        "params": result.get("params"),
        "rollup": result.get("rollup"),
    }


def start(
    feature: str,
    spec: str | None = None,
    region: str = "深圳",
    period: str | None = None,
    price_source: str | None = None,
    rates: dict[str, Any] | None = None,
    quantity: float | None = None,
) -> dict[str, Any]:
    """起一个组价 HITL 会话，跑到首个闸或 done。

    参数：feature —— 构件/做法描述；spec —— 国标版本（缺则 setup 闸采集）；region/period/price_source/rates —— setup 口径；
      quantity —— 可选工程量 Q（给定则 quantity_gate 自动过，缺则停闸录入）。
    返回：会话响应（含 task_id；首个 interrupt 通常是编码确认闸，或 spec 缺失时的 setup 录入闸）。
    """
    task_id = uuid.uuid4().hex
    item: dict[str, Any] = {"feature": feature}
    if quantity is not None:
        item["quantity"] = quantity
    initial: dict[str, Any] = {
        "task_id": task_id,
        "feature": feature,
        "region": region,
        "items": [item],
        "status": "running",
    }
    if spec:
        initial["spec_version"] = spec
    if period:
        initial["period"] = period
    if price_source:
        initial["price_source"] = price_source
    if rates:
        initial["rates"] = rates

    result = _graph.invoke(initial, config=_config(task_id))
    return _format(task_id, result)


def resume(task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """以用户决策续跑会话到下个闸或 done。

    参数：task_id —— 会话标识；decision —— 闸的用户输入（confirm: ``{action, value?}``；input: 字段值 dict）。
    返回：会话响应（下个 interrupt 或终态）。
    """
    result = _graph.invoke(Command(resume=decision), config=_config(task_id))
    return _format(task_id, result)


def _pending_interrupt(snapshot: Any) -> Any:
    """从图快照里提取**当前挂起的 interrupt 闸 payload**（供按 task_id 恢复渲染当前闸）。

    参数：snapshot —— ``graph.get_state`` 的 StateSnapshot。
    返回：第一个挂起 task 的首个 interrupt 的 value（即闸 payload）；无挂起 → None。
    langgraph 把暂停点挂在 ``snapshot.tasks[*].interrupts``，``invoke`` 的 ``__interrupt__`` 不落 state，
    故重新读 state（如前端内嵌组件按 task_id 拉当前闸 / 进程重启后恢复）须从这里取。
    """
    if not snapshot:
        return None
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts = getattr(task, "interrupts", ()) or ()
        if interrupts:
            return interrupts[0].value
    return None


def get_state(task_id: str) -> dict[str, Any]:
    """读会话当前持久化状态（不推进图）。

    参数：task_id。返回：``{task_id, status, interrupt, next, values}``——values 为完整 §5.4 状态文档
      （含已钉 code/quota、overrides、audit_log）；next 为待跑节点（空=已完成）；
      interrupt 为当前挂起的闸 payload（非空即 ``status=awaiting_input``，供按 task_id 恢复渲染当前闸）。
    """
    snapshot = _graph.get_state(_config(task_id))
    values = snapshot.values if snapshot else {}
    interrupt = _pending_interrupt(snapshot)
    status = "awaiting_input" if interrupt is not None else values.get("status", "unknown")
    return {
        "task_id": task_id,
        "status": status,
        "interrupt": interrupt,
        "next": list(snapshot.next) if snapshot else [],
        "values": values,
    }
