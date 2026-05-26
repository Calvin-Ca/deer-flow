"""阶段 1 第一步：从条款树 JSON 建立双索引（BM25 + 向量）。

输入：data/structured/<standard>_clauses.json
输出：
  data/vector_store/<standard>/bm25.pkl        BM25 索引
  data/vector_store/<standard>/metadata.json   条款元数据（与向量一一对应）
  Milvus collection: building_code_<standard>  向量索引（含全部元数据）

安装依赖（服务器上）：
  uv pip install -e ".[retrieval]"

Milvus 服务已在服务器上运行（端口 19530）。

嵌入模型：BAAI/bge-large-zh-v1.5（中文规范效果最佳）
Rerank 模型：BAAI/bge-reranker-large（阶段 1 检索时使用）
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import click
from rich.console import Console
from tqdm import tqdm

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORE = ROOT / "data" / "vector_store"

EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度
COLLECTION_PREFIX = "building_code"


# ---------------------------------------------------------------------------
# 文本准备
# ---------------------------------------------------------------------------

def clause_to_text(clause: dict) -> str:
    """把条款转成用于嵌入的文本：条款号 + 正文（+ 表格 caption）。"""
    path = clause.get("clause_path", "")
    content = clause.get("content", "").strip()
    table_captions = " ".join(
        t.get("caption", "") for t in clause.get("tables", []) if t.get("caption")
    )
    parts = [f"条款{path}", content]
    if table_captions:
        parts.append(table_captions)
    return " ".join(p for p in parts if p)


def clause_to_row(clause: dict) -> dict:
    """Milvus 一行数据：标量字段 + embedding 占位（由调用方填入）。"""
    return {
        "clause_path":   clause.get("clause_path", ""),
        "standard_id":   clause.get("standard_id", ""),
        "content":       (clause.get("content") or "")[:65_000],  # VARCHAR 上限
        "is_mandatory":  bool(clause.get("is_mandatory", False)),
        "level":         int(clause.get("level", 0)),
        "page":          int(clause.get("page", 0)),
        "references_to": json.dumps(clause.get("references_to", []), ensure_ascii=False),
        "has_tables":    bool(clause.get("tables")),
        "has_images":    bool(clause.get("images")),
    }


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------

def build_bm25(clauses: list[dict], store_dir: Path) -> None:
    from rank_bm25 import BM25Okapi

    console.print("构建 BM25 索引…")
    corpus = [list(clause_to_text(c)) for c in clauses]  # 字符级分词（中文无空格）
    bm25 = BM25Okapi(corpus)

    out = store_dir / "bm25.pkl"
    with open(out, "wb") as f:
        pickle.dump({"bm25": bm25, "clause_paths": [c["clause_path"] for c in clauses]}, f)
    console.print(f"[green]✓ BM25 索引已写入 {out}[/green]")


# ---------------------------------------------------------------------------
# 向量索引（Milvus）
# ---------------------------------------------------------------------------

def build_vector_index(
    clauses: list[dict],
    collection_name: str,
    milvus_host: str,
    milvus_port: int,
    batch_size: int,
    hf_endpoint: str,
) -> None:
    import os
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint

    from sentence_transformers import SentenceTransformer
    from pymilvus import (
        connections, utility, Collection, CollectionSchema,
        FieldSchema, DataType,
    )

    console.print(f"加载嵌入模型 {EMBED_MODEL}…")
    model = SentenceTransformer(EMBED_MODEL)

    # 连接 Milvus
    connections.connect(host=milvus_host, port=str(milvus_port))
    console.print(f"[green]已连接 Milvus {milvus_host}:{milvus_port}[/green]")

    # 建或重建 collection
    if utility.has_collection(collection_name):
        console.print(f"[yellow]集合 {collection_name} 已存在，将重建[/yellow]")
        utility.drop_collection(collection_name)

    fields = [
        FieldSchema(name="id",            dtype=DataType.INT64,   is_primary=True, auto_id=True),
        FieldSchema(name="clause_path",   dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="standard_id",   dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="content",       dtype=DataType.VARCHAR, max_length=65_535),
        FieldSchema(name="is_mandatory",  dtype=DataType.BOOL),
        FieldSchema(name="level",         dtype=DataType.INT64),
        FieldSchema(name="page",          dtype=DataType.INT64),
        FieldSchema(name="references_to", dtype=DataType.VARCHAR, max_length=2_048),  # JSON 字符串
        FieldSchema(name="has_tables",    dtype=DataType.BOOL),
        FieldSchema(name="has_images",    dtype=DataType.BOOL),
        FieldSchema(name="embedding",     dtype=DataType.FLOAT_VECTOR, dim=EMBED_DIM),
    ]
    schema = CollectionSchema(fields=fields, description=f"建筑规范条款库 {collection_name}")
    collection = Collection(name=collection_name, schema=schema)
    console.print(f"集合 {collection_name} 已创建")

    # 分批嵌入并插入
    texts = [clause_to_text(c) for c in clauses]
    rows = [clause_to_row(c) for c in clauses]

    total_batches = (len(clauses) + batch_size - 1) // batch_size
    for i in tqdm(range(0, len(clauses), batch_size), total=total_batches, desc="嵌入并插入"):
        batch_texts = texts[i: i + batch_size]
        batch_rows = rows[i: i + batch_size]

        embeddings = model.encode(
            batch_texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()

        data = {k: [r[k] for r in batch_rows] for k in batch_rows[0]}
        data["embedding"] = embeddings
        collection.insert(list(data.values()))

    collection.flush()

    # 建 HNSW 向量索引
    collection.create_index(
        field_name="embedding",
        index_params={
            "metric_type": "COSINE",
            "index_type":  "HNSW",
            "params":      {"M": 16, "efConstruction": 200},
        },
    )
    # 标量索引（用于过滤强条）
    collection.create_index(field_name="is_mandatory")
    collection.create_index(field_name="clause_path")

    collection.load()
    console.print(f"[green]✓ 向量索引完成：{collection.num_entities} 个向量[/green]")


# ---------------------------------------------------------------------------
# 元数据快照（供不依赖 Milvus 的脚本使用）
# ---------------------------------------------------------------------------

def save_metadata(clauses: list[dict], store_dir: Path) -> None:
    out = store_dir / "metadata.json"
    snapshot = [clause_to_row(c) for c in clauses]
    # references_to 在快照里保持 list 格式，方便引用扩展
    for i, clause in enumerate(clauses):
        snapshot[i]["references_to"] = clause.get("references_to", [])
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    console.print(f"[green]✓ 元数据快照已写入 {out}[/green]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--input", "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="data/structured/<standard>_clauses.json",
)
@click.option("--standard-id", default="", help="规范标识（默认从文件名推断）。")
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="索引存储目录，默认 data/vector_store/<standard>/",
)
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--batch-size", default=64, show_default=True, help="嵌入批大小。")
@click.option(
    "--hf-endpoint", default="https://hf-mirror.com", show_default=True,
    help="HuggingFace 镜像地址。",
)
@click.option("--bm25-only", is_flag=True, help="只建 BM25，跳过向量索引（不需要 GPU）。")
def main(
    input_path: Path,
    standard_id: str,
    store_dir: Path | None,
    milvus_host: str,
    milvus_port: int,
    batch_size: int,
    hf_endpoint: str,
    bm25_only: bool,
) -> None:
    """从条款树 JSON 建立 BM25 + 向量双索引（Milvus）。"""

    if not standard_id:
        standard_id = (
            input_path.stem
            .replace("_clauses", "")
            .replace("_", " ")
            .strip()
        )

    safe_id = standard_id.replace(" ", "_").replace("(", "").replace(")", "")
    if store_dir is None:
        store_dir = DEFAULT_STORE / safe_id
    store_dir.mkdir(parents=True, exist_ok=True)

    collection_name = f"{COLLECTION_PREFIX}_{safe_id}".lower()

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]存储目录：[/bold]{store_dir}")
    console.print(f"[bold]Milvus collection：[/bold]{collection_name}")

    with open(input_path, encoding="utf-8") as f:
        clauses = json.load(f)
    console.print(f"加载 {len(clauses)} 条条款")

    build_bm25(clauses, store_dir)
    save_metadata(clauses, store_dir)

    if not bm25_only:
        build_vector_index(clauses, collection_name, milvus_host, milvus_port, batch_size, hf_endpoint)
    else:
        console.print("[dim]--bm25-only：跳过向量索引[/dim]")

    console.print(f"\n[bold green]✓ 索引构建完成[/bold green]")
    console.print(f"  BM25:   {store_dir}/bm25.pkl")
    console.print(f"  元数据: {store_dir}/metadata.json")
    if not bm25_only:
        console.print(f"  Milvus collection: {collection_name}")


if __name__ == "__main__":
    main()
