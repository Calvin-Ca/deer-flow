"""BM25 检索器 —— 条文号 / 清单编码 / 术语精确召回（承旧 engine.bm25_search）。

读 ``index.bm25_index`` 的 bm25.pkl + ``index.metadata_index`` 的 metadata.json，对查询做字符级
分词（``utils.tokenizer.tokenize``，**与建索引期同一套**）后 BM25 打分取 top_k。返回「索引行 dict」
（带 ``_bm25_score`` / ``_source``）供 hybrid RRF 合并；``retrieve`` 出口转 RetrievedChunk。
"""
from __future__ import annotations

from pathlib import Path

from ir.query import RetrievalQuery
from ir.retrieval import RetrievedChunk
from index import bm25_index, metadata_index
from retrieval.base import Retriever
from utils.tokenizer import tokenize


def bm25_search(query: str, bm25, node_paths: list[str], metadata: list[dict], top_k: int) -> list[dict]:
    """BM25 打分取 top_k，返回索引行 dict（承旧 engine，逐字保持）。"""
    tokens = tokenize(query)
    scores = bm25.get_scores(tokens)
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
    results = []
    for idx, score in ranked:
        if score == 0:
            break
        item = dict(metadata[idx])
        item["_bm25_score"] = float(score)
        item["_source"] = "bm25"
        results.append(item)
    return results


class Bm25Retriever(Retriever):
    """BM25 关键词召回（单路）。"""

    name = "bm25"

    def __init__(self, store_dir: Path) -> None:
        self.store_dir = Path(store_dir)
        self._bm25 = None
        self._node_paths: list[str] = []
        self._metadata: list[dict] = []

    def load(self) -> None:
        """惰性加载 bm25.pkl + metadata.json（首次检索时）。"""
        if self._bm25 is None:
            self._bm25, self._node_paths = bm25_index.load(self.store_dir)
            self._metadata = metadata_index.load(self.store_dir)

    @property
    def metadata(self) -> list[dict]:
        """已加载的 metadata 行（公开访问器，惰性加载）—— 供 hybrid 引用扩展复用，
        避免外部碰私有 ``_metadata`` 与惰性加载副作用。"""
        self.load()
        return self._metadata

    def search_rows(self, text: str, top_k: int) -> list[dict]:
        """返回索引行 dict（供 hybrid RRF 合并）。"""
        self.load()
        return bm25_search(text, self._bm25, self._node_paths, self._metadata, top_k)

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]:
        rows = self.search_rows(query.text, query.top_k)
        return [RetrievedChunk.from_row(r) for r in rows]
