"""TocSplitter —— 基于 PDF 原生目录(TOC)的多层级切分（核心设计原则 1，当前默认实现）。

把 TOC 法的两件实现 —— 目录打标器 ``CatalogLabeler``（splitter/catalog_labeler.py）与建树器
``TreeBuilder``（splitter/tree_builder.py）—— 收口成「切分轴」的一个策略（随本切法
内聚在 splitter/ 包内）：

  ① CatalogLabeler.annotate：给每块打 ``catalog`` 标签 + 解析出**有序目录条目表**（骨架真值）；
  ② TreeBuilder.apply：以目录条目为骨架建**保留 parent/child 的语义树**——条目物化为骨架
     节点（根治父链断裂），正文按条文号号段 / 目录归属挂载，连边后算定固有事实（祖先链 /
     引用图分型 + 反向边）；无目录页时退化为复用 MinerU 标题层级 best-effort。

中间产物 ``annotated``（带 catalog/catalog_source 的扁平块）作 ``SplitResult.debug_blocks``
落 structure.json 供调试；切分统计（catalog_source 分解）由 CatalogLabeler.print_stats 打印。
"""
from __future__ import annotations

from splitter.base import Splitter, SplitResult
from splitter.catalog_labeler import CatalogLabeler
from splitter.tree_builder import TreeBuilder


class TocSplitter(Splitter):
    """以 PDF 原生目录为骨架的多层级切分（CatalogLabeler + TreeBuilder）。"""

    name = "toc"

    def split(
        self,
        elements,
        *,
        standard_id,
        profile,
        source_file="",
    ) -> SplitResult:
        """目录打标 → 建树 → 节点树（含 parent/child + 祖先链 + 引用图）。

        参数 / 返回：见基类 Splitter.split。debug_blocks = 目录打标后的扁平块（structure.json）。
        """
        labeler = CatalogLabeler(standard_id)
        annotated = labeler.annotate(elements)
        labeler.print_stats(annotated)  # 观测：catalog_source 来源分解（方案 5 可见）

        nodes = TreeBuilder(profile).apply(
            annotated,
            entries=labeler.entries,
            source_file=source_file,
        )
        return SplitResult(nodes=nodes, debug_blocks=annotated)
