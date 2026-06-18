"""切分层 —— 阶段 1 结构层「切分轴」（PRD §3.1 / §3.2）：Document(IR) → Chunk 树。

把「文档怎么切成检索单元结构」做成可插拔策略（``factory`` 注册表，按 ``profile.structure_strategy``
选）。当前/默认成员 ``toc``（``TocSplitter``，基于原生目录的多层级切分，核心设计原则 1）；
``semantic`` / ``tree`` 为占位扩展点。

TOC 法吃**解析层格式归一**的 ``Document``（纯版面块、未打标），本层先目录打标（CatalogLabeler）
再建树 + 算固有事实 + 引用图分型，全部内聚于单一 ``toc_splitter`` 模块（按 §0 目录打标 / §1 引用 /
§2 建树 / §3 切分策略 分段），纯函数仍可独立单测（见 ``tests/test_splitter_pure.py``）。

启动（与 parser 同构·registry 驱动）：包级入口 ``__main__.py`` 遍历注册表，把声明了 ``run_cli``
的切法挂成 ``python -m splitter <切法名>``（如 ``python -m splitter toc --input … --subsplit number``）；
占位切法无 ``run_cli`` → 不出现在 CLI。切分只到结构层（出 chunks.json；catalog_blocks.json 为本层
目录打标快照，随 ``SplitResult.catalog_blocks`` 返回、run_cli 顺带落盘），不碰表征 / 索引（那是
``build.py`` 的事）。``run_cli`` 是切法私有
能力（不进 Splitter ABC，鸭子类型），与 ``parser`` 层的 ``run_cli`` 约定一致。
"""
from __future__ import annotations

from ingest.splitter import factory  # import 即触发切法注册，并暴露 splitter.factory
from ingest.splitter.base import Splitter, SplitResult

# 取切法走工厂：splitter.factory.select(profile.structure_strategy)（不在包级 re-export select——
# 「选切法」是工厂的职责，调用方显式走 factory，与 parser 层一致）。
__all__ = ["Splitter", "SplitResult", "factory"]
