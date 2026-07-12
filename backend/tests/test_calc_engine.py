"""calc_engine 智能计算引擎单测（纯函数，零 mock；契约与原 nodes 计算家族一致 + 新增诚实性标注）。"""
from __future__ import annotations

from app.ce.cost.calc_engine import calc_dispatch, calc_unit_rate, compute_cost
from app.ce.cost.calc_engine import cost_calc

_COMPONENTS = [
    {"category": "人工", "name": "综合用工", "consumption": 1.0, "unit_price": 150},
    {"category": "材料", "name": "混凝土", "consumption": 1.01, "unit_price": 400},
    {"category": "机械", "name": "振捣器", "consumption": 0.1, "unit_price": 50},
]
_RMM = 150.0 + 404.0 + 5.0  # 559.0


# ── 综合单价：带费率正常算 ──
def test_unit_rate_with_rates():
    r = calc_unit_rate({"components": _COMPONENTS, "management_rate": 0.1, "profit_rate": 0.05})
    assert r["status"] == "done"
    assert r["rmm_cost"] == _RMM
    assert r["unit_price"] == round(_RMM * 1.15, 2)
    assert "rates_missing" not in r
    assert any(row["item"] == "综合单价" for row in r["breakdown"])


# ── 诚实性标注：费率全缺 → rates_missing 显式标出（综合单价=人材机费不是静默近似） ──
def test_unit_rate_flags_missing_rates():
    r = calc_unit_rate({"components": _COMPONENTS})
    assert r["status"] == "done"
    assert r["unit_price"] == _RMM  # 无费率 → 退化为人材机费
    assert r["rates_missing"] is True
    assert "警" in r["rate_provenance"]["warning"] or "费率未提供" in r["rate_provenance"]["warning"]


def test_rates_missing_propagates_through_compute_chain():
    r = compute_cost({"target": "line_total", "components": _COMPONENTS, "quantity": 10})
    assert r["status"] == "done" and r["rates_missing"] is True
    assert r["total_price"] == round(_RMM * 10, 2)
    assert r["steps"] == ["unit_rate", "line_total"]


def test_no_flag_when_unit_price_given_directly():
    r = compute_cost({"target": "line_total", "unit_price": 850.0, "quantity": 10})
    assert r["status"] == "done" and "rates_missing" not in r
    assert r["total_price"] == 8500.0


# ── capability_gap 闸：未知 target / operation 交人，不猜 ──
def test_unknown_target_capability_gap():
    r = compute_cost({"target": "grand_total"})
    assert r["status"] == "awaiting_input"
    assert r["interrupt"]["gate_type"] == "capability_gap"
    assert r["interrupt"]["detail"]["supported_targets"] == ["unit_rate", "line_total"]


def test_unknown_operation_capability_gap():
    r = calc_dispatch({"operation": "整体项目成本"})
    assert r["status"] == "awaiting_input"
    assert r["interrupt"]["gate_type"] == "capability_gap"


# ── cost_calc 工具：显式参数与 payload 合并（显式优先），rates 块兼容 ──
def test_tool_explicit_args_merge():
    r = cost_calc(target="unit_rate", components=_COMPONENTS, management_rate=0.1, profit_rate=0.05)
    assert r["status"] == "done" and r["unit_price"] == round(_RMM * 1.15, 2)


def test_tool_explicit_args_win_over_payload():
    r = cost_calc(operation="line_total", unit_price=100.0, quantity=3.0, payload={"unit_price": 999.0})
    assert r["amount"] == 300.0


def test_tool_rates_block_compat():
    r = cost_calc(target="unit_rate", payload={"components": _COMPONENTS, "rates": {"management_rate": 0.1, "profit_rate": 0.05}})
    assert r["unit_price"] == round(_RMM * 1.15, 2)
