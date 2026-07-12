"""清单智能匹配工具（bill_match）—— lead 直调的双模确定性工具（引擎薄壳）。

对应 CLAUDE.md §1 能力 2 的完整两面（2026-07-12 合并为单一双模工具，原单核实工具
``verify_bill_code`` 随之退役——两面本是同一匹配问题的正反向）：
- **选码**（``code`` 缺省）：项目特征 → 召回候选 → 门限自动选定 / 低置信转人工，
  选定后顺带带出缺特征提醒 + 历史纠正 few-shot（``exemplar_hints``）；
- **核实**（``code`` 给定）：编码格式 + ``ce-db_bill_get`` 真值存在性 + 特征项 diff +
  召回交叉核对 → verdict。

匹配逻辑全部在 ``bill_match_engine.match_bill``——与 workflow 选码节点
（``nodes.bill_match_node`` / ``select_bill_node``）同一底座。本模块只负责工具面
（名称/schema/注入本模块的 ``call_mcp_tool``，单测在此打 monkeypatch）。

与 ``verify.py``（组价结果复核 ``verify_cost``，纯函数无 I/O）分工不同：本工具查真值做匹配；
语义贴切度不判（那是 cost-critic 的 LLM 那半）。verdict 口径全局一致：任一 critical → fail；
无 critical 有 warn → doubt；全清 → pass。spec 过 agent 面口径闸（默认仅 2013）。
"""
from __future__ import annotations

from typing import Any

from langchain.tools import tool

from .bill_match_engine import match_bill as _match_bill
from .mcp import call_mcp_tool


def bill_match(
    feature: str,
    code: str | None = None,
    spec: str | None = None,
    provided_features: list[str] | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """清单智能匹配：给项目特征选清单编码，或核实一条已选编码对不对、特征有无遗漏。

    Deterministic bill-item matcher with two modes sharing one engine. Without
    ``code`` it recalls candidates from the project feature and auto-selects when
    confidence clears the threshold (low confidence returns candidates for human
    review, with few-shot hints from past human corrections). With ``code`` it
    verifies that choice: code format, existence in the bill spec truth (ce-db),
    required-feature diff, and a recall cross-check. Use this for single or a few
    components; for a full bill of quantities use cost_workflow_start instead.

    Args:
        feature: Project feature description of the component or construction method.
        code: Optional bill code to verify (9 or 12 digits). Omit to select a new code.
        spec: Bill standard version. Only 2013 (Shenzhen caliber) is supported;
            omit to use it by default.
        provided_features: Feature item names the user has already filled in.
            If omitted, feature names are matched against the description text.
        top_k: Number of recall candidates.
    """
    return _match_bill(
        feature=feature,
        spec=spec,
        code=code,
        provided_features=provided_features,
        top_k=top_k,
        call_tool=call_mcp_tool,
    )


bill_match_tool = tool("bill_match", parse_docstring=True)(bill_match)

__all__ = ["bill_match", "bill_match_tool"]
