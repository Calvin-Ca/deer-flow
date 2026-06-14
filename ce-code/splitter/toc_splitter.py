"""TocSplitter —— 基于 PDF 原生目录(TOC)的多层级切分（核心设计原则 1，当前默认实现）。

承旧 ``splitter/toc.py``。把 TOC 法的两件内部实现 —— 目录打标器 ``CatalogLabeler``
（catalog_labeler.py）与建树器 ``TreeBuilder``（tree_builder.py）—— 收口成切分策略：

  ① CatalogLabeler.annotate：给每块打 ``catalog`` 标签 + 解析出**有序目录条目表**（骨架真值）；
  ② TreeBuilder.apply：以目录条目为骨架建**保留 parent/child 的语义树**——条目物化为骨架节点
     （根治父链断裂），正文按条文号号段 / 目录归属挂载，连边后算定固有事实（祖先链 / 引用图分型
     + 反向边）；无目录页时退化为复用 MinerU 标题层级 best-effort。

IR 适配（本轮重构）：入参由旧「list[block dict]」改为 ``Document``——内部 ``document.block_dicts()``
还原成 CatalogLabeler/TreeBuilder 期望的 block dict（复用其成熟的 dict 管道，零改动），出参由旧
「list[node dict]」经 ``_node_to_chunk`` 映射为 ``list[Chunk]``（node_type→chunk_type、node_level→level、
reprs→features[空]，其余同名）。中间产物 annotated（带 catalog/catalog_source）作 debug_blocks
落 structure.json。

> block→node 的主干心智模型（碰到标题切一次游标、下个标题前的普通块都算当前标题正文；目录条目
> 物化骨架、更细标题并入所属节）见 ``tree_builder.py`` 的 TreeBuilder 文档，此处不重述。
"""
from __future__ import annotations

from core.chunk import Chunk
from core.document import Document
from core.profile import ParseProfile
from splitter.base import Splitter, SplitResult
from splitter.catalog_labeler import CatalogLabeler
from splitter.tree_builder import TreeBuilder


def _node_to_chunk(node: dict) -> Chunk:
    """建树器产出的 node dict → Chunk IR（字段改名 + 丢空 reprs）。

    node_type→chunk_type、node_level→level、reprs→features（切分阶段为空，由表征层挂）；
    references / provenance 由 Chunk.from_dict 内部转成 Reference / Provenance。
    """
    d = dict(node)
    d["chunk_type"] = d.pop("node_type", "leaf")
    d["level"] = d.pop("node_level", 0)
    d.pop("reprs", None)  # 切分阶段无表征；features 由 feature 层 enrich 挂
    return Chunk.from_dict(d)


class TocSplitter(Splitter):
    """以 PDF 原生目录为骨架的多层级切分（CatalogLabeler + TreeBuilder）。"""

    name = "toc"

    def split(self, document: Document, *, profile: ParseProfile) -> SplitResult:
        """目录打标 → 建树 → Chunk 树（含 parent/child + 祖先链 + 引用图）。

        参数 / 返回：见基类 Splitter.split。debug_blocks = 目录打标后的扁平块（structure.json）。
        """
        blocks = document.block_dicts()
        labeler = CatalogLabeler(document.standard_id)
        annotated = labeler.annotate(blocks)
        labeler.print_stats(annotated)  # 观测：catalog_source 来源分解（方案 5 可见）

        nodes = TreeBuilder(profile).apply(
            annotated,
            entries=labeler.entries,
            source_file=document.source_file,
        )
        chunks = [_node_to_chunk(n) for n in nodes]
        return SplitResult(chunks=chunks, debug_blocks=annotated)
