"""utils —— 跨层小工具（无业务语义、无外部服务依赖）。

  tokenizer    字符级中文分词（BM25 语料/查询共用）。
  text_cleaner 轻量文本归一（占位，按需扩展）。
  logger       统一日志配置（服务/编排共用）。
"""
from __future__ import annotations

from utils.tokenizer import tokenize

__all__ = ["tokenize"]
