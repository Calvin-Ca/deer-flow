"""splitter 纯函数单元测试 —— 无 IO、无外部服务，锁住 regex 重灾区的边界行为。

覆盖 splitter 包里**无状态纯函数**（建树/引用/目录解析的解析逻辑），它们是 node_path
识别、引用图分型、目录条目解析的核心，正则边界多、最易回归：

  - tree_builder.classify_heading / _parent_path / _resolve_parent  —— 条款号 → 路径/父链
  - references.extract_references                                   —— 散文 → 分型引用边（含精度闸）
  - catalog_labeler._split_catalog_line / _is_catalog_list / _norm —— 目录条目解析

运行（从 ce-code 根，单行）：
  uv run python tests/test_splitter_pure.py        # 独立跑（无需 pytest，stdlib assert）
  uv run pytest tests/test_splitter_pure.py        # 若装了 pytest 亦可发现 test_* 函数
"""
from __future__ import annotations

from splitter.tree_builder import classify_heading, _parent_path, _resolve_parent
from splitter import references
from splitter.catalog_labeler import _split_catalog_line, _is_catalog_list, _norm


# ── classify_heading：标题文字 → node_path / 来源 / 置信 ──────────────────────────

def test_classify_heading_number():
    """本规范条号命中 number 正则、置信 1.0。"""
    for txt, path in [("5 总则", "5"), ("5.3 防火分区", "5.3"), ("5.3.4 燃气", "5.3.4")]:
        info = classify_heading(txt)
        assert info is not None and info["node_path"] == path
        assert info["node_path_source"] == "number"
        assert info["node_path_confidence"] == 1.0


def test_classify_heading_appendix():
    """附录根 / 附录字母条号均判 number。"""
    assert classify_heading("附录E")["node_path"] == "附录E"
    assert classify_heading("附录 E 计算方法")["node_path"] == "附录E"
    appc = classify_heading("E.1.1 计算")
    assert appc["node_path"] == "E.1.1" and appc["node_path_source"] == "number"


def test_classify_heading_unnumbered():
    """无编号标题取标题文字（text_level 兜底，置信 0.6）。"""
    info = classify_heading("术语和定义")
    assert info["node_path"] == "术语和定义"
    assert info["node_path_source"] == "text_level"
    assert info["node_path_confidence"] == 0.6


def test_classify_heading_cross_ref_fragment_is_none():
    """数字后紧跟 节/条/款/项 = 交叉引用片段，返回 None（不建节点）。"""
    assert classify_heading("5.3节规定的防火分区") is None
    assert classify_heading("5.2.1条") is None


# ── _parent_path / _resolve_parent：号段反推父链 ─────────────────────────────────

def test_parent_path():
    """号段反推父路径；顶层（章/附录根/无编号标题）→ None。"""
    assert _parent_path("5.3.4") == "5.3"
    assert _parent_path("5.3") == "5"
    assert _parent_path("5") is None
    assert _parent_path("E.1.1") == "E.1"
    assert _parent_path("E.1") == "附录E"
    assert _parent_path("附录E") is None
    assert _parent_path("前言") is None


def test_resolve_parent_skips_missing_levels():
    """中间层级缺节点时继续上探到最近存在的祖先。"""
    by_path = {"5": {}, "5.3.4": {}}  # 缺 5.3
    assert _resolve_parent("5.3.4", by_path) == "5"   # 5.3 不存在 → 上探到 5
    assert _resolve_parent("5.3", by_path) == "5"
    assert _resolve_parent("5", by_path) is None


# ── extract_references：散文 → 分型引用边 + 精度闸 ───────────────────────────────

def _tos(refs):
    """引用边列表 → {(to, type)} 集合，便于断言。"""
    return {(r["to"], r["type"]) for r in refs}


def test_extract_references_typed():
    """分型关键词命中对应 type；裸引用默认 strong。"""
    assert _tos(references.extract_references("应符合5.2.1的规定")) == {("5.2.1", "strong")}
    assert _tos(references.extract_references("参见5.3.1")) == {("5.3.1", "weak")}
    assert _tos(references.extract_references("本条不适用于5.4.2")) == {("5.4.2", "exclude")}
    assert _tos(references.extract_references("第5.2.1条")) == {("5.2.1", "strong")}


def test_extract_references_dedup_and_self():
    """(to,type) 去重；剔除自引。"""
    assert _tos(references.extract_references("见5.2.1，又见5.2.1")) == {("5.2.1", "strong")}
    assert references.extract_references("本条即5.3.1", self_path="5.3.1") == []


def test_extract_references_cross_standard():
    """跨规范号 → cross_standard 边；其内部数字不另抽为本规范条号。"""
    refs = references.extract_references("应符合《建筑设计防火规范》GB 50016-2014 的规定")
    assert ("GB 50016-2014", "cross_standard") in _tos(refs)
    assert not any(r["type"] != "cross_standard" for r in refs)  # 不误抓 50016/2014


def test_extract_references_precision_drops_measures():
    """精度闸：量值（号后接单位）与表/图引用（号前 表/图/式）不入引用边。"""
    assert references.extract_references("防火间距不应小于6.0m") == []
    assert references.extract_references("耐火极限1.5h") == []
    assert references.extract_references("坡度5.3%") == []
    assert references.extract_references("净高3.6米") == []
    assert references.extract_references("见表5.3") == []
    assert references.extract_references("如图5.3所示") == []


def test_extract_references_precision_keeps_real_refs():
    """精度闸召回安全：有锚点 / 有引用措辞的真实条款引用仍保留。"""
    assert _tos(references.extract_references("第5.3条")) == {("5.3", "strong")}        # 第…条 锚点
    assert _tos(references.extract_references("应符合5.3的规定")) == {("5.3", "strong")}  # 两段无锚但量值闸不误杀
    assert _tos(references.extract_references("见5.3.1")) == {("5.3.1", "strong")}       # 三段号


# ── catalog_labeler：目录条目行解析 ──────────────────────────────────────────────

def test_split_catalog_line():
    """目录行剥尾部页码 + 导引点 → (标题, 页码)。"""
    assert _split_catalog_line("5.3 防火分区 …… 23") == ("5.3 防火分区", 23)
    assert _split_catalog_line("1 总则（1）") == ("1 总则", 1)
    assert _split_catalog_line("术语和定义") == ("术语和定义", None)


def test_is_catalog_list():
    """过半条目以页码结尾 → 整列判目录页。"""
    assert _is_catalog_list(["1 总则 … 1", "2 术语 … 3", "5 防火 … 9"]) is True
    assert _is_catalog_list(["这是一段普通正文", "另一段正文", "再一段"]) is False
    assert _is_catalog_list([]) is False


def test_norm():
    """归一化：全角数字/点转半角、去所有空白。"""
    assert _norm("５.３　防火") == "5.3防火"
    assert _norm(" 5 . 3 ") == "5.3"


# ── 独立运行器（无 pytest 时用）──────────────────────────────────────────────────

def _run() -> int:
    """收集本模块所有 test_* 函数顺序执行，打印通过/失败，返回失败数（退出码）。"""
    tests = {k: v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)}
    failed = 0
    for name, fn in tests.items():
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(_run())
