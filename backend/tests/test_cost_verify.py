"""cost_verify 确定性复核器单测（纯函数，无服务依赖）。"""
from __future__ import annotations

from app.ce.cost.verify import cost_verify_tool, recompute_unit_price, verify_cost_result

# 一套自洽的组价结果：人材机费=100(人工60+材料30+机械10)，管理费率10%+利润5%+风险0 → 综合单价=115。
_COMPONENTS = [
    {"category": "人工", "consumption": 6, "unit_price": 10},   # 60
    {"category": "材料", "consumption": 3, "unit_price": 10},   # 30
    {"category": "机械", "consumption": 1, "unit_price": 10},   # 10
]
_RATES = {"management_rate": 0.10, "profit_rate": 0.05, "risk_rate": 0.0}


def _result(**over):
    base = {"code": "010502001", "region": "深圳", "spec": "2024",
            "components": _COMPONENTS, "rates": _RATES, "claimed_unit_price": 115.0}
    base.update(over)
    return base


# ── 独立重算 ──
def test_recompute_matches_hand_calc():
    r = recompute_unit_price(_COMPONENTS, _RATES)
    assert r["rmm"] == 100.0
    assert r["management_fee"] == 10.0 and r["profit"] == 5.0 and r["risk_fee"] == 0.0
    assert r["unit_price"] == 115.0 and r["skipped"] == 0


def test_recompute_skips_nonnumeric_rows():
    comps = _COMPONENTS + [{"category": "材料", "consumption": None, "unit_price": 5}]
    assert recompute_unit_price(comps, _RATES)["skipped"] == 1


def test_recompute_rate_key_aliases():
    r = recompute_unit_price(_COMPONENTS, {"management_fee_rate": 0.10, "profit_rate": 0.05})
    assert r["unit_price"] == 115.0  # 兼容 *_fee_rate 写法


# ── 算术复核：匹配 / 不符 ──
def test_arith_pass_when_claimed_matches():
    out = verify_cost_result(_result(claimed_unit_price=115.0))
    assert out["verdict"] == "pass" and out["findings"] == []


def test_arith_mismatch_is_critical_fail():
    out = verify_cost_result(_result(claimed_unit_price=130.0))  # agent 报错了数
    assert out["verdict"] == "fail"
    assert any(f["type"] == "arith_mismatch" and f["severity"] == "critical" for f in out["findings"])


def test_arith_within_tolerance_passes():
    out = verify_cost_result(_result(claimed_unit_price=115.01))  # 分位误差在容差内
    assert out["verdict"] == "pass"


def test_claimed_without_components_is_doubt():
    out = verify_cost_result({"code": "010502001", "region": "深圳", "spec": "2024", "claimed_unit_price": 200.0})
    assert out["verdict"] == "doubt"
    assert any(f["type"] == "uncheckable_price" for f in out["findings"])


# ── 编码格式 ──
def test_code_12digit_ok():
    out = verify_cost_result(_result(code="010502001001"))
    assert out["code"] == "010502001" and not any(f["type"] == "code_format" for f in out["findings"])


def test_code_malformed_is_critical():
    out = verify_cost_result(_result(code="ABC123"))
    assert out["verdict"] == "fail"
    assert any(f["type"] == "code_format" for f in out["findings"])


# ── 地域/版本隔离（串库红线） ──
def test_region_leak_is_critical():
    out = verify_cost_result(_result(region="北京"))
    assert out["verdict"] == "fail"
    assert any(f["type"] == "region_leak" and f["severity"] == "critical" for f in out["findings"])


def test_spec_invalid_is_critical():
    out = verify_cost_result(_result(spec="2020"))
    assert any(f["type"] == "spec_invalid" for f in out["findings"])


# ── 完整性 / 价格覆盖 ──
def test_incomplete_components_downgrade_to_warn_not_arith_fail():
    comps = _COMPONENTS + [{"category": "材料", "consumption": None, "unit_price": 5}]
    out = verify_cost_result(_result(components=comps))
    # 有行缺数值 → 重算不完整，不拿它判 arith_mismatch（降级 warn）
    assert out["verdict"] == "doubt"
    assert any(f["type"] == "components_incomplete" for f in out["findings"])
    assert not any(f["type"] == "arith_mismatch" for f in out["findings"])


def test_missing_material_price_is_warn():
    mats = [{"std": "钢筋HRB400", "price": {"value": 4000}}, {"std": "商砼C30", "price": {"value": None}}]
    out = verify_cost_result(_result(materials=mats))
    assert out["verdict"] == "doubt"
    assert any(f["type"] == "price_missing" for f in out["findings"])


# ── 工具封装 ──
def test_tool_wrapper_invokes():
    out = cost_verify_tool.invoke({"result": _result()})
    assert out["verdict"] == "pass"
