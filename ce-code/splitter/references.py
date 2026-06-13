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

# 本规范内条款号:第?5.2.1条/款/项(2~4 段,至少两段以排除年份/页码)
_NUM_RE = re.compile(r"第?\s*(\d+\.\d+(?:\.\d+){0,2})\s*(?:条|款|项|节)?")

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
        to = m.group(1)
        if to == self_path:
            continue
        rtype = _classify(text[max(0, m.start() - 10): m.start()])
        key = (to, rtype)
        if key not in seen:
            seen.add(key)
            found.append({"to": to, "type": rtype})
    return found


def build_referenced_by(clauses: list[dict]) -> None:
    """全量扫描 references,原地回填每条 ``referenced_by``(仅本规范内边)。"""
    paths = {c["clause_path"] for c in clauses}
    reverse: dict[str, list[str]] = {}
    for c in clauses:
        src = c["clause_path"]
        for ref in c.get("references", []):
            tgt = ref["to"]
            if ref["type"] == "cross_standard" or tgt not in paths:
                continue
            reverse.setdefault(tgt, [])
            if src not in reverse[tgt]:
                reverse[tgt].append(src)
    for c in clauses:
        c["referenced_by"] = reverse.get(c["clause_path"], [])


def annotate_references(clauses: list[dict]) -> None:
    """原地富化:为每条写 ``references``(分型)+ ``referenced_by``(反向)。

    保留旧 ``references_to`` 不动(由 to_v1_compat 在落库时统一桥接),便于对照。
    """
    for c in clauses:
        c["references"] = extract_references(c.get("content", ""), c.get("clause_path", ""))
    build_referenced_by(clauses)
