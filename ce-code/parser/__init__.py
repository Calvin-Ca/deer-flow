"""parser —— ① 解析层：原始文档 → Document IR（结构层的上游，可插拔多解析模型）。

  base / factory  Parser 基类 + 工厂（按 profile.parser_strategy 选解析模型）。
  mineru          MineruParser（实装）：content_list.json → Document（包 format_adapter）。
  unstructured    UnstructuredParser（占位·未实装）。
  format_adapter  MinerU v1 content_list → 统一块（page 归一、HTML 表格展开、text_level 透传、
                  block_idx 溯源）；MineruParser 复用其逻辑，亦可独立单测。
  mineru_client / pdf_parser / split_parse   阶段 0 实跑 PDF→content_list.json（远程 API 默认 /
                  ``--local`` 本地 CLI）；产物缓存 ``data/parsed/<std>/``（不可变·只跑一次）。

编排见包级入口 ``__main__.py``（``python -m parser``）。解析模型产出的 Document 喂给切分层
``splitter``（见 ``service/build_service.py``）。
"""
from __future__ import annotations

from parser.base import Parser
from parser.factory import DEFAULT_PARSER, create, register
from parser.format_adapter import FormatAdapter
from parser.mineru import MineruParser

__all__ = ["Parser", "create", "register", "DEFAULT_PARSER", "MineruParser", "FormatAdapter"]
