"""keyword 表征 —— 关键词/术语抽取（占位·未实装）。

预留「从节点抽领域关键词/术语作精确召回辅助通道」插槽（清单编码、构件名、工艺术语）。本轮只立
骨架，``build`` 抛 NotImplementedError；实装时产 ``ChunkFeature(kind="keyword", data={"terms":[...]})``，
``register`` 进 pipeline 即并入，profile.features 加 "keyword" 启用。
"""
from __future__ import annotations

from ir.chunk import Chunk
from ir.feature import ChunkFeature
from feature.base import Feature


class KeywordFeature(Feature):
    """关键词表征（占位·未实装）。"""

    kind = "keyword"

    def build(self, chunk: Chunk) -> ChunkFeature:
        raise NotImplementedError(
            "KeywordFeature 未实装（占位）。默认 profile.features 不含 keyword；实装后再启用。"
        )
