"""price_engine 智能询价引擎单测（monkeypatch 掉 MCP，纯逻辑验证）。"""
from __future__ import annotations

from typing import Any

import app.ce.cost.price_engine as pe
from app.ce.cost.price_engine import price_query_tool, price_trend, query_price, query_with_fallback

_STEEL = {"name": "HRB400钢筋", "spec": "Φ20", "unit": "t", "category": "材料", "price": 3850.0, "price_type": "信息价"}
_STEEL_25 = {"name": "HRB400钢筋", "spec": "Φ25", "unit": "t", "category": "材料", "price": 3900.0, "price_type": "信息价"}


def _fake_mcp(rows_by_period=None, rows=None, query_status="ok", suggest_rows=None):
    """按期号返回可控价格行；suggest_rows 供近似料召回。"""
    def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "ce-db_price_query":
            if query_status != "ok":
                return {"status": query_status, "tool": name, "error": "unavailable"}
            if rows_by_period is not None:
                got = rows_by_period.get(arguments.get("period"), [])
            else:
                got = rows if rows is not None else [_STEEL]
            return {"status": "ok", "tool": name, "result": {"results": got, "count": len(got)}}
        if name == "ce-db_price_suggest":
            return {"status": "ok", "tool": name, "result": {"suggestions": suggest_rows or []}}
        raise AssertionError(f"unexpected tool {name}")
    return _call


# ── region 口径闸：他省硬拒（EH-03 纵深） ──
def test_unsupported_region_rejected():
    r = query_price("螺纹钢", region="北京", call_tool=_fake_mcp())
    assert r["status"] == "unsupported_region" and "深圳" in r["message"]
    t = price_trend("螺纹钢", ["2026-04", "2026-05"], region="上海", call_tool=_fake_mcp())
    assert t["status"] == "unsupported_region"


# ── 单期取价：单条 done / 多规格 need_review / 零命中 no_source ──
def test_single_hit_done():
    r = query_price("HRB400", call_tool=_fake_mcp())
    assert r["status"] == "done" and r["count"] == 1


def test_multi_spec_need_review():
    r = query_price("HRB400", call_tool=_fake_mcp(rows=[_STEEL, _STEEL_25]))
    assert r["status"] == "need_review" and r["count"] == 2


def test_zero_hit_no_source():
    r = query_price("不存在的材料", call_tool=_fake_mcp(rows=[]))
    assert r["status"] == "no_source" and r["candidates"] == []


def test_blocked_on_service_error():
    r = query_price("HRB400", call_tool=_fake_mcp(query_status="error"))
    assert r["status"] == "blocked"


# ── 两层启发式：精确 miss → 近似料召回 ──
def test_fallback_to_suggest_when_exact_miss():
    approx = {"name": "干混砌筑砂浆M5", "price": 410.0, "score": 0.72}
    got = query_with_fallback("干混砂浆", call_tool=_fake_mcp(rows=[], suggest_rows=[approx]))
    assert got == [approx]


def test_fallback_skipped_when_exact_hits():
    got = query_with_fallback("HRB400", call_tool=_fake_mcp(rows=[_STEEL], suggest_rows=[{"name": "别的"}]))
    assert got == [_STEEL]


# ── 走势：逐期取数 + 确定性环比（C-04 差价在代码算） ──
def test_trend_deltas_computed_per_spec():
    by_period = {
        "2026-04": [dict(_STEEL, price=3800.0)],
        "2026-05": [dict(_STEEL, price=3850.0)],
        "2026-06": [dict(_STEEL, price=3790.0)],
    }
    t = price_trend("HRB400", ["2026-04", "2026-05", "2026-06"], call_tool=_fake_mcp(rows_by_period=by_period))
    assert t["status"] == "done" and len(t["series"]) == 3
    assert [d["delta"] for d in t["deltas"]] == [50.0, -60.0]
    assert t["missing_periods"] == []


def test_trend_insufficient_data_when_single_period_has_rows():
    by_period = {"2026-05": [_STEEL], "2026-06": []}
    t = price_trend("HRB400", ["2026-05", "2026-06"], call_tool=_fake_mcp(rows_by_period=by_period))
    assert t["status"] == "insufficient_data"
    assert t["missing_periods"] == ["2026-06"] and t["deltas"] == []


def test_trend_requires_two_periods():
    t = price_trend("HRB400", ["2026-05"], call_tool=_fake_mcp())
    assert t["status"] == "awaiting_input" and t["required_fields"] == ["periods"]


def test_trend_groups_by_spec_no_cross_spec_delta():
    by_period = {
        "2026-04": [dict(_STEEL, price=3800.0), dict(_STEEL_25, price=3860.0)],
        "2026-05": [dict(_STEEL, price=3850.0)],  # Φ25 缺 5 月 → 该组无环比
    }
    t = price_trend("HRB400", ["2026-04", "2026-05"], call_tool=_fake_mcp(rows_by_period=by_period))
    assert len(t["deltas"]) == 1 and t["deltas"][0]["spec"] == "Φ20"


# ── 工具封装：单期/走势双模同一工具名 price_query ──
def test_tool_wrapper_single_and_trend(monkeypatch):
    monkeypatch.setattr(pe, "call_mcp_tool", _fake_mcp(rows=[_STEEL]))
    out = price_query_tool.invoke({"material": "HRB400"})
    assert out["status"] == "done"
    by_period = {"2026-04": [dict(_STEEL, price=3800.0)], "2026-05": [dict(_STEEL, price=3850.0)]}
    monkeypatch.setattr(pe, "call_mcp_tool", _fake_mcp(rows_by_period=by_period))
    out2 = price_query_tool.invoke({"material": "HRB400", "periods": ["2026-04", "2026-05"]})
    assert out2["status"] == "done" and out2["deltas"][0]["delta"] == 50.0
