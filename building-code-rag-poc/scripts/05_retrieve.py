"""阶段 1 第二步：混合检索 + 引用扩展 + Rerank。

流程（按 CLAUDE.md §4.3）：
  Query
    → BM25 召回（精确匹配条文号、术语）
    → 向量召回（语义相似，Milvus HNSW）
    → RRF 合并去重
    → 引用图扩展（命中条款的 references_to 一并拉取）
    → Rerank（bge-reranker-large；强条不被截断）
    → 返回 top-k（带元数据）

使用方式：
  # 单条查询
  uv run python scripts/05_retrieve.py \\
    --store-dir data/vector_store/GB_50016-20142018 \\
    --query "24米住宅疏散楼梯宽度"

  # 批量评测
  uv run python scripts/05_retrieve.py \\
    --store-dir data/vector_store/GB_50016-20142018 \\
    --eval-set data/eval_set/gb50016_eval.json \\
    --output data/eval_results/gb50016_retrieval.json
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent
RERANK_MODEL = "BAAI/bge-reranker-large"
EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
COLLECTION_PREFIX = "building_code"

MILVUS_OUTPUT_FIELDS = [
    "clause_path", "standard_id", "content", "is_mandatory",
    "level", "page", "references_to", "has_tables", "has_images",
]


# ---------------------------------------------------------------------------
# 索引加载
# ---------------------------------------------------------------------------

def load_bm25(store_dir: Path):
    with open(store_dir / "bm25.pkl", "rb") as f:
        data = pickle.load(f)
    return data["bm25"], data["clause_paths"]


def load_metadata(store_dir: Path) -> list[dict]:
    with open(store_dir / "metadata.json", encoding="utf-8") as f:
        return json.load(f)


def connect_milvus(milvus_host: str, milvus_port: int, collection_name: str):
    from pymilvus import connections, Collection
    connections.connect(host=milvus_host, port=str(milvus_port))
    collection = Collection(name=collection_name)
    collection.load()
    return collection


# ---------------------------------------------------------------------------
# 单路召回
# ---------------------------------------------------------------------------

def bm25_search(
    query: str,
    bm25,
    clause_paths: list[str],
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
    collection,
    model,
    top_k: int,
    filter_mandatory: bool = False,
) -> list[dict]:
    query_vec = model.encode([query], normalize_embeddings=True)[0].tolist()

    expr = "is_mandatory == true" if filter_mandatory else ""
    search_params = {"metric_type": "COSINE", "params": {"ef": 64}}

    hits = collection.search(
        data=[query_vec],
        anns_field="embedding",
        param=search_params,
        limit=top_k,
        expr=expr or None,
        output_fields=MILVUS_OUTPUT_FIELDS,
    )

    results = []
    for hit in hits[0]:
        item = {f: hit.entity.get(f) for f in MILVUS_OUTPUT_FIELDS}
        # references_to 在 Milvus 里是 JSON 字符串，还原为 list
        refs_raw = item.get("references_to", "[]")
        item["references_to"] = json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
        item["_vector_score"] = hit.score
        item["_source"] = "vector"
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# RRF 合并去重
# ---------------------------------------------------------------------------

def merge_results(bm25_results: list[dict], vector_results: list[dict]) -> list[dict]:
    k = 60  # RRF 常数
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, item in enumerate(bm25_results):
        path = item["clause_path"]
        scores[path] = scores.get(path, 0) + 1 / (k + rank + 1)
        items[path] = item

    for rank, item in enumerate(vector_results):
        path = item["clause_path"]
        scores[path] = scores.get(path, 0) + 1 / (k + rank + 1)
        if path not in items:
            items[path] = item
        else:
            items[path]["_source"] = "both"
            items[path]["_vector_score"] = item.get("_vector_score")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results = []
    for path, rrf_score in ranked:
        item = dict(items[path])
        item["_rrf_score"] = rrf_score
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# 引用图扩展
# ---------------------------------------------------------------------------

def expand_references(results: list[dict], metadata: list[dict], max_depth: int = 1) -> list[dict]:
    meta_by_path = {m["clause_path"]: m for m in metadata}
    existing_paths = {r["clause_path"] for r in results}
    expanded = list(results)

    to_expand = list(existing_paths)
    for _ in range(max_depth):
        next_expand = []
        for path in to_expand:
            item = meta_by_path.get(path, {})
            for ref in item.get("references_to", []):
                if ref not in existing_paths and ref in meta_by_path:
                    ref_item = dict(meta_by_path[ref])
                    ref_item["_source"] = "ref_expand"
                    ref_item["_rrf_score"] = 0.0
                    expanded.append(ref_item)
                    existing_paths.add(ref)
                    next_expand.append(ref)
        to_expand = next_expand

    return expanded


# ---------------------------------------------------------------------------
# Rerank
# ---------------------------------------------------------------------------

def rerank(query: str, results: list[dict], top_k: int, hf_endpoint: str) -> list[dict]:
    import os
    if hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", hf_endpoint)

    from FlagEmbedding import FlagReranker
    reranker = FlagReranker(RERANK_MODEL, use_fp16=True)

    pairs = [[query, r.get("content", "")] for r in results]
    scores = reranker.compute_score(pairs, normalize=True)

    for item, score in zip(results, scores):
        item["_rerank_score"] = float(score)

    results_sorted = sorted(results, key=lambda x: x.get("_rerank_score", 0), reverse=True)
    mandatory = [r for r in results_sorted if r.get("is_mandatory")]
    non_mandatory = [r for r in results_sorted if not r.get("is_mandatory")]
    # 强条全部保留，非强条截断到 top_k
    return mandatory + non_mandatory[: max(0, top_k - len(mandatory))]


# ---------------------------------------------------------------------------
# 主检索入口
# ---------------------------------------------------------------------------

def retrieve(
    query: str,
    store_dir: Path,
    milvus_host: str,
    milvus_port: int,
    collection_name: str,
    top_k: int,
    bm25_top_k: int,
    vector_top_k: int,
    hf_endpoint: str,
    skip_rerank: bool,
) -> list[dict]:
    bm25, clause_paths = load_bm25(store_dir)
    metadata = load_metadata(store_dir)

    from sentence_transformers import SentenceTransformer
    embed_model = SentenceTransformer(EMBED_MODEL, model_kwargs={"use_safetensors": True})
    collection = connect_milvus(milvus_host, milvus_port, collection_name)

    bm25_results = bm25_search(query, bm25, clause_paths, metadata, bm25_top_k)
    vector_results = vector_search(query, collection, embed_model, vector_top_k)

    merged = merge_results(bm25_results, vector_results)
    expanded = expand_references(merged, metadata)

    if skip_rerank:
        mandatory = [r for r in expanded if r.get("is_mandatory")]
        non_mandatory = [r for r in expanded if not r.get("is_mandatory")]
        return mandatory + non_mandatory[: max(0, top_k - len(mandatory))]

    return rerank(query, expanded, top_k, hf_endpoint)


# ---------------------------------------------------------------------------
# 批量评测
# ---------------------------------------------------------------------------

def run_eval(
    eval_path: Path,
    store_dir: Path,
    milvus_host: str,
    milvus_port: int,
    collection_name: str,
    top_k: int,
    hf_endpoint: str,
    skip_rerank: bool,
    output_path: Path,
) -> None:
    with open(eval_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    results = []
    recall_scores = []

    for item in eval_set:
        query = item["query"]
        expected = set(item.get("expected_clauses", []))
        if not expected:
            continue

        hits = retrieve(
            query, store_dir, milvus_host, milvus_port, collection_name,
            top_k, top_k * 2, top_k * 2, hf_endpoint, skip_rerank,
        )
        hit_paths = {h["clause_path"] for h in hits}
        recalled = expected & hit_paths
        recall = len(recalled) / len(expected)

        mandatory_expected = set(item.get("expected_clauses", [])) if item.get("must_be_mandatory") else set()
        mandatory_recalled = mandatory_expected & hit_paths
        mandatory_recall = len(mandatory_recalled) / len(mandatory_expected) if mandatory_expected else None

        results.append({
            "id": item["id"],
            "query": query,
            "expected": list(expected),
            "recalled": list(recalled),
            "missed": list(expected - hit_paths),
            "recall": recall,
            "mandatory_recall": mandatory_recall,
        })
        recall_scores.append(recall)

    avg_recall = sum(recall_scores) / len(recall_scores) if recall_scores else 0
    mandatory_recalls = [r["mandatory_recall"] for r in results if r["mandatory_recall"] is not None]
    avg_mandatory_recall = sum(mandatory_recalls) / len(mandatory_recalls) if mandatory_recalls else 0

    summary = {
        "total_queries": len(results),
        "avg_recall": avg_recall,
        "avg_mandatory_recall": avg_mandatory_recall,
        "results": results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    console.print(f"\n[bold]评测结果[/bold]")
    console.print(f"  查询数：{len(results)}")
    console.print(f"  平均召回率：{avg_recall:.1%}")
    console.print(f"  强条平均召回率：{avg_mandatory_recall:.1%}  ← 核心指标")
    console.print(f"[green]✓ 详细结果写入 {output_path}[/green]")


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_results(query: str, results: list[dict]) -> None:
    console.print(f"\n[bold]查询：[/bold]{query}")
    console.print(f"[bold]召回 {len(results)} 条条款[/bold]\n")

    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("条款号", style="cyan", width=12)
    t.add_column("强条", width=4)
    t.add_column("来源", width=10)
    t.add_column("Rerank", justify="right", width=7)
    t.add_column("正文片段", no_wrap=False, max_width=60)

    for r in results:
        mandatory = "[red]✓[/red]" if r.get("is_mandatory") else ""
        rerank_score = f"{r['_rerank_score']:.3f}" if "_rerank_score" in r else "-"
        snippet = (r.get("content") or "")[:80].replace("\n", " ")
        t.add_row(r["clause_path"], mandatory, r.get("_source", ""), rerank_score, snippet)

    console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="data/vector_store/<standard>/",
)
@click.option("--query", "-q", default="", help="单条检索查询。")
@click.option(
    "--eval-set", "eval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
@click.option(
    "--output", "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
)
@click.option("--top-k", default=20, show_default=True, help="最终返回条款数（强条不截断）。")
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--collection", default="", help="Milvus collection 名（默认从 store-dir 推断）。")
@click.option("--hf-endpoint", default="https://hf-mirror.com", show_default=True)
@click.option("--skip-rerank", is_flag=True, help="跳过 Rerank（调试用）。")
def main(
    store_dir: Path,
    query: str,
    eval_path: Path | None,
    output_path: Path | None,
    top_k: int,
    milvus_host: str,
    milvus_port: int,
    collection: str,
    hf_endpoint: str,
    skip_rerank: bool,
) -> None:
    """混合检索 + 引用扩展 + Rerank（Milvus）。"""

    if not collection:
        collection = f"{COLLECTION_PREFIX}_{store_dir.name}".lower()

    if eval_path:
        if output_path is None:
            output_path = ROOT / "data" / "eval_results" / f"{store_dir.name}_retrieval.json"
        run_eval(eval_path, store_dir, milvus_host, milvus_port, collection,
                 top_k, hf_endpoint, skip_rerank, output_path)
    elif query:
        results = retrieve(
            query, store_dir, milvus_host, milvus_port, collection,
            top_k, top_k * 2, top_k * 2, hf_endpoint, skip_rerank,
        )
        print_results(query, results)
    else:
        console.print("[red]请指定 --query 或 --eval-set[/red]")


if __name__ == "__main__":
    main()
