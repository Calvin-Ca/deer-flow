"""context_aug 表征 —— 拼祖先链的语境增强文本（small-to-big 入口，免费）。

PRD §3.1：context_aug 载体=向量 + BM25，作用=拼祖先链的语境增强文本（small-to-big 入口）。叶子
节点单看正文常缺语境（"应符合本表规定"脱离所在章节难判），故在其文本前拼上祖先标题链 → 既利
语义召回，也作 small-to-big「小粒度匹配、大粒度返回」的索引侧入口。

祖先链 ``ancestor_titles`` 已由建树器 TreeBuilder 作「固有事实」一次算定，本表征**直接复用、不
重算**。向量同 dense，由索引期计算。
"""
from __future__ import annotations

from ir.chunk import Chunk
from ir.feature import ChunkFeature
from feature.base import Feature

_PATH_SEP = " / "   # 祖先标题之间
_BODY_SEP = " ‖ "   # 祖先链与本条正文之间（PRD §3.1 示例用此分隔符）


class ContextAugFeature(Feature):
    """context_aug 表征：祖先标题链 ‖ 本条正文（复用 TreeBuilder 算定的 ancestor_titles）。"""

    kind = "context_aug"

    def build(self, chunk: Chunk) -> ChunkFeature:
        ancestors = [t for t in (chunk.ancestor_titles or []) if t]
        body = chunk.content.strip() or chunk.title.strip()
        prefix = _PATH_SEP.join(ancestors)
        text = f"{prefix}{_BODY_SEP}{body}" if prefix else body
        return ChunkFeature(kind=self.kind, text=text, meta={"ancestors": len(ancestors)})
