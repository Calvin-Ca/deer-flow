#!/usr/bin/env python3
"""Backlog 落地自测：``recompute_rollup``（re-rollup 内核）+ ``select_quota``（套定额模型挂点红线）。

跑法（服务器）：``cd ce-services && uv run python tools/test_backlog.py``
（无 pytest 依赖，assert + __main__ 直跑；函数按 ``test_*`` 命名，将来接 pytest 亦可发现）。

覆盖：
  recompute_rollup —— ① tax=0 → total==税前（不变量）；② 提高税率 → total 增；③ 改费率逐件重算综合单价；
    ④ 纯函数：不改入参 state。
  select_quota —— ⑤ 未接模型 → 多子目 need_review（行为不变）；⑥ 接入模型高置信在候选内 → 选中自动过；
    ⑦ 越界子目号 → 作废转人工（红线1）；⑧ 低置信 → 强制 need_review。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost import quota_selection  # noqa: E402
from cost.graph import recompute_rollup  # noqa: E402


def _base_state() -> dict:
    """单构件、已算好综合合价的会话累积态（供 recompute 测试）。"""
    return {
        "items": [{
            "feature": "C30 现浇钢筋混凝土矩形柱",
            "single_work": "单项工程1", "unit_work": "单位工程1",
            "quota_basis": {"labor_cost": 100.0, "material_cost": 300.0, "machine_cost": 50.0},
            "quantity": 10.0,
            "unit_price": {"total_price": 1000.0},  # 预置综合合价（params-only 重算不动它）
        }],
        "rates": {"management_fee_rate": 15.0, "profit_rate": 8.0, "risk_rate": 0.0, "fee_base": "labor_machine"},
        "params": {"measure_fee": 0.0, "other_fee": 0.0, "fee_levy": 0.0, "tax_rate": 9.0},
    }


def test_recompute_tax_zero_equals_pretax() -> None:
    """税率=0 覆盖 → 总造价 == 税前造价（税金不变量，与百分比/小数口径无关）。"""
    out = recompute_rollup(_base_state(), params_override={"tax_rate": 0})
    r = out["rollup"]
    assert abs(r["total"] - r["pre_tax_total"]) < 1e-6, (r["total"], r["pre_tax_total"])


def test_recompute_higher_tax_raises_total() -> None:
    """提高税率 → 总造价单调增（税前不变）。"""
    low = recompute_rollup(_base_state(), params_override={"tax_rate": 6})["rollup"]
    high = recompute_rollup(_base_state(), params_override={"tax_rate": 12})["rollup"]
    assert abs(low["pre_tax_total"] - high["pre_tax_total"]) < 1e-6  # 税前一致
    assert high["total"] > low["total"], (low["total"], high["total"])


def test_recompute_rates_recomputes_unit_price() -> None:
    """改费率 → 逐件重算综合单价（更高管理费率 → 更高综合合价 → 更高总价）。"""
    lo = recompute_rollup(_base_state(), rates_override={"management_fee_rate": 10.0})
    hi = recompute_rollup(_base_state(), rates_override={"management_fee_rate": 30.0})
    lo_tp = lo["rollup"]["single_works"][0]["unit_works"][0]["subtotal"]
    hi_tp = hi["rollup"]["single_works"][0]["unit_works"][0]["subtotal"]
    assert hi_tp > lo_tp > 0, (lo_tp, hi_tp)
    assert hi["rollup"]["total"] > lo["rollup"]["total"]


def test_recompute_is_pure() -> None:
    """纯函数：不修改入参 state（items/params/rates 原样）。"""
    state = _base_state()
    snap = copy.deepcopy(state)
    recompute_rollup(state, params_override={"tax_rate": 3}, rates_override={"management_fee_rate": 20.0})
    assert state == snap, "recompute_rollup 不应修改入参 state"


# ── select_quota（套定额模型挂点）──

_QUOTAS = [
    {"子目号": "A4-31", "name": "现浇矩形柱 C30", "labor_cost": 100, "material_cost": 300, "machine_cost": 50},
    {"子目号": "A4-32", "name": "现浇异形柱 C30", "labor_cost": 120, "material_cost": 320, "machine_cost": 55},
]


def test_select_quota_no_model_needs_review() -> None:
    """未接入模型（默认）→ need_review、子目号=None（多子目维持人工确认，行为不变）。"""
    quota_selection.register_quota_selector(None)
    sel = quota_selection.select_quota("C30 矩形柱", "010502001", _QUOTAS)
    assert sel["need_review"] is True and sel["子目号"] is None


def test_select_quota_model_high_conf_picks() -> None:
    """接入模型、高置信、候选内 → 选中、不停闸。"""
    quota_selection.register_quota_selector(
        lambda f, c, qs: {"子目号": "A4-31", "confidence": 0.95, "reason": "现浇矩形柱直配"})
    try:
        sel = quota_selection.select_quota("C30 矩形柱", "010502001", _QUOTAS)
        assert sel["子目号"] == "A4-31" and sel["need_review"] is False and sel["confidence"] == 0.95
    finally:
        quota_selection.register_quota_selector(None)


def test_select_quota_out_of_candidate_voided() -> None:
    """模型选了候选外子目号（造子目）→ 作废、转人工（红线1）。"""
    quota_selection.register_quota_selector(
        lambda f, c, qs: {"子目号": "Z9-99", "confidence": 0.99})
    try:
        sel = quota_selection.select_quota("C30 矩形柱", "010502001", _QUOTAS)
        assert sel["need_review"] is True and sel["子目号"] is None
    finally:
        quota_selection.register_quota_selector(None)


def test_select_quota_low_conf_needs_review() -> None:
    """校准后置信 < τ → 强制 need_review（只建议不定稿）。"""
    quota_selection.register_quota_selector(
        lambda f, c, qs: {"子目号": "A4-31", "confidence": 0.10})
    try:
        sel = quota_selection.select_quota("C30 矩形柱", "010502001", _QUOTAS)
        assert sel["need_review"] is True
    finally:
        quota_selection.register_quota_selector(None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} 项全过。")
