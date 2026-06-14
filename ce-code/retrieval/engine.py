"""retrieval 检索引擎 —— 对底层知识库提供检索方法（纯检索，无生成/编排）。

从 ``scripts/05_retrieve.py`` 收敛而来，逻辑逐字保持不变，只把 CLI / 展示 /
评测留在脚本侧。对外暴露的检索方法：

  search(...)             混合检索（BM25 + 向量 + RRF 合并 + 引用扩展 + Rerank）
  bm25_search / vector_search / merge_results / expand_references / rerank
                          各召回原语（供服务端按需单独组合，如 /expand）
  get_clause(...)         按 node_path 直取单条款

依赖：requests（embedding/调用）、pymilvus（向量库）、rank-bm25（已序列化进 pkl）、
可选 FlagEmbedding（rerank，不可用时自动 fallback 到 RRF 顺序）。
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

from .config import RERANK_MODEL

MILVUS_OUTPUT_FIELDS = [
    "node_id", "node_path", "parent_id", "granularity", "standard_id", "content",
    "level", "page", "references_to", "has_tables", "has_images",
]
# 注：is_mandatory 字段已随 T4 从索引移除（强条机制 2026-06-12 废）；parent_id 供 T9
# small-to-big 上探。ref-type 感知的引用扩展与 small-to-big 仍在 T9，本处只读不变。


# ---------------------------------------------------------------------------
# 索引加载
# ---------------------------------------------------------------------------

def load_bm25(store_dir: Path):
    with open(store_dir / "bm25.pkl", "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["node_paths"]


def load_metadata(store_dir: Path) -> list[dict]:
    with open(store_dir / "metadata.json", encoding="utf-8") as f:
        return json.load(f)


def embed_texts(texts: list[str], embed_url: str, model_id: str) -> list[list[float]]:
    import requests
    resp = requests.post(
        f"{embed_url}/v1/embeddings",
        json={"model": model_id, "input": texts},
        timeout=120,
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in data]


def connect_milvus(milvus_host: str, milvus_port: int, collection_name: str):
    from pymilvus import MilvusClient
    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    return client, collection_name


# ---------------------------------------------------------------------------
# 单路召回
# ---------------------------------------------------------------------------

def bm25_search(
    query: str,
    bm25,
    node_paths: list[str],
    metadata: list[dict],
    top_k: int,
) -> list[dict]:
    tokens = list(query)  # 字符级分词（中文无空格）
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


def vector_search(
    query: str,
    client,
    collection_name: str,
    embed_url: str,
    embed_model_id: str,
    top_k: int,
) -> list[dict]:
    query_vec = embed_texts([query], embed_url, embed_model_id)[0]

    search_params = {"metric_type": "COSINE", "params": {"ef": 64}}

    hits = client.search(
        collection_name=collection_name,
        data=[query_vec],
        anns_field="embedding",
        search_params=search_params,
        limit=top_k,
        output_fields=MILVUS_OUTPUT_FIELDS,
    )

    results = []
    for hit in hits[0]:
        item = {f: hit.get(f) for f in MILVUS_OUTPUT_FIELDS}
        refs_raw = item.get("references_to", "[]")
        item["references_to"] = json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
        item["_vector_score"] = hit.get("distance")
        item["_source"] = "vector"
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# RRF 合并去重
# ---------------------------------------------------------------------------

def merge_results(bm25_results: list[dict], vector_results: list[dict]) -> list[dict]:
    """RRF 合并去重。

    去重键用 ``node_id``（节点唯一键），不用 ``node_path``：clause 粒度下两者 1:1
    等价，但 section / paragraph 粒度下 node_path 不再唯一（同号多检索单元），唯有
    node_id 恒唯一——故统一按 node_id 去重，避免换粒度后误合并。
    """
    k = 60  # RRF 常数
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, item in enumerate(bm25_results):
        nid = item["node_id"]
        scores[nid] = scores.get(nid, 0) + 1 / (k + rank + 1)
        items[nid] = item

    for rank, item in enumerate(vector_results):
        nid = item["node_id"]
        scores[nid] = scores.get(nid, 0) + 1 / (k + rank + 1)
        if nid not in items:
            items[nid] = item
        else:
            items[nid]["_source"] = "both"
            items[nid]["_vector_score"] = item.get("_vector_score")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results = []
    for nid, rrf_score in ranked:
        item = dict(items[nid])
        item["_rrf_score"] = rrf_score
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# 引用图扩展
# ---------------------------------------------------------------------------

def expand_references(results: list[dict], metadata: list[dict], max_depth: int = 1) -> list[dict]:
    """沿可扩展引用边（strong / cross_standard）一跳扩展命中集。

    ``references_to`` 存的是被引目标的 ``node_path``（如 "5.2.1"），故引用解析按
    node_path 查 ``meta_by_path``；**去重则按 node_id**（与 merge_results 一致，
    section/paragraph 粒度下 node_path 不唯一，node_id 恒唯一）。跨规范引用
    （cross_standard，如 "GB 50116-2013"）不在本规范 metadata 内 → 查不到自动跳过。
    """
    meta_by_path = {m["node_path"]: m for m in metadata}
    existing_ids = {r.get("node_id") for r in results}
    expanded = list(results)

    frontier = list(results)
    for _ in range(max_depth):
        next_frontier = []
        for item in frontier:
            for ref_path in item.get("references_to", []):
                ref_item = meta_by_path.get(ref_path)
                if ref_item is None or ref_item.get("node_id") in existing_ids:
                    continue
                new_item = dict(ref_item)
                new_item["_source"] = "ref_expand"
                new_item["_rrf_score"] = 0.0
                expanded.append(new_item)
                existing_ids.add(ref_item.get("node_id"))
                next_frontier.append(new_item)
        frontier = next_frontier

    return expanded


# ---------------------------------------------------------------------------
# Rerank
# ---------------------------------------------------------------------------

def rerank(query: str, results: list[dict], top_k: int) -> list[dict]:
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


# ---------------------------------------------------------------------------
# 单条款直取
# ---------------------------------------------------------------------------

def get_clause(store_dir: Path, node_path: str) -> dict | None:
    """按 node_path 从 metadata 直取单条款（供服务端 /clause 端点）。"""
    for m in load_metadata(store_dir):
        if m.get("node_path") == node_path:
            return dict(m)
    return None


# ---------------------------------------------------------------------------
# 主检索入口
# ---------------------------------------------------------------------------

def search(
    query: str,
    store_dir: Path,
    milvus_host: str,
    milvus_port: int,
    collection_name: str,
    embed_url: str,
    embed_model_id: str,
    top_k: int,
    bm25_top_k: int,
    vector_top_k: int,
    skip_rerank: bool,
    stats: dict | None = None,
) -> list[dict]:
    """混合检索主入口（重构前名为 ``retrieve``，逻辑不变）。

    若传入 ``stats`` 字典，会回填各阶段命中数（bm25_hits / vector_hits /
    merged / expanded / final），供服务层做可观测性输出；检索逻辑本身不受影响。
    """
    bm25, node_paths = load_bm25(store_dir)
    metadata = load_metadata(store_dir)
    client, col_name = connect_milvus(milvus_host, milvus_port, collection_name)

    bm25_results = bm25_search(query, bm25, node_paths, metadata, bm25_top_k)
    vector_results = vector_search(query, client, col_name, embed_url, embed_model_id, vector_top_k)

    merged = merge_results(bm25_results, vector_results)
    expanded = expand_references(merged, metadata)

    if skip_rerank:
        final = expanded[:top_k]
    else:
        final = rerank(query, expanded, top_k)

    if stats is not None:
        stats.update(
            bm25_hits=len(bm25_results),
            vector_hits=len(vector_results),
            merged=len(merged),
            expanded=len(expanded),
            final=len(final),
        )

    return final
