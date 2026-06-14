"""检索层（retrieval/）—— 多路召回 + 混合编排（RetrievalQuery → KnowledgeContext）。

承旧 ``retrieval/engine.py`` 的检索数值逻辑，拆为可插拔多策略：
  base            Retriever 基类。
  bm25_retriever  BM25 关键词召回（单路）。
  dense_retriever 向量语义召回（单路）。
  hybrid_retriever BM25 + 向量 + RRF + 引用扩展 + rerank（默认主力，逐字保持旧召回）。
  graph_retriever KG 多跳（占位·面向 Phase C）。
  rrf             RRF 合并 + 引用扩展（行 dict 层纯函数）。
  service         RetrievalService —— 对外统一检索入口（search / expand / get_clause）。

rerank + 索引（rerank 模型 / Milvus）只在检索栈加载一份（唯一 owner）。
"""
from __future__ import annotations

from retrieval.base import Retriever
from retrieval.bm25_retriever import Bm25Retriever
from retrieval.dense_retriever import DenseRetriever
from retrieval.graph_retriever import GraphRetriever
from retrieval.hybrid_retriever import HybridRetriever
from retrieval.service import RetrievalService

__all__ = [
    "Retriever", "Bm25Retriever", "DenseRetriever", "HybridRetriever",
    "GraphRetriever", "RetrievalService",
]
