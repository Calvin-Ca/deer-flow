"""索引构建库 —— 把检索单元（已挂 reprs 的节点）建成 BM25 + Milvus 双索引。

从旧 ``pipeline/04_build_index.py`` 的库函数抽出（CLI/编排上移到根 ``build.py``）。
放 retrieval/ 包内：索引构建与检索共用 store 路径 / collection 命名（retrieval.config），
是「build 期」与「检索期」的接缝。各表征明确消费方：``sparse``→BM25 语料、``dense``→
嵌入文本（向量）、``raw``→content 字段（返回/rerank 用）；引用扩展用 ``references_to``
（从节点 references 桥接出 strong/cross_standard 边的 to，供 engine 沿用 list[str] 口径）。

设计转向（2026-06-12）后：**无 is_mandatory 字段 / 强条索引**。行带 node_id / parent_id /
granularity——parent_id 是检索期 small-to-big（T9）上探父节点的锚点。

依赖服务（服务器已部署）：Milvus localhost:19530 / vLLM BGE-large localhost:8097（dim 1024）。
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

from rich.console import Console
from tqdm import tqdm

from core import schema  # EXPANDABLE_REF_TYPES

console = Console()

EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度


# ---------------------------------------------------------------------------
# 文本 / 行准备（消费 reprs）
# ---------------------------------------------------------------------------

def _repr_text(node: dict, kind: str, fallback: str = "") -> str:
    """取节点某表征的文本（reprs 已由 reprs.enrich 挂好）；缺失则回退。"""
    return (node.get("reprs", {}).get(kind) or {}).get("text", fallback)


def _expandable_refs(node: dict) -> list[str]:
    """从节点 references 桥接出可正向扩展的目标 path 列表（strong / cross_standard）。

    供 engine.expand_references（吃 list[str] 的 references_to）沿用；weak/exclude 不入。
    """
    return [
        r["to"] for r in node.get("references", [])
        if r.get("to") and r.get("type") in schema.EXPANDABLE_REF_TYPES
    ]


def node_to_row(node: dict, granularity: str) -> dict:
    """Milvus 一行标量字段（embedding 由调用方填）。无 is_mandatory（强条机制已废）。"""
    return {
        "node_id":       node.get("node_id", ""),
        "node_path":   node.get("node_path", ""),
        "parent_id":     node.get("parent_id") or "",      # 检索期 small-to-big 锚点（T9）
        "granularity":   granularity,
        "standard_id":   node.get("standard_id", ""),
        "content":       _repr_text(node, "raw", node.get("content", ""))[:65_000],
        "level":         int(node.get("level", 0)),
        "page":          int(node.get("page", 0)),
        "references_to": json.dumps(_expandable_refs(node), ensure_ascii=False),
        "has_tables":    bool(node.get("tables")),
        "has_images":    bool(node.get("images")),
    }


# ---------------------------------------------------------------------------
# BM25（消费 sparse 表征）
# ---------------------------------------------------------------------------

def build_bm25(units: list[dict], store_dir: Path) -> None:
    from rank_bm25 import BM25Okapi

    console.print("构建 BM25 索引…")
    corpus = [list(_repr_text(u, "sparse")) for u in units]  # 字符级分词（中文无空格）
    bm25 = BM25Okapi(corpus)

    out = store_dir / "bm25.pkl"
    with open(out, "wb") as f:
        pickle.dump({"bm25": bm25, "node_paths": [u.get("node_path", "") for u in units]}, f)
    console.print(f"[green]✓ BM25 索引已写入 {out}[/green]")


# ---------------------------------------------------------------------------
# 向量索引（Milvus，消费 dense 表征）
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str], embed_url: str, model_id: str, batch_size: int) -> list[list[float]]:
    """调 vLLM embedding API，分批返回向量列表。"""
    import requests

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        # 空字符串替换为占位符；超长文本截断到 480 字（BGE max 512 tokens）
        batch = [t[:480] if t.strip() else "无内容" for t in batch]
        resp = requests.post(
            f"{embed_url}/v1/embeddings",
            json={"model": model_id, "input": batch},
            timeout=120,
        )
        if not resp.ok:
            print(f"[ERROR] batch {i}: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda x: x["index"])
        all_embeddings.extend(item["embedding"] for item in data)
    return all_embeddings


def build_vector_index(
    units: list[dict],
    collection_name: str,
    granularity: str,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    batch_size: int,
) -> None:
    from pymilvus import MilvusClient, DataType

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    console.print(f"[green]已连接 Milvus {milvus_host}:{milvus_port}[/green]")

    # 建或重建 collection
    if client.has_collection(collection_name):
        console.print(f"[yellow]集合 {collection_name} 已存在，将重建[/yellow]")
        client.drop_collection(collection_name)

    schema_ = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema_.add_field("id",            DataType.INT64,        is_primary=True)
    schema_.add_field("node_id",       DataType.VARCHAR,      max_length=192)
    schema_.add_field("node_path",   DataType.VARCHAR,      max_length=128)
    schema_.add_field("parent_id",     DataType.VARCHAR,      max_length=192)
    schema_.add_field("granularity",   DataType.VARCHAR,      max_length=16)
    schema_.add_field("standard_id",   DataType.VARCHAR,      max_length=64)
    schema_.add_field("content",       DataType.VARCHAR,      max_length=65_535)
    schema_.add_field("level",         DataType.INT64)
    schema_.add_field("page",          DataType.INT64)
    schema_.add_field("references_to", DataType.VARCHAR,      max_length=2_048)
    schema_.add_field("has_tables",    DataType.BOOL)
    schema_.add_field("has_images",    DataType.BOOL)
    schema_.add_field("embedding",     DataType.FLOAT_VECTOR, dim=EMBED_DIM)

    index_params = client.prepare_index_params()
    index_params.add_index("embedding",   metric_type="COSINE", index_type="HNSW",
                           params={"M": 16, "efConstruction": 200})
    index_params.add_index("node_path", index_type="INVERTED")
    index_params.add_index("node_id",     index_type="INVERTED")

    client.create_collection(collection_name=collection_name,
                             schema=schema_, index_params=index_params)
    console.print(f"集合 {collection_name} 已创建")

    # 分批嵌入（dense 表征文本）并插入
    texts = [_repr_text(u, "dense", u.get("content", "")) for u in units]
    rows = [node_to_row(u, granularity) for u in units]

    console.print(f"调用嵌入服务 {embed_url}，model={embed_model_id}…")
    total_batches = (len(units) + batch_size - 1) // batch_size
    for i in tqdm(range(0, len(units), batch_size), total=total_batches, desc="嵌入并插入"):
        batch_texts = texts[i: i + batch_size]
        batch_rows = rows[i: i + batch_size]
        embeddings = embed_texts(batch_texts, embed_url, embed_model_id, len(batch_texts))
        for j, row in enumerate(batch_rows):
            row["embedding"] = embeddings[j]
        client.insert(collection_name=collection_name, data=batch_rows)

    client.flush(collection_name)
    stats = client.get_collection_stats(collection_name)
    console.print(f"[green]✓ 向量索引完成：{stats['row_count']} 个向量[/green]")


# ---------------------------------------------------------------------------
# 元数据快照（供不依赖 Milvus 的脚本使用）
# ---------------------------------------------------------------------------

def save_metadata(units: list[dict], granularity: str, store_dir: Path) -> None:
    out = store_dir / "metadata.json"
    snapshot = [node_to_row(u, granularity) for u in units]
    # references_to 在快照里保持 list 格式，方便引用扩展
    for i, u in enumerate(units):
        snapshot[i]["references_to"] = _expandable_refs(u)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 元数据快照已写入 {out}[/green]")


# ---------------------------------------------------------------------------
# 高层入口（build.py 编排调用）
# ---------------------------------------------------------------------------

def build_index(
    units: list[dict],
    store_dir: Path,
    collection_name: str,
    granularity: str,
    *,
    milvus_host: str = "localhost",
    milvus_port: int = 19530,
    embed_url: str = "http://localhost:8097",
    embed_model_id: str = "/model",
    batch_size: int = 64,
    bm25_only: bool = False,
) -> None:
    """把检索单元建成 BM25 + metadata（+ 可选 Milvus 向量）索引，落 store_dir。

    参数：
        units (list[dict]): 已挂 reprs 的检索单元（view 选粒度 + reprs.enrich 之后）。
        store_dir (Path): 输出目录（按 profile 隔离，调用方建好）。
        collection_name (str): Milvus collection 名（由 retrieval.config.collection_name 推断）。
        granularity (str): 索引粒度（写入每行 granularity 字段）。
        bm25_only (bool): 只建 BM25 + metadata，跳过向量索引（无 embedding/Milvus 时用）。
    返回：
        无（产物写 store_dir：bm25.pkl / metadata.json / Milvus collection）。
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    build_bm25(units, store_dir)
    save_metadata(units, granularity, store_dir)
    if bm25_only:
        console.print("[dim]bm25_only：跳过向量索引[/dim]")
        return
    build_vector_index(units, collection_name, granularity,
                       milvus_host, milvus_port, embed_url, embed_model_id, batch_size)
