"""向量检索器 —— bge-large-zh-v1.5 语义召回（承旧 engine.vector_search）。

对查询嵌入后在 Milvus collection 做 COSINE 近邻搜索，取 top_k。返回「索引行 dict」（带
``_vector_score`` / ``_source`` + ``references_to`` 解析回 list）供 hybrid RRF 合并；``retrieve``
出口转 RetrievedChunk。

embedding/Milvus 地址由调用方传入（service 从 config.DEFAULTS 取，与建索引期一致）。
"""
from __future__ import annotations

import json

from core.query import RetrievalQuery
from core.retrieval import RetrievedChunk
from retrieval.base import Retriever

# 与建索引期 Milvus schema 对齐的输出标量字段（无 is_mandatory，含 small-to-big 锚点 parent_id）。
MILVUS_OUTPUT_FIELDS = [
    "node_path", "parent_id", "granularity", "standard_id", "content",
    "node_level", "page", "references_to", "has_tables", "has_images",
]


def embed_texts(texts: list[str], embed_url: str, model_id: str) -> list[list[float]]:
    """调 embedding API 取向量（查询期，不截断；承旧 engine.embed_texts）。"""
    import requests
    resp = requests.post(
        f"{embed_url}/v1/embeddings",
        json={"model": model_id, "input": texts},
        timeout=120,
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in data]


def vector_search(query: str, client, collection_name: str, embed_url: str,
                  embed_model_id: str, top_k: int) -> list[dict]:
    """向量近邻搜索，返回索引行 dict（承旧 engine，逐字保持）。"""
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


class DenseRetriever(Retriever):
    """向量语义召回（单路）。"""

    name = "dense"

    def __init__(self, collection_name: str, *, milvus_host: str = "localhost",
                 milvus_port: int = 19530, embed_url: str = "http://localhost:8097",
                 embed_model_id: str = "/model") -> None:
        self.collection_name = collection_name
        self.milvus_host = milvus_host
        self.milvus_port = milvus_port
        self.embed_url = embed_url
        self.embed_model_id = embed_model_id
        self._client = None

    def _connect(self):
        if self._client is None:
            from pymilvus import MilvusClient
            self._client = MilvusClient(uri=f"http://{self.milvus_host}:{self.milvus_port}")
        return self._client

    def search_rows(self, text: str, top_k: int) -> list[dict]:
        """返回索引行 dict（供 hybrid RRF 合并）。"""
        client = self._connect()
        return vector_search(text, client, self.collection_name,
                             self.embed_url, self.embed_model_id, top_k)

    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]:
        rows = self.search_rows(query.text, query.top_k)
        return [RetrievedChunk.from_row(r) for r in rows]
