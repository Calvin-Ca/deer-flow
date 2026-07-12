"""DeerFlow tool wrappers for the application-level CE cost workflow."""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

from .nodes import calc_tool_node
from .state import CostNodeName
from .workflow import get_workflow_state, resume_workflow, run_workflow_node, start_workflow


def cost_workflow_start(
    feature: str | None = None,
    features: list[dict[str, Any]] | None = None,
    spec: str | None = None,
    region: str = "深圳",
    period: str | None = None,
    quantity: float | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """Start the application-level stateful construction-cost workflow.

    Use this for a complete pricing task that needs bill matching, bill selection,
    pricing data retrieval, deterministic calculation boundaries, and possible HITL.
    Do not call this for one-off known-key lookups.

    Args:
        feature: Single component or construction method description.
        features: Multiple feature objects, each with at least description/feature/name.
        spec: Bill standard version, usually 2013 or 2024. Defaults to Shenzhen 2013.
        region: Pricing region. Defaults to 深圳.
        period: Optional price period such as YYYY-MM.
        quantity: Optional quantity for the single feature.
        top_k: Number of bill candidates to recall.
    """
    return start_workflow(
        feature=feature,
        features=features,
        spec=spec,
        region=region,
        period=period,
        quantity=quantity,
        top_k=top_k,
    )


def cost_workflow_node(
    node: CostNodeName,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run a single application-level cost-workflow node.

    Use this for intermediate process tasks such as "only recall bill candidates",
    "query price for this material", "fetch quota by code", or deterministic
    rollup/check steps. The lead agent chooses the node; the node owns its own
    narrow tool or calculation boundary.

    Args:
        node: Node name to run. Supported values include bill_match, select_bill,
            price_compose, bill_get, quota_get, price_query, fee_rate_lookup,
            unit_price, rollup, and check.
        payload: Node input object.
    """
    return run_workflow_node(node, payload)


def cost_workflow_resume(
    task_id: str,
    selected_code: str | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume a paused application-level cost workflow after human input.

    Args:
        task_id: Workflow task id returned by cost_workflow_start.
        selected_code: Human-confirmed bill code when the paused node is select_bill.
        decision: Optional normalized decision object. It may contain selected_code.
    """
    normalized = dict(decision or {})
    if selected_code:
        normalized["selected_code"] = selected_code
    return resume_workflow(task_id, normalized)


def cost_workflow_state(task_id: str) -> dict[str, Any]:
    """Read the current application-level cost workflow state.

    Args:
        task_id: Workflow task id returned by cost_workflow_start.
    """
    return get_workflow_state(task_id)


def cost_calc(
    target: str | None = None,
    operation: str | None = None,
    components: list[dict[str, Any]] | None = None,
    unit_price: float | None = None,
    quantity: float | None = None,
    items: list[dict[str, Any]] | None = None,
    management_rate: float | None = None,
    profit_rate: float | None = None,
    risk_rate: float | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a single deterministic construction-cost calculation (LLM never does math).

    Use this when the user asks to calculate one value in the pricing process,
    such as 综合单价 (unit rate), 清单合价 (line total), 汇总 (rollup), or a
    完整性检查 (check) — without starting the stateful workflow. Formulas come
    from built-in calculation rules; unsupported targets pause for human rules
    instead of guessing. When no fee rates are given the result is flagged
    rates_missing (综合单价 degenerates to 人材机费) — relay that caveat.

    Args:
        target: Calculation target for chained computation, unit_rate (综合单价)
            or line_total (清单合价). The engine computes prerequisite layers
            automatically and returns a step-by-step breakdown.
        operation: Single-step operation, one of unit_price, unit_rate,
            line_total, rollup, check. Ignored when target is given.
        components: 工料机行 for unit_rate/unit_price, each with category
            (人工/材料/机械), consumption (or quantity), and unit_price (or price).
        unit_price: 综合单价 for line_total. Omit to compute it from components first.
        quantity: 工程量 for line_total.
        items: Rows for rollup (each with amount) or check (bill items to validate).
        management_rate: 企业管理费率 as a decimal, e.g. 0.1. Should come from
            fee_rate_lookup or the user, never invented.
        profit_rate: 利润率 as a decimal.
        risk_rate: 风险费率 as a decimal.
        payload: Extra input fields not covered by the explicit args (merged in,
            explicit args win).
    """
    merged = dict(payload or {})
    explicit = {
        "target": target,
        "operation": operation,
        "components": components,
        "unit_price": unit_price,
        "quantity": quantity,
        "items": items,
        "management_rate": management_rate,
        "profit_rate": profit_rate,
        "risk_rate": risk_rate,
    }
    merged.update({k: v for k, v in explicit.items() if v is not None})
    # 兼容 rates 块写法（与 verify_cost 入参同形）：展平到顶层供 unit_rate 读取，已有顶层键不覆盖。
    rates = merged.pop("rates", None)
    if isinstance(rates, dict):
        for key, value in rates.items():
            merged.setdefault(key, value)
    return calc_tool_node(merged)


cost_workflow_start_tool = tool("cost_workflow_start", parse_docstring=True)(cost_workflow_start)
cost_workflow_node_tool = tool("cost_workflow_node", parse_docstring=True)(cost_workflow_node)
cost_workflow_resume_tool = tool("cost_workflow_resume", parse_docstring=True)(cost_workflow_resume)
cost_workflow_state_tool = tool("cost_workflow_state", parse_docstring=True)(cost_workflow_state)
cost_calc_tool = tool("cost_calc", parse_docstring=True)(cost_calc)

__all__ = [
    "cost_calc",
    "cost_calc_tool",
    "cost_workflow_node",
    "cost_workflow_node_tool",
    "cost_workflow_resume",
    "cost_workflow_resume_tool",
    "cost_workflow_start",
    "cost_workflow_start_tool",
    "cost_workflow_state",
    "cost_workflow_state_tool",
]
