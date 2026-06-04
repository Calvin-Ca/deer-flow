"""阶段 1 第一步：从条款树 JSON 建立双索引（BM25 + 向量）。

输入：data/structured/<standard>_clauses.json
输出：
  data/vector_store/<standard>/bm25.pkl        BM25 索引
  data/vector_store/<standard>/metadata.json   条款元数据（与向量一一对应）
  Milvus collection: building_code_<standard>  向量索引（含全部元数据）

依赖服务（服务器已部署）：
  Milvus:          localhost:19530
  vLLM BGE-large:  localhost:8097  (model id: /model, dim: 1024)

安装依赖：
  uv pip install --python .venv/bin/python "pymilvus>=2.4.0" "rank-bm25>=0.2.2" requests
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
    clauses: list[dict],
    collection_name: str,
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

    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id",            DataType.INT64,         is_primary=True)
    schema.add_field("clause_path",   DataType.VARCHAR,       max_length=64)
    schema.add_field("standard_id",   DataType.VARCHAR,       max_length=64)
    schema.add_field("content",       DataType.VARCHAR,       max_length=65_535)
    schema.add_field("is_mandatory",  DataType.BOOL)
    schema.add_field("level",         DataType.INT64)
    schema.add_field("page",          DataType.INT64)
    schema.add_field("references_to", DataType.VARCHAR,       max_length=2_048)
    schema.add_field("has_tables",    DataType.BOOL)
    schema.add_field("has_images",    DataType.BOOL)
    schema.add_field("embedding",     DataType.FLOAT_VECTOR,  dim=EMBED_DIM)

    index_params = client.prepare_index_params()
    index_params.add_index("embedding",    metric_type="COSINE", index_type="HNSW",
                           params={"M": 16, "efConstruction": 200})
    index_params.add_index("is_mandatory", index_type="INVERTED")
    index_params.add_index("clause_path",  index_type="INVERTED")

    client.create_collection(collection_name=collection_name,
                             schema=schema, index_params=index_params)
    console.print(f"集合 {collection_name} 已创建")

    # 分批嵌入并插入
    texts = [clause_to_text(c) for c in clauses]
    rows = [clause_to_row(c) for c in clauses]

    console.print(f"调用嵌入服务 {embed_url}，model={embed_model_id}…")
    total_batches = (len(clauses) + batch_size - 1) // batch_size
    for i in tqdm(range(0, len(clauses), batch_size), total=total_batches, desc="嵌入并插入"):
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
@click.option("--embed-url", default="http://localhost:8097", show_default=True,
              help="vLLM embedding 服务地址。")
@click.option("--embed-model-id", default="/model", show_default=True,
              help="vLLM 中的模型 ID（见 /v1/models）。")
@click.option("--batch-size", default=64, show_default=True, help="嵌入批大小。")
@click.option("--bm25-only", is_flag=True, help="只建 BM25，跳过向量索引。")
def main(
    input_path: Path,
    standard_id: str,
    store_dir: Path | None,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    batch_size: int,
    bm25_only: bool,
) -> None:
    """从条款树 JSON 建立 BM25 + 向量双索引（Milvus + vLLM）。"""

    if not standard_id:
        standard_id = (
            input_path.stem
            .replace("_clauses", "")
            .replace("_", " ")
            .strip()
        )

    safe_id = standard_id.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")
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
        build_vector_index(clauses, collection_name, milvus_host, milvus_port, embed_url, embed_model_id, batch_size)
    else:
        console.print("[dim]--bm25-only：跳过向量索引[/dim]")

    console.print(f"\n[bold green]✓ 索引构建完成[/bold green]")
    console.print(f"  BM25:   {store_dir}/bm25.pkl")
    console.print(f"  元数据: {store_dir}/metadata.json")
    if not bm25_only:
        console.print(f"  Milvus collection: {collection_name}")


if __name__ == "__main__":
    main()
