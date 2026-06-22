"""索引层（index/）—— 阶段 3：Chunk 树 → 选粒度视图 → BM25 + 向量 + 元数据索引。

承旧 ``retrieval/indexer.py`` + ``core/view.py``，拆为：
  manager        粒度视图 view + 行准备 chunk_to_row + 编排 build_index（中枢）。
  bm25_index     rank-bm25 倒排（消费 sparse 表征）。
  vector_index   Milvus 向量（消费 dense 表征，索引期统一嵌入）。
  metadata_index metadata.json 快照（BM25/clause 直取路径共用）。
  graph_index    图索引（占位·面向 Phase C 造价 KG）。

按 ``{standard}/{profile}`` 隔离落 ``data/vector_store/``；collection 名由 config.collection_name 推断
（与 service/检索共享，零改动对齐）。
"""
from __future__ import annotations

from index.manager import build_index, chunk_to_row, view

__all__ = ["view", "chunk_to_row", "build_index"]
