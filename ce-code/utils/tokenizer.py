"""字符级中文分词 —— BM25 语料与查询**必须用同一套**分词，否则召回错位。

中文无空格，BM25 直接按**字符**切（``list(text)``）：条文号 "1.0.3" / 清单编码 / 术语的
逐字匹配靠它，与旧 ``retrieval/engine.bm25_search`` 和 ``indexer.build_bm25`` 的 ``list(...)``
逐字一致（行为保持）。抽到此处统一 owner，建索引期与检索期共用，杜绝两侧分词漂移。
"""
from __future__ import annotations


def tokenize(text: str) -> list[str]:
    """字符级分词：返回字符列表（空串 → 空列表）。

    参数：
        text (str): 待分词文本（BM25 语料行 / 查询）。
    返回：
        list[str]: 逐字 token 列表。
    """
    return list(text or "")
