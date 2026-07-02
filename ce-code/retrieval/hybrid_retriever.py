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

import requests

from ir.query import RetrievalQuery
from ir.retrieval import RetrievedChunk
from index import metadata_index
from retrieval.base import Retriever
from retrieval.bm25_retriever import Bm25Retriever
from retrieval.dense_retriever import DenseRetriever
from retrieval.rrf import expand_references, merge_results


# cross-encoder 精排已拆为独立 GPU 服务（service.rerank_api，config.RERANK_URL，默认 :8098）——
# 把唯一的本地 GPU 消费者移出知识层，:8100 检索栈即纯 CPU（embedding 早已在 :8097，同款做法）。
# 服务不可达（连不上）→ 缓存「不可用」，后续本进程一律 fallback RRF，不再逐查询空等超时（呼应旧
# _RERANKER_FAILED 语义：一次判定、不反复重试）。单次打分错（如 5xx）不缓存，仅该查询退回 RRF。
_RERANK_UNREACHABLE = False


def rerank(query: str, results: list[dict], top_k: int) -> list[dict]:
    """cross-encoder 精排 —— HTTP 调 rerank 服务；不可用时 fallback 到 RRF 顺序。

    参数：query 查询文本；results RRF 合并+扩展后的行 dict 列表；top_k 截断数。
    返回：按精排分降序（服务可用）或原 RRF 顺序（服务不可达/打分失败）截断后的前 top_k 行。
    """
    global _RERANK_UNREACHABLE
    if _RERANK_UNREACHABLE or not results:
        return results[:top_k]
    from config import RERANK_TIMEOUT, RERANK_URL
    try:
        passages = [r.get("content", "") for r in results]
        resp = requests.post(f"{RERANK_URL}/rerank",
                             json={"query": query, "passages": passages, "normalize": True},
                             timeout=RERANK_TIMEOUT)
        resp.raise_for_status()
        scores = resp.json().get("scores", [])
        for item, score in zip(results, scores):
            item["_rerank_score"] = float(score)
        results_sorted = sorted(results, key=lambda x: x.get("_rerank_score", 0), reverse=True)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"[retrieval] Rerank 服务不可达（{e}），本进程后续统一 fallback RRF 排序")
        _RERANK_UNREACHABLE = True  # 连不上就别每查询空等超时，一次判定
        return results[:top_k]
    except Exception as e:
        print(f"[retrieval] Rerank 打分失败（{e}），本次 fallback RRF 排序")
        return results[:top_k]
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
        expanded = expand_references(merged, self.bm25.metadata)  # 公开访问器（已惰性加载）

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
