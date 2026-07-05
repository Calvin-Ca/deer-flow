"""HITL 会话门面 —— 可中断组价图的 start / resume / state 三动作。

把 langgraph 图包成可被 HTTP 路由薄调的会话原语：
- ``start`` —— 起一个 task_id、跑到首个闸（interrupt）或 done，返回闸 payload + 累积 events；
- ``resume`` —— 注入用户决策 ``Command(resume=...)``、续到下个闸或 done；
- ``get_state`` —— 从 checkpointer 读持久化状态（已钉编码 / override / audit_log）。

图 + checkpointer 在模块加载时**建一次单例**（SqliteSaver 文件级持久化，进程重启状态仍在，原则 4）。
``thread_id == task_id``：langgraph 据此把同一任务的暂停-恢复串起来。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from typing import Any

import requests
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from common.config import HITL_CHECKPOINT_DB
from cost.graph import build_graph

logger = logging.getLogger("ce-services.cost.session")

# 单例：SqliteSaver 持久化连接（check_same_thread=False —— uvicorn 多线程下复用同一连接）+ 编译图。
_conn = sqlite3.connect(HITL_CHECKPOINT_DB, check_same_thread=False)
_checkpointer = SqliteSaver(_conn)
_graph = build_graph(_checkpointer)

# ── 会话级并发闸（M1 加固）：同一 task_id 的写路径（resume/rewind）串行化 ──
# SqliteSaver 内部有语句级锁（单条 checkpoint 读写安全），但**同一会话**并发 resume/rewind 在图
# 逻辑层会交错：两次 invoke 各自「读 checkpoint → 跑节点 → 写回」，后写覆盖先写、审计乱序。
# 按 task_id 上锁：同会话串行（第二个请求等第一个跑完），不同会话互不阻塞。
# start/stream_start 用全新 uuid 无竞争不上锁；get_state/re_rollup 只读不上锁（快照读自洽）。
# 锁表随会话数线性增长（每会话一把 Lock，字节级），POC 量级不做回收。
_locks_guard = threading.Lock()
_task_locks: dict[str, threading.Lock] = {}


def _task_lock(task_id: str) -> threading.Lock:
    """取（惰性建）task_id 专属锁；建锁本身经 guard 锁保护（dict 写入非原子安全区）。"""
    with _locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = _task_locks[task_id] = threading.Lock()
        return lock


def _config(task_id: str) -> dict[str, Any]:
    """构造 langgraph 调用 config（按 task_id 路由 checkpointer 线程）。"""
    return {"configurable": {"thread_id": task_id}}


# 可回退到的闸节点（gate 节点，会 interrupt 等人）；compute 节点（list_match/compose）不可作回退目标。
REWINDABLE_GATES = frozenset({
    "setup", "feature_gate", "list_gate", "quota_gate",
    "price_gate", "quantity_gate", "rates_gate", "params_gate", "rollup",
})


def _error(task_id: str, message: str) -> dict[str, Any]:
    """构造一个与 ``_format`` 同形的错误响应（status=error + error 文案），供 rewind 非法入参返回。"""
    return {
        "task_id": task_id, "status": "error", "interrupt": None, "error": message,
        "events": [], "items": [], "overrides": [], "audit_log": [],
        "rates": None, "params": None, "rollup": None,
    }


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


def _norm_feature(entry: str | dict[str, Any]) -> dict[str, Any] | None:
    """把 features 元素归一为 item dict：纯字符串 → ``{feature}``；dict → 保留 feature + 分组标签 + 预供 Q。

    参数：entry —— 字符串描述 或 ``{feature, single_work?, unit_work?, quantity?}``。
    返回：``{feature[, single_work, unit_work, quantity]}``；feature 为空 → None（调用方过滤，不建空构件）。
    quantity（M2 批量列清单）：抽取器从原文抽到的工程量随件预供 → quantity_gate 自动过；
      非正数/非数值丢弃（不编量），照常停闸人工录入。
    """
    if isinstance(entry, str):
        return {"feature": entry} if entry else None
    feat = entry.get("feature")
    if not feat:
        return None
    item: dict[str, Any] = {"feature": feat}
    for k in ("single_work", "unit_work"):
        if entry.get(k):
            item[k] = entry[k]
    qty = entry.get("quantity")
    if isinstance(qty, (int, float)) and qty > 0:
        item["quantity"] = float(qty)
    return item


def start(
    feature: str | None = None,
    spec: str | None = None,
    region: str = "深圳",
    period: str | None = None,
    price_source: str | None = None,
    rates: dict[str, Any] | None = None,
    quantity: float | None = None,
    features: list[str | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """起一个组价 HITL 会话，跑到首个闸或 done（支持单/多构件）。

    参数：feature —— 单构件描述；features —— 多构件列表（给定则按外层循环逐件办，优先于 feature；
      元素可为纯描述字符串，或 ``{feature, single_work?, unit_work?}`` 带两级汇总分组）；
      spec —— 国标版本（缺则 setup 闸采集）；region/period/price_source/rates —— setup 口径；
      quantity —— 可选工程量 Q（仅单构件时预供 items[0]；多构件各件经 quantity_gate 录入）。
    返回：会话响应（含 task_id；首个 interrupt 通常是编码确认闸，或 spec 缺失时的 setup 录入闸）。
    """
    task_id = uuid.uuid4().hex
    initial = _initial_state(task_id, feature, region, rates, quantity, features,
                             spec=spec, period=period, price_source=price_source)
    result = _graph.invoke(initial, config=_config(task_id))
    return _format(task_id, result)


def _initial_state(
    task_id: str,
    feature: str | None,
    region: str,
    rates: dict[str, Any] | None,
    quantity: float | None,
    features: list[str | dict[str, Any]] | None,
    *,
    spec: str | None = None,
    period: str | None = None,
    price_source: str | None = None,
) -> dict[str, Any]:
    """构造图初始状态（start 与 stream_start 共用）——多构件归一 + Q 预供 + setup 口径。

    参数同 ``start``。返回：图 ``invoke``/``stream`` 的 initial dict（仅含给定项，缺省交 setup 节点归一）。
    """
    items: list[dict[str, Any]] = [it for it in (_norm_feature(f) for f in (features or [])) if it]
    if not items and feature:
        items = [{"feature": feature}]
    if quantity is not None and len(items) == 1:  # Q 预供仅对单构件无歧义
        items[0]["quantity"] = quantity
    initial: dict[str, Any] = {
        "task_id": task_id,
        "feature": items[0]["feature"] if items else None,
        "region": region,
        "current_item": 0,
        "items": items,
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
    return initial


def _exc_detail(exc: Exception) -> str:
    """把流式跑图时的依赖/LLM 异常映射成可读 detail（SSE error 事件用，与 router `_map_session_exc` 同款文案）。"""
    if isinstance(exc, requests.HTTPError):
        return exc.response.text if exc.response is not None else str(exc)
    if isinstance(exc, requests.RequestException):
        return f"依赖服务不可达（知识服务 :8100 / LLM :8099）: {exc}"
    return f"LLM 选码输出非合法 JSON: {exc}"  # ValueError（含 json.JSONDecodeError）


def _run_stream(task_id: str, graph_input: Any) -> Any:
    """逐节点跑图并产出 SSE 消息（生成器）——事件即产即发，暂停/终态由跑完后读 state 判定。

    参数：task_id —— 会话标识；graph_input —— 初始状态（start）或 ``Command(resume=...)``（resume）。
    产出（dict，供 router 逐条 ``data: {json}``）：
      - ``{type:"event", event}`` —— 每个节点新增的一条 provenance 事件（stream_mode=updates 的节点增量）；
      - ``{type:"interrupt", gate}`` —— 跑到闸暂停（跑完后 state 有挂起 interrupt）；
      - ``{type:"done", status, rollup, items, overrides, audit_count}`` —— 无暂停跑到终态（done/blocked）；
      - ``{type:"error", detail}`` —— 节点内依赖/LLM 异常（如实透传，不静默）。
    版本鲁棒：不依赖 ``__interrupt__`` 特殊块，跑完统一读 ``get_state`` 判暂停/终态（跳过非节点块）。
    """
    config = _config(task_id)
    try:
        for chunk in _graph.stream(graph_input, config=config, stream_mode="updates"):
            if not isinstance(chunk, dict):
                continue
            for node, update in chunk.items():
                if node == "__interrupt__" or not isinstance(update, dict):
                    continue
                for ev in update.get("events") or []:
                    yield {"type": "event", "event": ev}
    except (requests.RequestException, ValueError) as exc:
        # 服务端必须留痕：此前只 yield 进 SSE，客户端断流后异常完全无痕（07-05 实测排障半小时
        # 才定位到 compose 404——异常路径不出声是本项目已两踩的坑，见 message-content-blocks-trap）。
        logger.warning("组价图流式执行异常 task=%s: %s", task_id, _exc_detail(exc))
        yield {"type": "error", "detail": _exc_detail(exc)}
        return

    snapshot = _graph.get_state(config)
    interrupt = _pending_interrupt(snapshot)
    if interrupt is not None:
        yield {"type": "interrupt", "gate": interrupt}
        return
    values = snapshot.values if snapshot else {}
    yield {
        "type": "done",
        "status": values.get("status", "done"),
        "rollup": values.get("rollup"),
        "items": values.get("items", []),
        "overrides": values.get("overrides", []),
        "audit_count": len(values.get("audit_log", [])),
    }


def stream_start(
    feature: str | None = None,
    spec: str | None = None,
    region: str = "深圳",
    period: str | None = None,
    price_source: str | None = None,
    rates: dict[str, Any] | None = None,
    quantity: float | None = None,
    features: list[str | dict[str, Any]] | None = None,
) -> Any:
    """起一个组价 HITL 会话并**逐节点流式**产出到首个闸或终态（参数同 ``start``）。

    先产 ``{type:"start", task_id}`` 供前端拿会话 id，再逐条产步骤事件 / 暂停闸 / 终态（见 ``_run_stream``）。
    """
    task_id = uuid.uuid4().hex
    initial = _initial_state(task_id, feature, region, rates, quantity, features,
                             spec=spec, period=period, price_source=price_source)
    yield {"type": "start", "task_id": task_id}
    yield from _run_stream(task_id, initial)


def stream_resume(task_id: str, decision: dict[str, Any]) -> Any:
    """以用户决策**逐节点流式**续跑会话到下个闸或终态（``resume`` 的 SSE 版）。

    参数：task_id；decision —— 闸决策（空 dict 归一哨兵，同 ``resume``）。产出见 ``_run_stream``。
    """
    with _task_lock(task_id):  # 流式期间持锁：同会话第二个 resume 等本次流完（客户端断连→生成器关闭→释锁）
        yield from _run_stream(task_id, Command(resume=decision or {"__resume__": True}))


def resume(task_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    """以用户决策续跑会话到下个闸或 done。

    参数：task_id —— 会话标识；decision —— 闸的用户输入（confirm: ``{action, value?}``；input: 字段值 dict）。
    返回：会话响应（下个 interrupt 或终态）。
    注：**空 decision（``{}``）替换为非空哨兵**——langgraph 把空 dict resume 当「未提供恢复值」会在同一闸
      重停死循环；全选填留空的合法「放弃」提交（如缺定额补录闸）会命中此坑，故在此归一化（哨兵不含业务
      字段，被下游 ``build_manual_basis`` 等按缺省/放弃处理）。
    """
    with _task_lock(task_id):  # 同会话并发 resume 串行化（后到请求等前一次跑完，防 checkpoint 交错）
        result = _graph.invoke(Command(resume=decision or {"__resume__": True}), config=_config(task_id))
    return _format(task_id, result)


def batch_resume(task_id: str, decisions: list[dict[str, Any]],
                 max_steps: int = 200) -> dict[str, Any]:
    """批量决策续跑（M2 评审表后端地基）：一次提交多闸决策，服务端循环 resume 到「决策池耗尽/
    未命中的闸/终态」为止——把「逐闸串行往返」压成一次调用。

    参数：
        task_id —— 会话标识；
        decisions —— 决策池，每条 ``{node, item?, decision}``：node=闸节点名（interrupt payload
          的 node 字段：list_coding / quota / quota_missing / price_item:N / quantity / feature /
          rates / params / rollup）；item=构件序号（可省=不限件，对项目级闸 rates/params/rollup 无意义）；
          decision=该闸的用户输入（与单闸 resume 同形）。
        max_steps —— 步数上限（防异常死循环；30 件×每件~5 闸远低于 200）。
    返回：``_format`` 响应 + ``batch {consumed, steps, stopped_reason}``：
      - stopped_reason=done/blocked —— 跑到终态；
      - no_match —— 停在决策池没有对应决策的闸（评审表语义：低置信件留人工，interrupt 照常返回）；
      - repeat_gate —— 同一闸消耗决策后仍重停（决策被闸拒绝，如半填重问），停下防空转；
      - max_steps —— 上限兜底。
    匹配规则：node 精确等值 且（决策未给 item 或 item==当前 current_item）；命中即消耗（一条决策
      只用一次——重停同闸不复用，交 repeat_gate 停下）。整个循环持会话锁（内部裸 invoke，不经
      ``resume()``——threading.Lock 不可重入）。
    """
    pool = [dict(d) for d in (decisions or [])]
    consumed: list[dict[str, Any]] = []
    steps = 0
    stopped = "no_match"
    with _task_lock(task_id):
        config = _config(task_id)
        last_key: tuple[Any, Any] | None = None
        while steps < max_steps:
            snapshot = _graph.get_state(config)
            gate = _pending_interrupt(snapshot)
            if gate is None:  # 无挂起闸：已是终态（done/blocked）
                stopped = (snapshot.values or {}).get("status", "done") if snapshot else "done"
                break
            node = gate.get("node") if isinstance(gate, dict) else None
            current_item = (snapshot.values or {}).get("current_item") if snapshot else None
            key = (node, current_item)
            if key == last_key:  # 消耗了决策仍重停同闸（被闸拒绝，如人材机半填重问）→ 停下防空转
                stopped = "repeat_gate"
                break
            idx = next((i for i, d in enumerate(pool)
                        if d.get("node") == node
                        and (d.get("item") is None or d.get("item") == current_item)), None)
            if idx is None:  # 池里没有这闸的决策：留给人工（评审表的「低置信标黄」件）
                stopped = "no_match"
                break
            entry = pool.pop(idx)
            consumed.append({"node": node, "item": current_item})
            last_key = key
            decision = entry.get("decision") or {}
            _graph.invoke(Command(resume=decision or {"__resume__": True}), config=config)
            steps += 1
        else:
            stopped = "max_steps"

        final = _graph.get_state(config)
    values = final.values if final else {}
    out = _format(task_id, {**values, "__interrupt__": None})
    out["interrupt"] = _pending_interrupt(final)
    if out["interrupt"] is not None:
        out["status"] = "awaiting_input"
    out["batch"] = {"consumed": consumed, "steps": steps, "stopped_reason": stopped,
                    "remaining_decisions": len(pool)}
    return out


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


def rewind(task_id: str, to_node: str) -> dict[str, Any]:
    """回退到指定闸重答（langgraph 时间旅行）——丢弃该闸之后的已钉值，从其前序 checkpoint 重跑到该闸暂停。

    参数：task_id —— 会话标识；to_node —— 目标闸节点名（须 ∈ ``REWINDABLE_GATES``）。
    返回：会话响应（重新停在该闸的 interrupt，供用户改答）；to_node 非法 / 历史无该闸前序检查点 →
      status=error + error 文案（不抛、不污染线程）。
    语义（呼应 §8.2「可回退」）：定位「该闸即将执行」的最近一次 checkpoint（``snapshot.next`` 含 to_node），
      从它重新 ``invoke(None)``——上游 compute（含 LLM 选码/取数）不重跑（checkpoint 已存、原则 3），
      该闸**之后**的锁值（编码/定额/价/Q/费率/参数）随回退作废，由用户重新确认。改答后照常走 ``resume``。
    """
    if to_node not in REWINDABLE_GATES:
        return _error(task_id, f"不可回退到 {to_node!r}（仅支持闸节点：{sorted(REWINDABLE_GATES)}）")
    config = _config(task_id)
    with _task_lock(task_id):  # 回退是写路径：查历史 + 重跑须与并发 resume 互斥（防回退点被同时推进作废）
        target = next((s for s in _graph.get_state_history(config) if to_node in (s.next or ())), None)
        if target is None:
            return _error(task_id, f"历史中无 {to_node!r} 的前序检查点，无法回退（该闸可能尚未到达）")
        result = _graph.invoke(None, config=target.config)
    return _format(task_id, result)


def re_rollup(
    task_id: str,
    params_override: dict[str, Any] | None = None,
    rates_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """what-if 重算已完成组价的总造价：读持久化累积态、套修正费率/项目费用、确定性重算（不改会话、不重跑 LLM）。

    用途（COST_STEP_DISPLAY_PLAN §8 决策5 / cost-agent 红线7）：用户「改费率/税率/项目费用后要新总价」时，
    重算走**服务端确定性图原语**（``graph.recompute_rollup``），而不是让弱模型在对话里凭回复文本数字反推。

    参数：task_id —— 已起过的会话；params_override —— measure_fee/other_fee/fee_levy/tax_rate 覆盖；
      rates_override —— management_fee_rate/profit_rate/risk_rate/fee_base 覆盖（变则逐件重算综合单价）。
    返回：``{task_id, status:"recomputed", rollup, rates, params}``；会话无构件数据 → status=error。
      **纯 what-if**：不修改 checkpoint、不推进图；要把新值写回需另走 rewind + resume。
    """
    from cost.graph import recompute_rollup
    snapshot = _graph.get_state(_config(task_id))
    values = snapshot.values if snapshot else {}
    if not values.get("items"):
        return _error(task_id, "会话无构件数据，无法重算总造价")
    out = recompute_rollup(values, params_override=params_override, rates_override=rates_override)
    return {"task_id": task_id, "status": "recomputed", **out}


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
