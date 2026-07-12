"""智能询价引擎——能力 4 的单源底座（lead 工具与 workflow 询价复用，2026-07-12）。

架构同能力 2/3（共享引擎、各套壳）：
- **lead 面**：``price_query`` 工具（本模块底部薄壳）——材料/规格/期号取深圳信息价，
  多期走势对比（显式期号列表逐期取数 + 引擎确定性算价差，C-04：差价在代码算、不入 LLM）；
- **workflow 面**：``nodes.price_query_node``（单点取价）与 ``nodes.price_review_node`` 的
  启发式询价（``query_with_fallback``：精确子串 miss → 近似料召回）复用本模块。

诚实红线：零命中如实 ``no_source`` 不编价；近似料只作候选推荐不硬定；走势缺哪期如实列出
（服务端单期查询语义：period=YYYY-MM，缺期=各料最新期；无「列可用期」API，故走势按调用方
显式给出的期号逐期取）。region 过 agent 面口径闸（默认仅深圳，他省服务层硬拒——EH-03 纵深）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.tools import tool

from .bill_match_engine import as_candidates
from .mcp import call_mcp_tool
from .state import normalize_region, unsupported_region_error

_McpCall = Callable[[str, dict[str, Any]], dict[str, Any]]

MAX_TREND_PERIODS = 12  # 走势逐期取数上限（防滥用：一年足矣）


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def query_price(
    name: str,
    region: str | None = None,
    period: str | None = None,
    category: str | None = None,
    top_k: int = 10,
    *,
    call_tool: _McpCall | None = None,
) -> dict[str, Any]:
    """单期信息价取数（``ce-db_price_query``，登载值原样返回不加工）。

    返回：``{status: done|need_review|no_source|blocked|unsupported_region, candidates, ...}``；
      多条候选 = 同名多规格 → need_review 由用户选规格；零命中 → no_source（诚实缺口，不编价）。
    """
    region_err = unsupported_region_error(region)
    if region_err:
        return region_err
    if not str(name or "").strip():
        return {"status": "awaiting_input", "required_fields": ["name"], "message": "请提供要询价的材料/人工/机械名称"}
    call = call_tool or call_mcp_tool
    arguments = _clean({"name": str(name).strip(), "region": normalize_region(region), "period": period, "category": category, "top_k": int(top_k)})
    tool_result = call("ce-db_price_query", arguments)
    if tool_result.get("status") != "ok":
        return {"status": "blocked", "error": tool_result, "arguments": arguments}
    candidates = as_candidates(tool_result.get("result"))
    status = "no_source" if not candidates else ("done" if len(candidates) == 1 else "need_review")
    return {
        "status": status,
        "candidates": candidates,
        "count": len(candidates),
        "period": period,
        "region": normalize_region(region),
        "result": tool_result.get("result"),
        "provenance": {"source": "ce-db_price_query", "arguments": arguments},
    }


def suggest_price(name: str, region: str | None = None, category: str | None = None, top_k: int = 5, *, call_tool: _McpCall | None = None) -> list[dict[str, Any]]:
    """近似料启发式召回（``ce-db_price_suggest``：n-gram 宽松召回 + 同类打分）；失败/无命中返回空。"""
    region_err = unsupported_region_error(region)
    if region_err:
        return []
    call = call_tool or call_mcp_tool
    suggest = call("ce-db_price_suggest", _clean({"name": str(name or "").strip(), "region": normalize_region(region), "category": category, "top_k": int(top_k)}))
    if suggest.get("status") != "ok":
        return []
    return as_candidates(suggest.get("result"))


def query_with_fallback(name: str, region: str | None = None, period: str | None = None, category: str | None = None, top_k: int = 5, *, call_tool: _McpCall | None = None) -> list[dict[str, Any]]:
    """两层启发式询价：精确/子串查价 miss → 近似料召回（price_review 的候选来源，宁缺毋造）。"""
    exact = query_price(name, region, period, category, top_k, call_tool=call_tool)
    candidates = exact.get("candidates") or []
    if candidates:
        return candidates
    return suggest_price(name, region, category, top_k, call_tool=call_tool)


def price_trend(
    name: str,
    periods: list[str],
    region: str | None = None,
    category: str | None = None,
    *,
    call_tool: _McpCall | None = None,
) -> dict[str, Any]:
    """多期走势对比：按显式期号列表逐期取数，引擎确定性算环比价差（C-04：不入 LLM）。

    返回：``{status: done|insufficient_data|..., series, deltas, missing_periods}``；
      仅一期有数 → insufficient_data 如实说明（不外推、不编价）。同名多规格时按
      「名称+规格」分组，只对每组独立算差。
    """
    region_err = unsupported_region_error(region)
    if region_err:
        return region_err
    periods = [str(p).strip() for p in (periods or []) if str(p).strip()][:MAX_TREND_PERIODS]
    if len(periods) < 2:
        return {"status": "awaiting_input", "required_fields": ["periods"], "message": "走势对比至少需要两个期号（YYYY-MM）"}

    series: list[dict[str, Any]] = []
    missing: list[str] = []
    for period in periods:
        got = query_price(name, region, period, category, top_k=10, call_tool=call_tool)
        if got["status"] == "blocked":
            return {"status": "blocked", "error": got.get("error"), "period": period}
        if got.get("candidates"):
            for c in got["candidates"]:
                series.append({"period": period, **{k: c.get(k) for k in ("name", "spec", "unit", "price", "price_type")}})
        else:
            missing.append(period)

    # 按 名称+规格 分组算环比（同名多规格各自成线，不跨规格比价）
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in series:
        groups.setdefault((str(row.get("name")), str(row.get("spec"))), []).append(row)
    deltas: list[dict[str, Any]] = []
    for (g_name, g_spec), rows in groups.items():
        rows.sort(key=lambda r: str(r.get("period")))
        for prev, curr in zip(rows, rows[1:]):
            p0, p1 = prev.get("price"), curr.get("price")
            if isinstance(p0, (int, float)) and isinstance(p1, (int, float)) and p0:
                deltas.append({
                    "name": g_name, "spec": g_spec,
                    "from_period": prev["period"], "to_period": curr["period"],
                    "delta": round(float(p1) - float(p0), 2),
                    "pct": round((float(p1) - float(p0)) / float(p0) * 100, 2),
                })

    periods_with_data = sorted({row["period"] for row in series})
    status = "done" if len(periods_with_data) >= 2 else "insufficient_data"
    return {
        "status": status,
        "series": series,
        "deltas": deltas,
        "periods_with_data": periods_with_data,
        "missing_periods": missing,
        "region": normalize_region(region),
        "provenance": {"source": "ce-db_price_query", "periods": periods},
    }


def price_query(
    material: str,
    period: str | None = None,
    periods: list[str] | None = None,
    category: str | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    """查深圳信息价：按材料/规格/期号取价，或给多个期号做走势对比（确定性算价差）。

    Shenzhen info-price lookup. Single mode fetches published prices for a
    material (fuzzy name match; multiple specs come back as candidates for the
    user to pick; zero hits return no_source honestly — never fabricate a
    price). Trend mode activates when ``periods`` lists two or more months and
    returns the per-period series plus deterministic month-over-month deltas.
    Region is locked to Shenzhen. Not for composing quota prices of a bill item
    (use quota_recommend or the cost workflow).

    Args:
        material: Material, labor, or machinery name, e.g. HRB400 钢筋.
        period: Single period YYYY-MM. Omit to use the latest published period.
        periods: Two or more periods YYYY-MM for trend comparison. Overrides period.
        category: Optional filter, one of 人工 / 材料 / 机械.
        top_k: Max price rows for single mode.
    """
    if periods and len([p for p in periods if str(p).strip()]) >= 2:
        return price_trend(material, periods, category=category, call_tool=call_mcp_tool)
    return query_price(material, period=period, category=category, top_k=top_k, call_tool=call_mcp_tool)


price_query_tool = tool("price_query", parse_docstring=True)(price_query)

__all__ = [
    "MAX_TREND_PERIODS", "price_query", "price_query_tool", "price_trend",
    "query_price", "query_with_fallback", "suggest_price",
]
