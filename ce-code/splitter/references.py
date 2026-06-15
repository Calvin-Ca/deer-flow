"""波1:引用边分型 + 双向。对应 PRD §1/§4 的【P0】引用边。

替代 02 旧的扁平 ``references_to: list[str]``。引用图是规范的知识图谱、GraphRAG
底座,所以分型 + 反向边在构建层一次建好,检索层只按 type 决定是否扩展。

  strong         应符合 / 符合…的规定 / 应按…执行  → 必拉、继承强制性
  weak           参见 / 见 / 参照 / 宜按            → 可选拉取
  exclude        不适用于 / 本条不含 / 除…外        → 禁止正向扩展(否则反引)
  cross_standard 《…》GB/JGJ/CJJ xxxxx              → 触发多规范召回

裸引用(``第5.2.1条`` 无修饰语)默认 **strong**:规范里裸引用通常是规范性引用,
且领域偏好"宁可多召不可漏"。误标的代价(多拉一条)远小于漏强条。
"""
from __future__ import annotations

import re
from typing import Literal

# 与 schema.RefType 保持一致;extract 包对 plain dict 操作,不强依赖 schema 导入路径
RefType = Literal["strong", "weak", "exclude", "cross_standard"]

# 本规范内条款号:可选「第」前缀(g1) + 2~4 段编号(g2) + 可选「条/款/项/节」量词后缀(g3)。
# 至少两段以排除年份/页码/章号。两段 d.d 与正文量值高度同形(6.0m/1.5h/坡度5.3%),故
# extract_references 对**无锚点**(无 第/条款项节)的号再加两道召回安全的精度闸,见该函数。
_NUM_RE = re.compile(r"(第)?\s*(\d+\.\d+(?:\.\d+){0,2})\s*(条|款|项|节)?")

# 号后紧跟量纲单位 → 这是量值不是条款引用(6.0m/1.5h/0.5MPa/5.3%/3.6kN)。召回安全:
# 真实条款引用号后从不接单位。
_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:mm|cm|km|m²|m³|m2|m3|m/s|kN|kPa|MPa|Pa|kg|m|h|s|t|N|%|‰|℃|°"
    r"|毫米|厘米|千米|米|秒|小时|倍)"
)
# 号前紧邻 表/图/式 → 这是表/图/公式引用不是条款引用(表5.3/图5.3/式5.3)。召回安全:
# 真实条款引用号前不会是这三个字。
_FIGURE_LEAD = ("表", "图", "式")

# 跨规范:《建筑设计防火规范》GB 50016-2014 / 现行国家标准…GB 50116
_CROSS_RE = re.compile(
    r"(?:《[^》]*》\s*)?((?:GB|GBJ|JGJ|CJJ|TB|DG|DGJ)\s*/?\s*[T]?\s*\d{4,5}(?:-\d+)?)"
)

# 分型关键词(在引用号前的小窗口里判断)
_EXCLUDE_KW = ("不适用", "本条不含", "本条不适用", "除外")
_WEAK_KW = ("参见", "参照", "宜按", "宜符合", "可按", "可参照", "详见")
_STRONG_KW = ("应符合", "尚应符合", "应按", "应执行", "符合本", "的规定", "应满足")


def _classify(prefix: str) -> RefType:
    """据引用号前文窗口判断引用类型;无修饰语 → 默认 strong(保守召回)。"""
    if any(k in prefix for k in _EXCLUDE_KW):
        return "exclude"
    if any(k in prefix for k in _STRONG_KW):
        return "strong"
    if any(k in prefix for k in _WEAK_KW):
        return "weak"
    return "strong"


def extract_references(text: str, self_path: str = "") -> list[dict]:
    """散文 → 分型引用边列表 ``[{to, type}]``,按 (to,type) 去重、剔除自引。"""
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # 排除句首已被《》圈起的跨规范号区间,避免本规范号正则误抓跨规范里的数字
    cross_spans: list[tuple[int, int]] = []
    for m in _CROSS_RE.finditer(text):
        to = re.sub(r"\s+", " ", m.group(1)).strip()
        cross_spans.append(m.span())
        key = (to, "cross_standard")
        if key not in seen:
            seen.add(key)
            found.append({"to": to, "type": "cross_standard"})

    for m in _NUM_RE.finditer(text):
        if any(s <= m.start() < e for s, e in cross_spans):
            continue  # 跨规范号里的内部条号,已由 cross 边覆盖
        to = m.group(2)
        if to == self_path:
            continue
        prefix = text[max(0, m.start() - 10): m.start()]
        anchored = bool(m.group(1) or m.group(3))  # 第… / …条款项节:确是条款引用
        if not anchored:
            # 无锚点号:两道召回安全的精度闸,滤掉与条款号同形的量值/表图引用。
            lead = prefix.rstrip()
            if lead and lead[-1] in _FIGURE_LEAD:
                continue  # 表5.3 / 图5.3 / 式5.3
            if _UNIT_AFTER_RE.match(text[m.end():]):
                continue  # 6.0m / 1.5h / 5.3%
        rtype = _classify(prefix)
        key = (to, rtype)
        if key not in seen:
            seen.add(key)
            found.append({"to": to, "type": rtype})
    return found


def build_referenced_by(nodes: list) -> None:
    """全量扫描 references,原地回填每个节点 ``referenced_by``(仅本规范内边)。

    入参为带 ``node_path`` / ``references`` / ``referenced_by`` 属性的节点对象
    (建树期 ``tree_builder._BuildNode``;鸭子类型,不强依赖具体类)。``references`` 元素仍是
    ``{to,type}`` dict。
    """
    paths = {c.node_path for c in nodes}
    reverse: dict[str, list[str]] = {}
    for c in nodes:
        src = c.node_path
        for ref in c.references:
            tgt = ref["to"]
            if ref["type"] == "cross_standard" or tgt not in paths:
                continue
            reverse.setdefault(tgt, [])
            if src not in reverse[tgt]:
                reverse[tgt].append(src)
    for c in nodes:
        c.referenced_by = reverse.get(c.node_path, [])


def annotate_references(nodes: list) -> None:
    """原地富化:为每个节点(带 ``content`` / ``node_path`` / ``references`` / ``referenced_by``
    属性,见 ``tree_builder._BuildNode``)写 ``references``(分型)+ ``referenced_by``(反向)。

    只产 typed ``references``/``referenced_by``;检索期扩展用的扁平 ``references_to``
    由 ``retrieval/indexer._expandable_refs`` 在建索引时从 typed 边派生(strong/cross_standard
    才入),不在本层落字段。
    """
    for c in nodes:
        c.references = extract_references(c.content, c.node_path)
    build_referenced_by(nodes)
