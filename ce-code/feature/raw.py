"""raw 表征 —— 节点字面原文（返回用，免费）。

PRD §3.1：raw 载体=存储，作用=返回展示用的字面原文，成本=免费。不进语义/稀疏召回，
只承载「命中后给用户看什么」。
"""
from __future__ import annotations

from core.chunk import Chunk
from core.feature import ChunkFeature
from feature.base import Feature


class RawFeature(Feature):
    """raw 表征：节点自身正文（不含子节点、不拼祖先），供命中后展示。"""

    kind = "raw"

    def build(self, chunk: Chunk) -> ChunkFeature:
        return ChunkFeature(kind=self.kind, text=chunk.content)
