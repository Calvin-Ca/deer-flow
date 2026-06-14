"""向量索引 —— 消费 dense 表征，建 Milvus collection（嵌入在索引期统一计算）。

承旧 ``retrieval/indexer.build_vector_index`` + ``embed_texts``。dense 表征只产待嵌入文本，**向量
由本模块用 embedding 模型统一算**（模型唯一 owner 在检索栈）。collection schema 与旧版逐字一致：
node_path（INVERTED 去重/直取）+ small-to-big 锚点 parent_id + 标量行 + FLOAT_VECTOR(embedding)；
**无 is_mandatory 字段**（强条机制已废）。

依赖服务（服务器已部署）：Milvus localhost:19530 / vLLM BGE-large localhost:8097（dim 1024）。
"""
from __future__ import annotations

from rich.console import Console
from tqdm import tqdm

from config import EMBED_DIM
from core.chunk import Chunk

console = Console()


def embed_texts(texts: list[str], embed_url: str, model_id: str, batch_size: int) -> list[list[float]]:
    """调 vLLM embedding API，分批返回向量列表（空串占位、超长截断 480 字）。"""
    import requests

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
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


def build(
    units: list[Chunk],
    rows: list[dict],
    collection_name: str,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    batch_size: int,
) -> None:
    """建/重建 Milvus collection：嵌入 dense 文本 + 插入标量行。

    参数：
        units (list[Chunk]): 检索单元（取 dense 表征文本嵌入）。
        rows (list[dict]): 与 units 同序的标量行（index.manager.chunk_to_row 产，references_to 为 JSON 串）。
        collection_name (str): collection 名（config.collection_name 推断）。
        其余：Milvus 地址 / 嵌入服务 / 批大小。
    返回：
        无。
    """
    from pymilvus import DataType, MilvusClient

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    console.print(f"[green]已连接 Milvus {milvus_host}:{milvus_port}[/green]")

    if client.has_collection(collection_name):
        console.print(f"[yellow]集合 {collection_name} 已存在，将重建[/yellow]")
        client.drop_collection(collection_name)

    schema_ = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema_.add_field("id",            DataType.INT64,        is_primary=True)
    schema_.add_field("node_path",     DataType.VARCHAR,      max_length=192)
    schema_.add_field("parent_id",     DataType.VARCHAR,      max_length=192)
    schema_.add_field("granularity",   DataType.VARCHAR,      max_length=16)
    schema_.add_field("standard_id",   DataType.VARCHAR,      max_length=64)
    schema_.add_field("content",       DataType.VARCHAR,      max_length=65_535)
    schema_.add_field("node_level",    DataType.INT64)
    schema_.add_field("page",          DataType.INT64)
    schema_.add_field("references_to", DataType.VARCHAR,      max_length=2_048)
    schema_.add_field("has_tables",    DataType.BOOL)
    schema_.add_field("has_images",    DataType.BOOL)
    schema_.add_field("embedding",     DataType.FLOAT_VECTOR, dim=EMBED_DIM)

    index_params = client.prepare_index_params()
    index_params.add_index("embedding", metric_type="COSINE", index_type="HNSW",
                           params={"M": 16, "efConstruction": 200})
    index_params.add_index("node_path", index_type="INVERTED")

    client.create_collection(collection_name=collection_name,
                             schema=schema_, index_params=index_params)
    console.print(f"集合 {collection_name} 已创建")

    texts = [u.feature_text("dense", u.content) for u in units]
    console.print(f"调用嵌入服务 {embed_url}，model={embed_model_id}…")
    total_batches = (len(units) + batch_size - 1) // batch_size
    for i in tqdm(range(0, len(units), batch_size), total=total_batches, desc="嵌入并插入"):
        batch_texts = texts[i: i + batch_size]
        batch_rows = [dict(r) for r in rows[i: i + batch_size]]
        embeddings = embed_texts(batch_texts, embed_url, embed_model_id, len(batch_texts))
        for j, row in enumerate(batch_rows):
            row["embedding"] = embeddings[j]
        client.insert(collection_name=collection_name, data=batch_rows)

    client.flush(collection_name)
    stats = client.get_collection_stats(collection_name)
    console.print(f"[green]✓ 向量索引完成：{stats['row_count']} 个向量[/green]")
