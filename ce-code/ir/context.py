"""KnowledgeContext IR —— 一次检索的完整结果（retrieval/service 产 → 调用方）。

= 旧 ``/search`` 返回体的类型化承载：查询回显 + 命中列表（RetrievedChunk）+ 可观测 meta
（各阶段命中数 / 耗时）。取数客户端（backend cost_workflow / MCP 消费方）拿它拼提示词 / 做编排。

**对外契约**：``to_response(...)`` 吐逐字等于旧 ``/search`` 响应的结构
（``query`` / ``standard`` / ``retrieved_clauses_count`` / ``clauses`` / ``meta``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ir.retrieval import RetrievedChunk


@dataclass
class KnowledgeContext:
    """一次检索的结果集。

    字段：
        query    查询文本（回显）。
        standard 命中的规范化 store 名。
        results  命中条款（已排序的 RetrievedChunk 列表）。
        meta     可观测信息（bm25_hits/vector_hits/merged/expanded/final、耗时等）。
    """

    query: str = ""
    standard: str = ""
    results: list[RetrievedChunk] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_response(self) -> dict:
        """旧 /search 响应结构（字段名逐字保持）。"""
        return {
            "query": self.query,
            "standard": self.standard,
            "retrieved_clauses_count": len(self.results),
            "clauses": [r.to_response() for r in self.results],
            "meta": self.meta,
        }
