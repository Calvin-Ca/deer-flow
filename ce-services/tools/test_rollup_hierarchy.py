#!/usr/bin/env python3
"""两级汇总原语 ``rollup_hierarchy`` 的自测（无 pytest 依赖，assert + __main__ 直跑）。

跑法（服务器）：``cd ce-services && uv run python tools/test_rollup_hierarchy.py``
（函数按 ``test_*`` 命名，将来若接 pytest 亦可被发现）。

覆盖：① 单组退化 = 旧 flat rollup 口径（subtotal/total 一致）；② 多组两级 Σ 正确；
③ 未计价（total_price=None）逐层计入 missing、不计金额；④ 税金按税前造价计。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost.pricing import (  # noqa: E402
    HierarchyItem,
    HierarchyRollupInput,
    RollupInput,
    rollup_cost,
    rollup_hierarchy,
)


def test_single_group_degenerates_to_flat_rollup() -> None:
    """单组（默认单项/单位工程）两级汇总 = 旧 flat rollup 口径：subtotal/pre_tax/total 数值一致。"""
    items = [
        HierarchyItem(single_work="默认单项工程", unit_work="默认单位工程", total_price=4898.72),
        HierarchyItem(single_work="默认单项工程", unit_work="默认单位工程", total_price=7069.20),
    ]
    hier = rollup_hierarchy(HierarchyRollupInput(items=items, tax_rate=9))
    flat = rollup_cost(RollupInput(subtotal=4898.72 + 7069.20, tax_rate=9))

    assert hier["subtotal"] == flat["subtotal"], (hier["subtotal"], flat["subtotal"])
    assert hier["pre_tax_total"] == flat["pre_tax_total"]
    assert hier["total"] == flat["total"]
    assert hier["tax"] == flat["tax"]
    # 单组树：一条单项工程 › 一条单位工程
    assert len(hier["single_works"]) == 1
    assert len(hier["single_works"][0]["unit_works"]) == 1
    assert hier["single_works"][0]["subtotal"] == hier["subtotal"]
    assert hier["missing_unit_price_items"] == 0


def test_two_level_grouping_sums() -> None:
    """两个单项工程、各含单位工程：逐层 subtotal = 其下构件合价之和，顶层 = Σ 单项工程。"""
    items = [
        HierarchyItem(single_work="1#住宅楼", unit_work="1#楼-土建", total_price=1000.0),
        HierarchyItem(single_work="1#住宅楼", unit_work="1#楼-土建", total_price=500.0),
        HierarchyItem(single_work="1#住宅楼", unit_work="1#楼-安装", total_price=200.0),
        HierarchyItem(single_work="2#配套楼", unit_work="2#楼-土建", total_price=300.0),
    ]
    hier = rollup_hierarchy(HierarchyRollupInput(items=items))

    assert hier["subtotal"] == 2000.0, hier["subtotal"]
    sw = {s["name"]: s for s in hier["single_works"]}
    assert set(sw) == {"1#住宅楼", "2#配套楼"}
    assert sw["1#住宅楼"]["subtotal"] == 1700.0
    assert sw["2#配套楼"]["subtotal"] == 300.0
    uw = {u["name"]: u for u in sw["1#住宅楼"]["unit_works"]}
    assert uw["1#楼-土建"]["subtotal"] == 1500.0
    assert uw["1#楼-土建"]["item_count"] == 2
    assert uw["1#楼-安装"]["subtotal"] == 200.0
    # 无税率 → total 为 None（不杜撰税金），pre_tax = subtotal（无项目级费用）
    assert hier["total"] is None
    assert hier["pre_tax_total"] == 2000.0


def test_missing_items_counted_not_summed() -> None:
    """未计价构件（total_price=None）逐层计入 missing、不计金额，不虚构总价。"""
    items = [
        HierarchyItem(single_work="A", unit_work="A-1", total_price=800.0),
        HierarchyItem(single_work="A", unit_work="A-1", total_price=None),  # 缺基价/缺 Q
        HierarchyItem(single_work="A", unit_work="A-2", total_price=None),
    ]
    hier = rollup_hierarchy(HierarchyRollupInput(items=items))

    assert hier["subtotal"] == 800.0  # 仅计已计价的一条
    assert hier["missing_unit_price_items"] == 2
    sw = hier["single_works"][0]
    assert sw["missing_unit_price_items"] == 2
    assert sw["item_count"] == 3
    uw = {u["name"]: u for u in sw["unit_works"]}
    assert uw["A-1"]["missing_unit_price_items"] == 1 and uw["A-1"]["subtotal"] == 800.0
    assert uw["A-2"]["missing_unit_price_items"] == 1 and uw["A-2"]["subtotal"] == 0.0


def test_project_fees_and_tax() -> None:
    """项目级费用（措施/其他/规费）+ 税金按税前造价计（复用 rollup_cost 口径）。"""
    items = [HierarchyItem(single_work="P", unit_work="P-1", total_price=10000.0)]
    hier = rollup_hierarchy(HierarchyRollupInput(
        items=items, measure_fee=1000.0, other_fee=500.0, fee_levy=300.0, tax_rate=9))

    assert hier["pre_tax_total"] == 11800.0  # 10000 + 1000 + 500 + 300
    assert hier["tax"] == 1062.0  # 11800 * 9%
    assert hier["total"] == 12862.0


def _main() -> None:
    tests = [
        test_single_group_degenerates_to_flat_rollup,
        test_two_level_grouping_sums,
        test_missing_items_counted_not_summed,
        test_project_fees_and_tax,
    ]
    for t in tests:
        t()
        print(f"  [pass] {t.__name__}")
    print(f"OK - {len(tests)} passed")


if __name__ == "__main__":
    _main()
