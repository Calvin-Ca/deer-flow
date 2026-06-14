"""TreeSplitter —— 通用标题层级树切分（占位·未实装）。

预留「不依赖原生目录、纯靠 MinerU 标题层级(text_level)建树」插槽：用于**有标题但无目录页**
的文档（TocSplitter 的 heading_fallback 是其雏形，此处拟独立成正式策略）。本轮只立骨架，
``split`` 抛 NotImplementedError；实装时产 ``list[Chunk]`` 树，``register`` 进 factory 即并入。
"""
from __future__ import annotations

from core.document import Document
from core.profile import ParseProfile
from splitter.base import Splitter, SplitResult


class TreeSplitter(Splitter):
    """通用标题层级树切分（占位·未实装）。"""

    name = "tree"

    def split(self, document: Document, *, profile: ParseProfile) -> SplitResult:
        raise NotImplementedError(
            "TreeSplitter 未实装（占位）。当前仅 toc 可用；实装时按 text_level 标题层级建 Chunk 树。"
        )
