"""造价资源归一化纯函数单测 —— 锁住定额↔信息价名称/规格/单位对齐的边界行为。

覆盖 ``cost.resource_norm`` 的无状态纯函数（同物异名匹配键、单位归一与换算系数），它们是
``resource_price_map`` matcher 的对齐核心，符号/单位变体多、最易回归：

  - canonical_key —— 规格嵌名 vs 分离、×↔x、括号/空格 → 同一匹配键
  - canonical_unit —— LaTeX 残留 ``$m^3$``、上标 ``^3`` → ``m³``
  - unit_factor —— 千块↔块 换算系数；单位不可换算 → None（红线：不猜价）

运行（从 ce-code 根，单行）：
  python3 tests/test_resource_norm.py        # 独立跑（无需 pytest，stdlib assert）
"""
from __future__ import annotations

from cost.resource_norm import canonical_key, canonical_unit, unit_factor


# ── canonical_key：定额（规格嵌名）与信息价（规格分离）应收敛到同键 ───────────────────

def test_canonical_key_spec_embedded_vs_split():
    # 定额「干混砌筑砂浆 M7.5」(spec=None) == 信息价「干混砌筑砂浆」+「M7.5」
    assert canonical_key("干混砌筑砂浆 M7.5", None) == canonical_key("干混砌筑砂浆", "M7.5")


def test_canonical_key_symbol_and_space():
    # ×↔x、括号、内部空格差异都应抹平
    a = canonical_key("普通混凝土实心砖 240×115×53 (10.0MPa)", None)
    b = canonical_key("普通混凝土实心砖", "240x115x53 10.0MPa")
    c = canonical_key("普通混凝土实心砖240×115×53(10.0MPa)", None)
    assert a == b == c


def test_canonical_key_distinguishes_real_diff():
    # 真不同的规格不应误并（M5 ≠ M7.5）
    assert canonical_key("湿拌砌筑砂浆", "M5") != canonical_key("湿拌砌筑砂浆", "M7.5")


def test_canonical_key_empty():
    assert canonical_key(None, None) == ""
    assert canonical_key("", "") == ""


# ── canonical_unit：清 LaTeX / 上标残留 ─────────────────────────────────────────

def test_canonical_unit_latex():
    assert canonical_unit("$m^3$") == canonical_unit("m³") == "m³"
    assert canonical_unit("m3") == "m³"
    assert canonical_unit("m^2") == "m²"


def test_canonical_unit_passthrough():
    assert canonical_unit("千块") == "千块"
    assert canonical_unit(None) == ""


# ── unit_factor：换算系数 + 不可换算返回 None ───────────────────────────────────

def test_unit_factor_same():
    assert unit_factor("m³", "m³") == 1.0
    # 脏单位归一后相同 → 1.0（不是 None）
    assert unit_factor("$m^3$", "m³") == 1.0


def test_unit_factor_convertible():
    assert unit_factor("千块", "块") == 1000.0
    assert unit_factor("百块", "块") == 100.0


def test_unit_factor_incompatible_is_none():
    # 不可换算（红线：不猜价）
    assert unit_factor("t", "块") is None
    assert unit_factor("kg", "m³") is None


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  ✗ {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
