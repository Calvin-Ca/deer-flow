"""混合检索器 —— BM25 + 向量 + RRF 合并 + 引用图扩展 + rerank（承旧 engine.search）。

把两路单通道（``Bm25Retriever`` / ``DenseRetriever``）的「索引行 dict」结果在行层做 RRF 合并
（rrf.merge_results）、沿 strong/cross_standard 边一跳扩展（rrf.expand_references）、再 cross-encoder
精排（rerank，不可用时 fallback RRF 顺序），最后统一转 ``RetrievedChunk``。**数值流程与旧 engine.search
逐字一致**（bm25_top_k = vector_top_k = top_k*2），召回不变。

引用扩展 + rerank **收口于此**：规范内引用图是 Chunk 固有事实（references_to），轻量一跳即够，
无需独立图检索器（graph_retriever 留给未来真 KG 多跳）。
"""
from __future__ import annotations

from pathlib import Path

from core.query import RetrievalQuery
from core.retrieval import RetrievedChunk
from index import metadata_index
from retrieval.base import Retriever
from retrieval.bm25_retriever import Bm25Retriever
from retrieval.dense_retriever import DenseRetriever
from retrieval.rrf import expand_references, merge_results


def rerank(query: str, results: list[dict], top_k: int) -> list[dict]:
    """cross-encoder 精排（承旧 engine.rerank）；不可用时 fallback 到 RRF 顺序。"""
    from config import RERANK_MODEL
    try:
        from FlagEmbedding import FlagReranker
        reranker = FlagReranker(RERANK_MODEL, use_fp16=True)
        pairs = [[query, r.get("content", "")] for r in results]
        scores = reranker.compute_score(pairs, normalize=True)
        for item, score in zip(results, scores):
            item["_rerank_score"] = float(score)
        results_sorted = sorted(results, key=lambda x: x.get("_rerank_score", 0), reverse=True)
    except Exception as e:
        print(f"[retrieval] Rerank 不可用（{e}），使用 RRF 排序")
        results_sorted = results
    return results_sorted[:top_k]


class HybridRetriever(Retriever):
    """混合检索（BM25 + 向量 + RRF + 引用扩展 + rerank）。"""

    name = "hybrid"

    def __init__(self, store_dir: Path, collection_name: str, *,
                 milvus_host: str = "localhost", milvus_port: int = 19530,
                 embed_url: str = "http://localhost:8097", embed_model_id: str = "/model") -> None:
        self.store_dir = Path(store_dir)
        self.bm25 = Bm25Retriever(self.store_dir)
        self.dense = DenseRetriever(
            collection_name, milvus_host=milvus_host, milvus_port=milvus_port,
            embed_url=embed_url, embed_model_id=embed_model_id)

    def retrieve(self, query: RetrievalQuery, stats: dict | None = None) -> list[RetrievedChunk]:
        """混合检索主流程（与旧 engine.search 逐字一致）。

        参数：
            query (RetrievalQuery): 查询（top_k / text / skip_rerank）。
            stats (dict | None): 传入则回填各阶段命中数（bm25_hits/vector_hits/merged/expanded/final）。
        返回：
            list[RetrievedChunk]: 已排序命中。
        """
        top_k = query.top_k
        fanout = top_k * 2  # bm25_top_k = vector_top_k = top_k*2（旧 server/eval 口径）

        bm25_rows = self.bm25.search_rows(query.text, fanout)
        vector_rows = self.dense.search_rows(query.text, fanout)

        merged = merge_results(bm25_rows, vector_rows)
        metadata = self.bm25._metadata  # bm25.search_rows 已惰性加载 metadata
        expanded = expand_references(merged, metadata)

        if query.skip_rerank:
            final = expanded[:top_k]
        else:
            final = rerank(query.text, expanded, top_k)

        if stats is not None:
            stats.update(
                bm25_hits=len(bm25_rows), vector_hits=len(vector_rows),
                merged=len(merged), expanded=len(expanded), final=len(final),
            )
        return [RetrievedChunk.from_row(r) for r in final]
