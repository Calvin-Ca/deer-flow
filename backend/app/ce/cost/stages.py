"""组价流水线的阶段契约层——每个阶段的**业务**住这里（2026-07-12 workflow 打薄定案）。

分层约定：``workflow.py`` 只留实例化与装配（LangGraph 图机制/游标/暂停/事件/任务生命周期），
本模块是它与能力件（nodes / bill_match_engine / quota_engine）之间的接线板。每个 ``Stage``
声明两件业务：

- ``run(state, item)``    —— 正向执行：从 state/item 装配该步 payload、调能力件、返回节点结果；
- ``resume(state, item, decision)`` —— 闸上应用人工决策：缺回传 → ``waiting``（重发同一中断），
  有决策 → ``applied``（产出落位结果 + 事件标签），业务副作用（如选码纠正采集）也在这里。

**要把某一步从 tool 升级成 agent/LLM 执行，只改本模块该步的 run（或 step_strategies 路由），
workflow 一行不动**——这就是打薄的目的：装配与业务解耦，各自独立演进。

resume 统一出参协议（workflow 通用分发按此解释）：
    {"outcome": "waiting", "interrupt": dict | None}   # None = 保留现有中断不覆盖
    {"outcome": "applied", "result": dict, "event": str, "event_payload": dict}
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .nodes import price_review_node, run_business_step, run_node, select_bill_node, select_quota_node
from .quota_engine import extract_quota_schemes


# ---- 共用业务小件 -----------------------------------------------------------------
def strategy_for(state: dict[str, Any], step: str) -> str:
    """该业务步的执行策略（tool/agent/llm，state.step_strategies 路由；缺省 tool）。"""
    strategies = state.get("step_strategies")
    if not isinstance(strategies, dict):
        return "tool"
    strategy = strategies.get(step)
    return strategy if strategy in {"agent", "tool", "llm"} else "tool"


def selected_quotas(item: dict[str, Any]) -> list[dict[str, Any]]:
    """取当前采用的定额子目（选定方案的 quotas，否则回退 price_compose 全量）——询价/结算的扫描范围。"""
    selection = item.get("quota_selection") or {}
    scheme = selection.get("selected_scheme")
    if isinstance(scheme, dict) and isinstance(scheme.get("quotas"), list) and scheme["quotas"]:
        return scheme["quotas"]
    compose = item.get("price_compose") or {}
    result = compose.get("result") if isinstance(compose, dict) else None
    if isinstance(result, dict) and isinstance(result.get("quotas"), list):
        return result["quotas"]
    return []


def components_from_quotas(quotas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """定额子目 → compute 引擎的 components 入参（复用 nodes 侧实现）。"""
    from .nodes import _components_from_quotas

    return _components_from_quotas(quotas)


def build_result(items: list[Any]) -> dict[str, Any]:
    """整单结果投影（对外交付形态：逐条 描述/选码/价态/单价）。"""
    priced_items: list[dict[str, Any]] = []
    for item in items or []:
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


def _base_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec": state.get("spec"),
        "region": state.get("region"),
        "period": state.get("period"),
        "top_k": state.get("top_k", 10),
    }


# ---- 正向 run（每阶段的 payload 装配 + 能力件调用）--------------------------------
def run_bill_match(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return run_business_step("bill_match", {**_base_payload(state), **item}, strategy=strategy_for(state, "bill_match"))


def run_select_bill(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    # description 供选定后的特征缺口检查（少特征提醒）；缺则引擎静默跳过该检查。
    return select_bill_node({**_base_payload(state), "description": item.get("description"), "candidates": item.get("bill_match", {}).get("candidates", [])})


def run_quota_compose(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    selected_code = item.get("selection", {}).get("selected_code")
    return run_business_step("quota_compose", {**_base_payload(state), "code": selected_code}, strategy=strategy_for(state, "quota_compose"))


def run_select_quota(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    # ≤1 套方案由节点直接 done（auto_single_scheme），多方案落 review 闸并附引擎 LLM 预排建议。
    return select_quota_node({**_base_payload(state), "description": item.get("description"), "schemes": extract_quota_schemes(item.get("price_compose", {}))})


def run_price_review(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    # 选定组价的工料机询价：信息价缺失（no_source）的人材机 → 人工补价（仅有缺价料才停）。
    return price_review_node({"quotas": selected_quotas(item), "region": state.get("region"), "period": state.get("period")})


def run_settle(state: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    # 结算：选定方案+已询价的工料机 → compute 引擎算到 settle_target（默认综合单价）。
    # 目标层无确定性公式时 compute 返回 capability_gap，停下交人按规则试算后 resume。
    return run_node(
        "compute",
        {
            "target": state.get("settle_target") or "unit_rate",
            "components": components_from_quotas(selected_quotas(item)),
            "management_rate": state.get("management_rate"),
            "profit_rate": state.get("profit_rate"),
            "risk_rate": state.get("risk_rate"),
            "quantity": item.get("quantity"),
        },
    )


# ---- resume（闸上应用人工决策；出参协议见模块 docstring）---------------------------
def _waiting(interrupt: dict[str, Any] | None) -> dict[str, Any]:
    return {"outcome": "waiting", "interrupt": interrupt}


def _applied(result: dict[str, Any], event: str, event_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"outcome": "applied", "result": result, "event": event, "event_payload": event_payload or {}}


def resume_select_bill(state: dict[str, Any], item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    selected_code = decision.get("selected_code") or decision.get("code") or decision.get("manual_input")
    candidates = item.get("bill_match", {}).get("candidates", [])
    if not isinstance(selected_code, str) or not selected_code.strip():
        # 未回传编码 → 用原节点重新生成同一 review 中断，继续等待。
        return _waiting(select_bill_node({"candidates": candidates}).get("interrupt"))

    result = select_bill_node({"selected_code": selected_code.strip(), "candidates": candidates, "reason": decision.get("reason")})
    # 主动学习闭环·采集端：人工在闸上给的正确码入库，供未来相似构件检索作 few-shot。
    # 采集失败绝不拖垮组价——整段吞异常。
    try:
        from .exemplars import record_bill_correction

        record_bill_correction(
            item.get("description"),
            selected_code.strip(),
            candidates=candidates,
            region=state.get("region"),
            spec=state.get("spec"),
        )
    except Exception:  # noqa: BLE001
        pass
    return _applied(result, "human_selected", {"selected_code": selected_code.strip()})


def resume_select_quota(state: dict[str, Any], item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    schemes = extract_quota_schemes(item.get("price_compose", {}))
    selected_scheme = decision.get("selected_scheme") or decision.get("scheme_id")
    manual_input = decision.get("manual_input")
    has_selection = (isinstance(selected_scheme, str) and selected_scheme.strip()) or (isinstance(manual_input, str) and manual_input.strip())
    if not has_selection:
        return _waiting(select_quota_node({"schemes": schemes}).get("interrupt"))
    result = select_quota_node({"schemes": schemes, "selected_scheme": selected_scheme, "manual_input": manual_input, "reason": decision.get("reason")})
    return _applied(result, "human_selected", {"selected_scheme": selected_scheme})


def resume_price_review(state: dict[str, Any], item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    base = {"quotas": selected_quotas(item), "region": state.get("region"), "period": state.get("period")}
    if not decision.get("prices"):
        return _waiting(price_review_node(base).get("interrupt"))
    return _applied(price_review_node({**base, "prices": decision["prices"]}), "human_priced")


def resume_settle(state: dict[str, Any], item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    manual = decision.get("manual_result")
    if not isinstance(manual, dict) or not isinstance(manual.get("value"), (int, float)):
        # 未回传试算结果 → 保持 capability_gap 中断继续等待（不重发）。
        return _waiting(None)
    # 人/模型按用户规则试算的结果：强制标注「需人工复核、非定稿」+ 保留逐步 breakdown 展示。
    prev = item.get("settle") or {}
    target = prev.get("target") or decision.get("target") or "结果"
    result = {
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
    return _applied(result, "human_rule_estimate", {"target": target})


# ---- 阶段表（单条清单的组价 DAG；workflow 的通用解释器按此驱动）--------------------
@dataclass(frozen=True)
class Stage:
    """一个组价阶段的声明式契约。

    name        阶段 id（也是事件名）；
    output_key  该阶段写入 item 的键——游标据此推导当前进度（第一个未写入的阶段）；
    run         正向业务（payload 装配 + 能力件调用），见上；
    gate_node   非空 ⇒ 此阶段可触发 HITL 暂停（对应 pending.node，strict 状态策略：
                awaiting_input→暂停 / done→继续 / 其它→阻断）；为空 ⇒ 取数类阶段 lenient
                （仅 blocked→阻断）；
    resume      闸上人工决策的业务应用（仅 gate 阶段有）；
    pause_with_gate_type  暂停时 pending 里是否附带 gate_type（仅 settle 的 capability_gap 需要）。
    """

    name: str
    output_key: str
    run: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    gate_node: str | None = None
    resume: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    pause_with_gate_type: bool = False


STAGES: list[Stage] = [
    Stage("bill_match", "bill_match", run_bill_match),
    Stage("select_bill", "selection", run_select_bill, gate_node="select_bill", resume=resume_select_bill),
    Stage("quota_compose", "price_compose", run_quota_compose),
    Stage("select_quota", "quota_selection", run_select_quota, gate_node="select_quota", resume=resume_select_quota),
    Stage("price_review", "price_review", run_price_review, gate_node="price_review", resume=resume_price_review),
    Stage("settle", "settle", run_settle, gate_node="settle", resume=resume_settle, pause_with_gate_type=True),
]

__all__ = [
    "STAGES", "Stage", "build_result", "components_from_quotas", "selected_quotas", "strategy_for",
    "resume_price_review", "resume_select_bill", "resume_select_quota", "resume_settle",
    "run_bill_match", "run_price_review", "run_quota_compose", "run_select_bill", "run_select_quota", "run_settle",
]
