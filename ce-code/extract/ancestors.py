"""波3:祖先标题链。对应 PRD §1/§4 的【P1】chunk 携带祖先链。

叶子款/项向量化时拼上 ``ancestor_titles``(章/节标题)做 contextual / small-to-big,
召回时既给命中款、也能给完整上下文。纯由 clause_path 层级 + 各级标题重建。
"""
from __future__ import annotations


def _heading_text(clause: dict) -> str:
    """章/节条款的标题文本:``clause_path + 首行正文``。

    02 解析时把"5 建筑分类和耐火等级"的 num 存进 clause_path、其余存进 content,
    故章/节标题 = clause_path + content 首行。
    """
    path = clause.get("clause_path", "")
    body = (clause.get("content") or clause.get("title") or "").strip()
    first_line = body.split("\n", 1)[0].strip()[:40]
    return f"{path} {first_line}".strip()


def attach_ancestor_titles(clauses: list[dict]) -> None:
    """原地写每条 ``ancestor_titles``(从章到直接父节,不含自身)。"""
    by_path = {c.get("clause_path", ""): c for c in clauses}
    for c in clauses:
        path = c.get("clause_path", "")
        if not path or path.startswith("附录") or "." not in path:
            c["ancestor_titles"] = []
            continue
        parts = path.split(".")
        titles: list[str] = []
        for i in range(1, len(parts)):
            anc = by_path.get(".".join(parts[:i]))
            if anc is not None:
                titles.append(_heading_text(anc))
        c["ancestor_titles"] = titles
