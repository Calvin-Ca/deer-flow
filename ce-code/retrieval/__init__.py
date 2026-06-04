"""retrieval —— 建筑规范检索引擎（纯检索，无生成/无编排）。

只对底层知识库提供检索方法；生成（问答）与合规编排在 ``service/`` 层、import 本包。
被检索服务 / 合规服务 / POC 脚本共用，是"一个知识服务，N 个 skill"的共享内核。

常用入口：
    from retrieval.config import DEFAULTS, STANDARD_ALIASES, resolve_store_dir, collection_name
    from retrieval.engine import search, expand_references, get_clause
"""
from __future__ import annotations

from . import config, engine
from .engine import get_clause, search

__all__ = ["config", "engine", "search", "get_clause"]
