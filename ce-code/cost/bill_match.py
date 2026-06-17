"""构件→清单候选召回 —— /bill/match 取数原语（dense 向量检索）。

输入构件/做法的自然语言描述，嵌入后在 ``bill_spec_kb`` Milvus collection 做 COSINE 检索，返回
top_k 清单项候选 + 出处。**知识层只负责召回候选**；在候选内决策选码（LLM）归任务层 CostAgent
（红线：只建议不定稿，HITL 复核）。KG 约束（清单↔定额覆盖、章节对齐收窄候选）为后续增强项。

与 ``cost.query``（PG 只读取数）分层：本文件走 Milvus + embedding（与 PG 依赖隔离），故单列一文件。
嵌入复用规范轨 bge-large-zh-v1.5（``index.vector_index.embed_texts``）；建库见 ``cost.bill_index``。
"""
from __future__ import annotations

from config import COST_BILL_COLLECTION, DEFAULTS
from cost.bill_index import bill_embed_text

_OUTPUT_FIELDS = ["code", "name", "unit", "feature", "chapter", "doc_id", "spec_version"]

# rerank 候选池：dense 先多召回这么多条，再 cross-encoder 精排截到 top_k（实测 gold 均在 dense 前 3，
# 池给足即可。reranker 复用规范轨进程内单例，模型只加载一份，见 DEV §1「rerank 唯一 owner」）。
RERANK_POOL = 30


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


def _rerank_text(cand: dict) -> str:
    """候选清单项 → cross-encoder 配对文本（纯函数，便于单测）。

    与建库嵌入文本同构（``清单名。特征。章节``，复用 ``bill_embed_text``），让 query 与候选在
    同一表征语义空间配对打分。候选的 ``feature`` 是 "/"-拼接串，还原为列表喂入。
    """
    feats = cand.get("feature") or ""
    return bill_embed_text(cand.get("name") or "", feats.split("/") if feats else [], cand.get("chapter"))


def _rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """cross-encoder 精排 dense 候选（复用规范轨 reranker 单例）；不可用/失败时回退 dense 顺序。

    参数：query —— 构件描述；candidates —— dense 候选（含 name/feature/chapter）；top_k —— 截断数。
    返回：重排后前 top_k 候选，每条加 ``rerank_score``（cross-encoder 归一分）。
    """
    from retrieval.hybrid_retriever import _get_reranker

    reranker = _get_reranker()
    if reranker is None:                                   # 模型加载失败 → 退 dense 顺序
        return candidates[:top_k]
    try:
        pairs = [[query, _rerank_text(c)] for c in candidates]
        scores = reranker.compute_score(pairs, normalize=True)
        if isinstance(scores, float):                     # 单候选时 FlagReranker 返回标量
            scores = [scores]
        for c, s in zip(candidates, scores):
            c["rerank_score"] = round(float(s), 4)
        candidates = sorted(candidates, key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    except Exception as e:                                 # 打分异常不致命，退 dense 顺序
        print(f"[bill_match] rerank 打分失败（{e}），回退 dense 顺序")
    return candidates[:top_k]


def search_bill(
    query: str,
    top_k: int = 10,
    collection_name: str = COST_BILL_COLLECTION,
    rerank: bool = True,
    rerank_pool: int = RERANK_POOL,
    milvus_host: str = DEFAULTS["milvus_host"],
    milvus_port: int = DEFAULTS["milvus_port"],
    embed_url: str = DEFAULTS["embed_url"],
    embed_model_id: str = DEFAULTS["embed_model_id"],
) -> list[dict]:
    """构件描述 → 清单候选（dense 召回 + cross-encoder 精排）。

    流程：embedding(query) → Milvus COSINE 召回候选池（rerank 时取 ``max(top_k, rerank_pool)`` 条，
    否则 top_k）→ 开启 rerank 则 cross-encoder 重排截 top_k（实测把 Top-1 不稳的「本体 vs 模板」「部位词」
    类错排压下去；reranker 不可用时自动回退 dense 顺序，不致命）。

    参数：
        query (str): 构件/做法的自然语言描述（如「C30 现浇钢筋混凝土矩形柱」）。
        top_k (int): 返回候选数。
        collection_name (str): bill_spec_kb collection 名。
        rerank (bool): 是否 cross-encoder 精排（默认 True；评测对比可关）。
        rerank_pool (int): rerank 前 dense 召回的候选池大小。
        milvus_host/milvus_port/embed_url/embed_model_id: Milvus 与嵌入服务参数。
    返回：
        list[dict]: 候选清单项（code/name/unit/feature/chapter/doc_id/spec_version + score[+rerank_score]），
        按最终排序降序；collection 不存在抛 ValueError（语义化「索引未就绪」，由端点映射 503）。
    """
    from pymilvus import MilvusClient

    from index.vector_index import embed_texts

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    if not client.has_collection(collection_name):
        raise ValueError(f"清单向量库 {collection_name} 未就绪（先 cost.bill_index 建库）")

    vector = embed_texts([query], embed_url, embed_model_id, 1)[0]
    dense_limit = max(top_k, rerank_pool) if rerank else top_k
    results = client.search(
        collection_name=collection_name,
        data=[vector],
        limit=dense_limit,
        output_fields=_OUTPUT_FIELDS,
        search_params={"metric_type": "COSINE", "params": {"ef": 64}},
    )
    candidates = _shape_hits(results[0]) if results else []
    if rerank and candidates:
        return _rerank(query, candidates, top_k)
    return candidates[:top_k]
