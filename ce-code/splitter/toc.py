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

────────────────────────────────────────────────────────────────────────────
一个原始块（MinerU content_list 里的一项）如何变成节点字段 —— **三道判断，各管一件事**
（这段是给将来的自己看的完整心智模型，别再重新推一遍）：

  ┌ 是不是标题？  看 MinerU 给的 ``text_level``（FormatAdapter 原样透传，本流程**只读不
  │              重判**：有此键=标题、没有=正文）。**只用它回答是非，不取其层级数值**——
  │              真正层级另由 node_path 号段数算（MinerU 标的"几级"不可靠）。
  │
  ├ 是不是目录页？看 ``catalog``（CatalogLabeler 盖）。判据 = 「**行尾带页码**」
  │              （_CATALOG_TAIL_RE：`标题……23` / `标题(23)` / `标题　　23`）**且扎堆出现**
  │              （整列过半带页码 _is_catalog_list，或连续 ≥4 行像目录行 _mark_toc）——
  │              单独一行带数字不算，避免误伤正文。命中 → ``catalog="toc"``。
  │              ⚠ 正文里「复述目录的标题」（如正文中的 "1总则"）**不算目录页**：它没有
  │              行尾页码、不扎堆，故 catalog≠"toc"；它会被拿去和目录条目表比对，命中后打
  │              ``catalog=<条目标题>``、source=``toc_match`` —— 这是「归属某节」，不是
  │              「目录页那几页」。建树时 catalog=="toc" 跳过、catalog=<标题> 留下接地。
  │
  └ 编号是多少？  TreeBuilder.classify_heading 抠 ``node_path``（只对"是标题且非目录页"
                 的块、以及目录条目标题调用）。四级判定详见该函数 docstring。

block → node 的主干（TreeBuilder._absorb_body，按文档序逐块）：
  ① catalog=="toc" → 跳过（真目录页不进树，structure.json 已全量留档 + 溯源）。
  ② 是标题（text_level 非 None）→ 抠编号 classify_heading：
       · 抠出 None（数字后紧跟 节/条/款/项 = 交叉引用片段）→ 当内容块处理，不建节点；
       · 抠出 path → 查 by_path（已预放全部目录条目骨架）：
           查到 = **接地**（补 provenance/page，标题保留目录版，空骨架由此长出溯源）；
           查不到 = **新建节点**（多为目录没列的"条" 5.3.4）。
         随后把游标 ``cur`` 指向它。
  ③ 非标题块 → 累积进当前 ``cur``（content / tables / images + provenance）；
     cur 为空（首个标题之前的散块）则丢弃。
  即「**碰到标题切一次 cur，下一个标题出现前的普通块都算这个标题的正文**」。
────────────────────────────────────────────────────────────────────────────
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
