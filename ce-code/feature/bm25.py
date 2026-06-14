"""sparse 表征 —— BM25 条文号 / 术语精确召回文本（免费）。

PRD §3.1：sparse 载体=BM25，作用=条文号 / 术语精确召回，成本=免费。把 node_path（保证裸条文号
"5.3.4" 作可检索 token，即便 title 为空）、title、content 拼成词项丰富文本，交索引期 BM25 分词
建倒排。向量化不在此（那是 dense）。

> 文件名 ``bm25``（按消费方 BM25 索引命名），表征 ``kind`` 仍为 ``"sparse"``（语义投影名，与 dense
> 对偶；core.feature.FeatureKind 与 profile.DEFAULT_FEATURES 均用此）。
"""
from __future__ import annotations

from core.chunk import Chunk
from core.feature import ChunkFeature
from feature.base import Feature


class Bm25Feature(Feature):
    """sparse 表征：node_path + title + content 的词项丰富拼接（供 BM25 分词索引）。"""

    kind = "sparse"

    def build(self, chunk: Chunk) -> ChunkFeature:
        parts = [chunk.node_path, chunk.title, chunk.content]
        text = "\n".join(p for p in (s.strip() for s in parts) if p)
        return ChunkFeature(kind=self.kind, text=text)
