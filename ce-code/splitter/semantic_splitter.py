"""SemanticSplitter —— 语义切分（占位·未实装）。

预留「按语义边界切块」插槽：用 embedding 相邻相似度 / 主题漂移找切点，适配**无清晰原生目录**
的文档（散文型规范、说明书）。本轮只立骨架，``split`` 抛 NotImplementedError；实装时产
``list[Chunk]``（可为 ``parent_id=None`` 的扁平块），``register`` 进 factory 即并入，下游不动。
"""
from __future__ import annotations

from core.document import Document
from splitter.base import Splitter, SplitResult


class SemanticSplitter(Splitter):
    """语义切分（占位·未实装）。"""

    name = "semantic"

    def split(self, document: Document, *, max_depth: int | None = None,
              subsplit: str = "none") -> SplitResult:
        raise NotImplementedError(
            "SemanticSplitter 未实装（占位）。当前仅 toc 可用；实装时按语义边界产 list[Chunk]。"
        )
