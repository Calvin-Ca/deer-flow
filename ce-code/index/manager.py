"""索引管理器 —— 粒度视图 + 行准备 + 编排建索引（PRD §3.1 ③ / §3.2 阶段 3）。

承旧 ``retrieval/indexer.py`` 的「高层入口 + 行准备」+ 旧 ``core/view.py`` 的粒度视图，合一为
index 层的编排中枢：

  ① ``view(chunks, granularity)``：在**完整 Chunk 树**上选某一层 emit 检索单元（**不切树**）。
     clause 粒度 emit ``chunk_type=="leaf"`` **且已接地**（``is_grounded``，剔除未接地空骨架——
     content 空的死单元，承本会话已落地的过滤契约）；section/paragraph 留后补。
  ② ``chunk_to_row(chunk, granularity)``：Chunk → Milvus/BM25 共用的标量行（**字段名逐字保持旧
     index 行契约**：node_path/parent_id/granularity/standard_id/content/node_level/page/
     references_to/has_tables/has_images——`ce-services` 经 /search 读这些名）。
  ③ ``build_index(units, ...)``：分派 bm25_index / metadata_index /（可选）vector_index。

各表征明确消费方：``sparse``→BM25 语料、``dense``→嵌入文本（向量）、``raw``→content 字段。
引用扩展用 ``references_to``（从 Chunk.references 桥接 strong/cross_standard 边的 to）。
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from core.chunk import Chunk

console = Console()

# index_granularity → emit 该粒度的 chunk_type。clause emit leaf（叶·检索单元）。
_LEAF_TYPES: frozenset[str] = frozenset({"leaf"})


def view(chunks: list[Chunk], index_granularity: str = "clause") -> list[Chunk]:
    """在 Chunk 树上选定粒度视图，返回该层检索单元（索引期纯函数，确定性可重放）。

    参数：
        chunks (list[Chunk]): 完整节点树。
        index_granularity (str): section | clause | paragraph（最小切片仅 clause）。
    返回：
        list[Chunk]: 选中的检索单元（**已剔除未接地空骨架** leaf）。
    异常：
        NotImplementedError: section / paragraph（最小切片未实现）。
        ValueError: 取值非法。
    """
    if index_granularity == "clause":
        return [c for c in chunks if c.chunk_type in _LEAF_TYPES and c.is_grounded()]
    if index_granularity in ("section", "paragraph"):
        raise NotImplementedError(
            f"index_granularity={index_granularity!r} 后补；最小切片先只做 clause 层 emit"
        )
    raise ValueError(
        f"未知 index_granularity={index_granularity!r}（应为 section | clause | paragraph）"
    )


def chunk_to_row(chunk: Chunk, granularity: str) -> dict:
    """Chunk → 索引标量行（references_to 为 JSON 串，供 Milvus VARCHAR；无强条字段）。"""
    page = chunk.provenance.page[0] if chunk.provenance.page else 0
    return {
        "node_path":   chunk.node_path,                                   # 节点 id（去重键）
        "parent_id":   chunk.parent_id or "",                             # small-to-big 锚点
        "granularity": granularity,
        "standard_id": chunk.standard_id,
        "content":     chunk.feature_text("raw", chunk.content)[:65_000],
        "node_level":  int(chunk.level),                                  # 行字段名（= Chunk.level）
        "page":        int(page),
        "references_to": json.dumps(chunk.expandable_refs(), ensure_ascii=False),
        "has_tables":  bool(chunk.tables),
        "has_images":  bool(chunk.images),
    }


def build_index(
    units: list[Chunk],
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
        units (list[Chunk]): 已挂 features 的检索单元（view 选粒度之后）。
        store_dir (Path): 输出目录（按 profile 隔离，调用方建好）。
        collection_name (str): Milvus collection 名（config.collection_name 推断）。
        granularity (str): 索引粒度（写入每行 granularity 字段）。
        bm25_only (bool): 只建 BM25 + metadata，跳过向量索引（无 embedding/Milvus 时用）。
    返回：
        无（产物写 store_dir：bm25.pkl / metadata.json / Milvus collection）。
    """
    from index import bm25_index, metadata_index

    store_dir.mkdir(parents=True, exist_ok=True)
    rows = [chunk_to_row(u, granularity) for u in units]

    bm25_index.build(units, store_dir)
    # metadata 快照：references_to 存 list（方便引用扩展），其余同行
    meta_rows = [{**row, "references_to": u.expandable_refs()} for row, u in zip(rows, units)]
    metadata_index.save(meta_rows, store_dir)

    if bm25_only:
        console.print("[dim]bm25_only：跳过向量索引[/dim]")
        return

    from index import vector_index
    vector_index.build(units, rows, collection_name, milvus_host, milvus_port,
                       embed_url, embed_model_id, batch_size)
