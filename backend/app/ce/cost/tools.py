"""DeerFlow tool wrappers for the cost workflow four-piece（能力 6 的模型可见面）。

单点能力的工具壳各随其引擎（2026-07-12 收拢定案）：``bill_match`` 在 bill_match_engine、
``quota_recommend`` 在 quota_engine、``price_query`` 在 price_engine、``cost_calc`` 在
calc_engine——本文件只剩 workflow 四件套（start / node / resume / state）。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

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


cost_workflow_start_tool = tool("cost_workflow_start", parse_docstring=True)(cost_workflow_start)
cost_workflow_node_tool = tool("cost_workflow_node", parse_docstring=True)(cost_workflow_node)
cost_workflow_resume_tool = tool("cost_workflow_resume", parse_docstring=True)(cost_workflow_resume)
cost_workflow_state_tool = tool("cost_workflow_state", parse_docstring=True)(cost_workflow_state)

__all__ = [
    "cost_workflow_node",
    "cost_workflow_node_tool",
    "cost_workflow_resume",
    "cost_workflow_resume_tool",
    "cost_workflow_start",
    "cost_workflow_start_tool",
    "cost_workflow_state",
    "cost_workflow_state_tool",
]
