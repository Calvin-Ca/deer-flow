"""parser —— ① 解析层：原始文档 → Document IR（结构层的上游，可插拔多解析模型）。

  base / factory  Parser 基类（统一契约 ``adapt``）+ 工厂（按 profile.parser_strategy 选工具）。
  mineru          MinerU 工具（单文件·一个文件=一个工具）：纯逻辑适配器 FormatAdapter + 门面
                  MineruParser（``adapt`` 阶段1 / ``run`` 阶段0 引擎 / ``run_cli`` 阶段0 CLI 薄壳）。
                    · 阶段0 ``run``：PDF → content_list.json（调远程 MinerU API，产物缓存
                      ``data/parsed/<std>/``，不可变·只跑一次）。
                    · 阶段1 ``adapt``：content_list.json → Document（仅格式归一，出纯版面块）。
  unstructured    UnstructuredParser（占位·未实装，仅 ``adapt``）。

  注：目录打标（catalog 标签 + 目录条目表）已下沉到切分层 ``splitter.toc_splitter.CatalogLabeler``
  ——解析层只做格式归一、不碰结构语义。

取工具走工厂：``parser.factory.select(profile.parser_strategy)``（不在包级 re-export ``select``——
「取 parser」是工厂的职责，调用方显式走 ``factory``）。编排见包级入口 ``__main__.py``
（``python -m parser mineru``）。解析工具产出的 Document 喂给切分层 ``splitter``（见
根 ``build.py``）。
"""
from __future__ import annotations

from parser import factory  # import 即触发工具注册，并暴露 parser.factory
from parser.base import Parser
from parser.mineru import FormatAdapter, MineruParser

__all__ = ["Parser", "MineruParser", "FormatAdapter", "factory"]
