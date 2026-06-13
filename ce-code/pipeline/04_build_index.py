"""阶段 3 索引层：从节点树 nodes.json 建立双索引（BM25 + 向量）。

PRD §3.2 阶段 3：读建树产物 ``nodes.json`` → 按 ``index_granularity`` 选粒度视图
（``view.view``，T7）→ 挂表征（``reprs.enrich``，T8）→ emit 进 BM25 + Milvus 向量。
各表征有明确消费方：``sparse``→BM25 语料、``dense``→嵌入文本（向量）、``raw``→content
字段（返回/rerank 用）。粒度换实验只重跑本阶段、不重建树。

输入：data/structured/<standard>/<profile>/nodes.json
输出（按 profile 隔离，PRD §3.2）：
  data/vector_store/<standard>/<profile>/bm25.pkl       BM25 索引
  data/vector_store/<standard>/<profile>/metadata.json  检索单元元数据（与向量一一对应）
  Milvus collection: building_code_<safe_std>_<profile>  向量索引

设计转向（2026-06-12）后：**无 is_mandatory 字段 / 强条索引**（语气降级为 modal 表征，
第 4 步补）。行带 node_id / parent_id / granularity——parent_id 是检索期 small-to-big
（T9）上探父节点的锚点。引用扩展用 references_to（从节点 references 的可扩展边 strong /
cross_standard 桥接出的 to 列表，供 engine 沿用）。

依赖服务（服务器已部署）：
  Milvus:          localhost:19530
  vLLM BGE-large:  localhost:8097  (model id: /model, dim: 1024)
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import click
from rich.console import Console
from tqdm import tqdm

console = Console()

ROOT = Path(__file__).resolve().parent.parent  # ce-code/
DEFAULT_STORE = ROOT / "data" / "vector_store"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import schema  # noqa: E402  (EXPANDABLE_REF_TYPES)
import reprs  # noqa: E402  (阶段 2 表征注册表，T8)
from parse_profile import DEFAULT_REPRS  # noqa: E402
from view import view  # noqa: E402  (粒度视图，T7)
from retrieval.config import collection_name as build_collection_name  # noqa: E402

EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度


# ---------------------------------------------------------------------------
# 文本 / 行准备（消费 reprs，T8）
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
        "clause_path":   node.get("clause_path", ""),
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
        pickle.dump({"bm25": bm25, "clause_paths": [u.get("clause_path", "") for u in units]}, f)
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
    schema_.add_field("clause_path",   DataType.VARCHAR,      max_length=128)
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
    index_params.add_index("clause_path", index_type="INVERTED")
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
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--input", "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="建树产物 data/structured/<standard>/<profile>/nodes.json",
)
@click.option("--standard-id", default="", help="规范标识（默认从 nodes.json 路径的 <standard> 目录名推断）。")
@click.option("--profile-name", default="", help="parse_profile 名（默认从 nodes.json 父目录名推断）。")
@click.option(
    "--index-granularity",
    type=click.Choice(["section", "clause", "paragraph"]),
    default="clause", show_default=True,
    help="索引粒度视图（view 选哪层 emit；最小切片仅 clause 可用）。",
)
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="索引存储目录，默认 data/vector_store/<standard>/<profile>/",
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
    profile_name: str,
    index_granularity: str,
    store_dir: Path | None,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    batch_size: int,
    bm25_only: bool,
) -> None:
    """从节点树 nodes.json 建立 BM25 + 向量双索引（view 选粒度 → reprs 挂表征 → emit）。"""

    # 路径约定：data/structured/<standard>/<profile>/nodes.json
    if not profile_name:
        profile_name = input_path.parent.name
    if not standard_id:
        standard_id = input_path.parent.parent.name.replace("_", " ").strip()

    safe_id = standard_id.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")
    if store_dir is None:
        store_dir = DEFAULT_STORE / safe_id / profile_name
    store_dir.mkdir(parents=True, exist_ok=True)

    # collection 名用检索层共享规则按 store_dir.name（=profile）推断，与 07_eval/server 一致，
    # 故评测点 --store-dir 至本目录即得同名 collection、零改动。⚠️ 多规范并存时 profile 名
    # 须含规范区分（如 gb50016_clause），否则同名 profile 的 Milvus collection 会跨规范相撞。
    collection_name = build_collection_name(store_dir.name)

    console.print(f"[bold]规范：[/bold]{standard_id}")
    console.print(f"[bold]Profile：[/bold]{profile_name}  粒度={index_granularity}")
    console.print(f"[bold]存储目录：[/bold]{store_dir}")
    console.print(f"[bold]Milvus collection：[/bold]{collection_name}")

    with open(input_path, encoding="utf-8") as f:
        nodes = json.load(f)
    console.print(f"加载 {len(nodes)} 个节点")

    units = view(nodes, index_granularity)        # T7：选粒度视图
    reprs.enrich(units, list(DEFAULT_REPRS))       # T8：挂表征（免费 4 项）
    console.print(f"粒度 {index_granularity} → {len(units)} 个检索单元（已挂 reprs）")

    build_bm25(units, store_dir)
    save_metadata(units, index_granularity, store_dir)

    if not bm25_only:
        build_vector_index(units, collection_name, index_granularity,
                           milvus_host, milvus_port, embed_url, embed_model_id, batch_size)
    else:
        console.print("[dim]--bm25-only：跳过向量索引[/dim]")

    console.print(f"\n[bold green]✓ 索引构建完成[/bold green]")
    console.print(f"  BM25:   {store_dir}/bm25.pkl")
    console.print(f"  元数据: {store_dir}/metadata.json")
    if not bm25_only:
        console.print(f"  Milvus collection: {collection_name}")


if __name__ == "__main__":
    main()
