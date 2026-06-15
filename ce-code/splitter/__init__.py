"""切分层 —— 阶段 1 结构层「切分轴」（PRD §3.1 / §3.2）：Document(IR) → Chunk 树。

把「文档怎么切成检索单元结构」做成可插拔策略（``factory`` 注册表，按 ``profile.structure_strategy``
选）。当前/默认成员 ``toc``（``TocSplitter``，基于原生目录的多层级切分，核心设计原则 1）；
``semantic`` / ``tree`` 为占位扩展点。

内部件（随 TOC 法内聚，可独立单测）：``catalog_labeler``（目录打标）/ ``tree_builder``（建树 +
固有事实）/ ``references``（引用图分型）。
"""
from __future__ import annotations

from splitter import factory  # import 即触发切法注册，并暴露 splitter.factory
from splitter.base import Splitter, SplitResult

# 取切法走工厂：splitter.factory.select(profile.structure_strategy)（不在包级 re-export select——
# 「选切法」是工厂的职责，调用方显式走 factory，与 parser 层一致）。
__all__ = ["Splitter", "SplitResult", "factory"]
