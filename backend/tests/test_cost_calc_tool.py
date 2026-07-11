"""cost_calc 单点计算工具单测（直连 calc_tool_node 确定性引擎，无服务依赖）。"""
from __future__ import annotations

from app.ce.cost.tools import cost_calc, cost_calc_tool

# 人材机费=100(人工60+材料30+机械10)，管理费 10% + 利润 5% → 综合单价 115。
_COMPONENTS = [
    {"category": "人工", "consumption": 6, "unit_price": 10},
    {"category": "材料", "consumption": 3, "unit_price": 10},
    {"category": "机械", "consumption": 1, "unit_price": 10},
]
_RATES = {"management_rate": 0.10, "profit_rate": 0.05, "risk_rate": 0.0}


# ── target 链：unit_rate / line_total ──
def test_target_unit_rate():
    r = cost_calc(target="unit_rate", payload={"components": _COMPONENTS, "rates": _RATES})
    assert r["status"] == "done"
    assert r["unit_price"] == 115.0
    assert "unit_rate" in r["steps"]


def test_target_line_total_from_unit_price():
    r = cost_calc(target="line_total", payload={"unit_price": 115.0, "quantity": 35.6})
    assert r["status"] == "done"
    assert r["total_price"] == round(115.0 * 35.6, 2)


def test_target_line_total_chains_through_unit_rate():
    r = cost_calc(target="line_total", payload={"components": _COMPONENTS, "rates": _RATES, "quantity": 2})
    assert r["status"] == "done"
    assert r["unit_price"] == 115.0 and r["total_price"] == 230.0
    assert r["steps"] == ["unit_rate", "line_total"]


# ── operation 单步 ──
def test_operation_rollup():
    r = cost_calc(operation="rollup", payload={"items": [{"amount": 100.0}, {"amount": 23.5}]})
    assert r["status"] == "done" and r["amount"] == 123.5


def test_operation_check_rejects_bad_code():
    r = cost_calc(operation="check", payload={"items": [{"code": "123", "name": "x", "unit": "m3"}]})
    assert r["verdict"] == "reject"


# ── HITL 边界：无输入 / 超出内置公式 → 停下，不瞎算 ──
def test_awaiting_input_when_no_target_or_operation():
    r = cost_calc()
    assert r["status"] == "awaiting_input"


def test_capability_gate_on_unknown_operation():
    r = cost_calc(operation="整体项目成本")
    assert r["status"] == "awaiting_input"
    assert r["interrupt"]["gate_type"] == "capability_gap"


# ── 工具封装可用 ──
def test_tool_wrapper_invocable():
    out = cost_calc_tool.invoke({"target": "unit_rate", "payload": {"components": _COMPONENTS, "rates": _RATES}})
    assert out["unit_price"] == 115.0
