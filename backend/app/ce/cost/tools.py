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
    """启动应用层的有状态组价 workflow。

    用于需要清单匹配、选码、取价、确定性计算边界以及可能人工介入（HITL）的完整组价任务。
    单次的已知键查询不要调用本工具。

    Args:
        feature: 单个构件或施工做法的描述。
        features: 多个特征对象，每个至少含 description/feature/name。
        spec: 清单规范版本，通常为 2013 或 2024。默认深圳 2013。
        region: 组价地区。默认 深圳。
        period: 价格期号，可选，如 YYYY-MM。
        quantity: 单个特征的工程量，可选。
        top_k: 召回的清单候选数量。
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
    """运行应用层组价 workflow 的单个节点。

    用于中间过程任务，如「只召回清单候选」「查某材料的价」「按编码取定额」，或确定性的
    汇总/检查步骤。由 lead agent 选择节点；节点各自持有自己狭窄的工具或计算边界。

    Args:
        node: 要运行的节点名。支持的取值包括 bill_match、select_bill、price_compose、
            bill_get、quota_get、price_query、fee_rate_lookup、unit_price、rollup、check。
        payload: 节点输入对象。
    """
    return run_workflow_node(node, payload)


def cost_workflow_resume(
    task_id: str,
    selected_code: str | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在人工输入后续跑一个暂停的应用层组价 workflow。

    Args:
        task_id: cost_workflow_start 返回的 workflow 任务 id。
        selected_code: 暂停节点为 select_bill 时，人工确认的清单编码。
        decision: 规范化的决策对象，可选，其中可含 selected_code。
    """
    normalized = dict(decision or {})
    if selected_code:
        normalized["selected_code"] = selected_code
    return resume_workflow(task_id, normalized)


def cost_workflow_state(task_id: str) -> dict[str, Any]:
    """读取当前应用层组价 workflow 的状态。

    Args:
        task_id: cost_workflow_start 返回的 workflow 任务 id。
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
