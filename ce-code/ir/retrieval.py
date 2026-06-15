"""RetrievedChunk IR —— 单条检索命中（retrieval 产 → context/service）。

替代旧 engine 直接透传的「行 dict」为显式 ``@dataclass``。承载命中节点的索引行字段
（node_path / content / page / references_to …）+ 召回来源 + 各阶段评分。

**对外契约（不可破坏）**：``to_response()`` 吐**逐字等于旧 /search 返回 clauses[] 的字段名**
（``node_path`` / ``parent_id`` / ``granularity`` / ``standard_id`` / ``content`` / ``node_level`` /
``page`` / ``references_to`` / ``has_tables`` / ``has_images`` / ``_source`` / ``_*_score``）——
``ce-services`` 是活的 HTTP 客户端，字段名不得改。``from_row`` 与之互逆。

> 内部数值逻辑（RRF 合并 / 引用扩展 / rerank）仍在「行 dict」层做（retrieval/rrf.py，逐字
> 保持旧 engine 行为）；本 IR 只在**检索层出口**承接最终排序结果，给 service / 类型化消费方用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 旧 engine 在行 dict 上挂的评分键 → RetrievedChunk.scores 的内部键。
_SCORE_KEYS = {
    "_bm25_score": "bm25", "_vector_score": "vector",
    "_rrf_score": "rrf", "_rerank_score": "rerank",
}


@dataclass
class RetrievedChunk:
    """一条检索命中（索引行字段 + 来源 + 评分）。"""

    node_path: str
    standard_id: str = ""
    parent_id: str = ""               # 行格式用 "" 表示无父（非 None），契约保持
    granularity: str = ""
    content: str = ""
    node_level: int = 0               # 索引行字段名（= Chunk.level），契约保持不改名
    page: int = 0
    references_to: list[str] = field(default_factory=list)
    has_tables: bool = False
    has_images: bool = False
    source: str = ""                  # _source：bm25 / vector / both / ref_expand
    scores: dict = field(default_factory=dict)  # {bm25,vector,rrf,rerank}（按需有）

    @classmethod
    def from_row(cls, row: dict) -> "RetrievedChunk":
        """从检索行 dict（index 行 / RRF 合并结果，含 _source/_*_score）构造。"""
        refs = row.get("references_to", [])
        if isinstance(refs, str):
            import json
            refs = json.loads(refs or "[]")
        scores = {name: row[key] for key, name in _SCORE_KEYS.items()
                  if row.get(key) is not None}
        return cls(
            node_path=row.get("node_path", ""),
            standard_id=row.get("standard_id", ""),
            parent_id=row.get("parent_id", "") or "",
            granularity=row.get("granularity", ""),
            content=row.get("content", ""),
            node_level=int(row.get("node_level", 0)),
            page=int(row.get("page", 0)),
            references_to=list(refs or []),
            has_tables=bool(row.get("has_tables", False)),
            has_images=bool(row.get("has_images", False)),
            source=row.get("_source", ""),
            scores=scores,
        )

    def to_response(self) -> dict:
        """旧契约字段名 dict（/search 返回 clauses[] 的一条；/expand expanded_clauses 一条）。"""
        out: dict = {
            "node_path": self.node_path,
            "parent_id": self.parent_id,
            "granularity": self.granularity,
            "standard_id": self.standard_id,
            "content": self.content,
            "node_level": self.node_level,
            "page": self.page,
            "references_to": self.references_to,
            "has_tables": self.has_tables,
            "has_images": self.has_images,
            "_source": self.source,
        }
        for key, name in _SCORE_KEYS.items():
            if name in self.scores:
                out[key] = self.scores[name]
        return out
