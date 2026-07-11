"""清单选码核实器（verify_bill_code）—— lead 直调的确定性核实工具（引擎薄壳）。

对应 CLAUDE.md §1 能力 2：给定项目特征 + 待核清单编码，核实「编码对不对、特征少没少」。
匹配逻辑全部在 ``bill_match_engine.match_bill``（核实模式）——与 workflow 选码节点
（``nodes.bill_match_node`` / ``select_bill_node``）同一底座：``ce-db_bill_get`` 真值 +
``ce-rag_match_bill_item`` 召回交叉核对 + 特征项 diff。本模块只负责工具面（名称/schema/注入
本模块的 ``call_mcp_tool``，单测在此打 monkeypatch）。

与 ``verify.py``（组价结果复核 ``verify_cost``，纯函数无 I/O）分工不同：本工具查真值核选码；
语义贴切度不判（那是 cost-agent / cost-critic 的 LLM 那半）。
verdict 口径全局一致：任一 critical → fail；无 critical 有 warn → doubt；全清 → pass。
"""
from __future__ import annotations

from typing import Any

from langchain.tools import tool

from .bill_match_engine import match_bill
from .mcp import call_mcp_tool


def verify_bill_code(
    feature: str,
    code: str,
    spec: str | None = None,
    provided_features: list[str] | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """核实一条清单选码：编码是否合法存在、是否匹配项目特征、特征项有无遗漏。

    Deterministic checker for a bill-code choice. It validates code format, fetches
    the bill spec truth from ce-db, diffs the required feature schema against what
    the user provided, and cross-checks the code against ce-rag recall candidates.
    Use this when the user gives a project feature plus a candidate bill code and
    asks whether the code is right or whether features are missing. Not for picking
    a new code from scratch (delegate that instead).

    Args:
        feature: Project feature description of the component or construction method.
        code: Bill code to verify, 9 or 12 digits.
        spec: Bill standard version, 2013 or 2024. Defaults to Shenzhen 2013.
        provided_features: Feature item names the user has already filled in.
            If omitted, feature names are matched against the description text.
        top_k: Number of recall candidates for the cross-check.
    """
    return match_bill(
        feature=feature,
        spec=spec,
        code=code,
        provided_features=provided_features,
        top_k=top_k,
        call_tool=call_mcp_tool,
    )


verify_bill_code_tool = tool("verify_bill_code", parse_docstring=True)(verify_bill_code)

__all__ = ["verify_bill_code", "verify_bill_code_tool"]
