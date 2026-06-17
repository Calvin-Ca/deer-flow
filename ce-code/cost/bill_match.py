"""构件→清单候选召回 —— /bill/match 取数原语（dense 向量检索）。

输入构件/做法的自然语言描述，嵌入后在 ``bill_spec_kb`` Milvus collection 做 COSINE 检索，返回
top_k 清单项候选 + 出处。**知识层只负责召回候选**；在候选内决策选码（LLM）归任务层 CostAgent
（红线：只建议不定稿，HITL 复核）。KG 约束（清单↔定额覆盖、章节对齐收窄候选）为后续增强项。

与 ``cost.query``（PG 只读取数）分层：本文件走 Milvus + embedding（与 PG 依赖隔离），故单列一文件。
嵌入复用规范轨 bge-large-zh-v1.5（``index.vector_index.embed_texts``）；建库见 ``cost.bill_index``。
"""
from __future__ import annotations

from config import COST_BILL_COLLECTION, DEFAULTS

_OUTPUT_FIELDS = ["code", "name", "unit", "feature", "chapter", "doc_id", "spec_version"]


def _shape_hits(hits: list) -> list[dict]:
    """把单次 Milvus search 的命中列表整形为候选 dict（纯函数，便于单测）。

    参数：hits —— pymilvus search 返回的单 query 命中序列，每个 hit 含 ``entity``（dict）+ ``distance``。
    返回：list[dict]，每项 = entity 字段 + ``score``（COSINE 相似度，越大越相关）。
    """
    candidates: list[dict] = []
    for hit in hits:
        entity = hit.get("entity", hit)
        cand = {f: entity.get(f) for f in _OUTPUT_FIELDS}
        cand["score"] = round(float(hit.get("distance", 0.0)), 4)
        candidates.append(cand)
    return candidates


def search_bill(
    query: str,
    top_k: int = 10,
    collection_name: str = COST_BILL_COLLECTION,
    milvus_host: str = DEFAULTS["milvus_host"],
    milvus_port: int = DEFAULTS["milvus_port"],
    embed_url: str = DEFAULTS["embed_url"],
    embed_model_id: str = DEFAULTS["embed_model_id"],
) -> list[dict]:
    """构件描述 → 清单候选（dense 向量召回）。

    参数：
        query (str): 构件/做法的自然语言描述（如「C30 现浇钢筋混凝土矩形柱」）。
        top_k (int): 返回候选数。
        collection_name (str): bill_spec_kb collection 名。
        milvus_host/milvus_port/embed_url/embed_model_id: Milvus 与嵌入服务参数。
    返回：
        list[dict]: 候选清单项（code/name/unit/feature/chapter/doc_id/spec_version + score），按相似度降序；
        collection 不存在抛 ValueError（语义化「索引未就绪」，由端点映射 503）。
    """
    from pymilvus import MilvusClient

    from index.vector_index import embed_texts

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    if not client.has_collection(collection_name):
        raise ValueError(f"清单向量库 {collection_name} 未就绪（先 cost.bill_index 建库）")

    vector = embed_texts([query], embed_url, embed_model_id, 1)[0]
    results = client.search(
        collection_name=collection_name,
        data=[vector],
        limit=top_k,
        output_fields=_OUTPUT_FIELDS,
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
    )
    return _shape_hits(results[0]) if results else []
