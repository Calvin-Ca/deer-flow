"""graph 表征 —— 图结构投影（占位·未实装）。

预留「把节点的引用图邻接 / KG 关系投影成可检索/可遍历结构」插槽。**注意**：规范内的引用边
（references / referenced_by）已由 TreeBuilder 作固有事实算定在 Chunk 上，检索期轻量一跳扩展
当前在 hybrid_retriever 直接消费（无需本表征）；本占位面向**未来真 KG**（构件→清单→定额→工料机
多跳，Phase C），实装时产图嵌入 / 子图特征，配合 index/graph_index + retrieval/graph_retriever。

本轮只立骨架，``build`` 抛 NotImplementedError。
"""
from __future__ import annotations

from ingest.ir.chunk import Chunk
from ir.feature import ChunkFeature
from feature.base import Feature


class GraphFeature(Feature):
    """图结构表征（占位·未实装；面向未来真 KG）。"""

    kind = "graph"

    def build(self, chunk: Chunk) -> ChunkFeature:
        raise NotImplementedError(
            "GraphFeature 未实装（占位）。规范内引用扩展走 hybrid_retriever；本表征面向未来真 KG。"
        )
