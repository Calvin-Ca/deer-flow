"""图检索器 —— KG 多跳遍历召回（占位·未实装，面向 Phase C 造价 KG）。

预留「构件→清单→定额→工料机」多跳关系召回插槽（配合 index/graph_index）。**注意**：规范内
引用图的轻量一跳扩展已在 ``hybrid_retriever`` 直接做（references_to 是 Chunk 固有事实），无需本
检索器；本占位面向**真 KG**（P0 PG 关联表 / P1 Neo4j，DEV §3.3）。本轮只立骨架。
"""
from __future__ import annotations

from core.query import RetrievalQuery
from core.retrieval import RetrievedChunk
from retrieval.base import Retriever


class GraphRetriever(Retriever):
    """KG 多跳检索（占位·未实装）。"""

    name = "graph"

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]:
        raise NotImplementedError(
            "GraphRetriever 未实装（占位）。规范内引用扩展走 hybrid；本占位面向 Phase C 造价 KG 多跳。"
        )
