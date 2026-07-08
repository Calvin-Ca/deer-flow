"""构件→清单候选召回 —— /search/bill-match 取数原语（dense 向量检索）。

输入构件/做法的自然语言描述，嵌入后在 ``bill_spec_kb`` Milvus collection 做 COSINE 检索，返回
top_k 清单项候选 + 出处。**知识层只负责召回候选**；在候选内决策选码（LLM）归任务层 CostAgent
（红线：只建议不定稿，HITL 复核）。KG 约束（清单↔定额覆盖、章节对齐收窄候选）为后续增强项。

与 ``cost.query``（PG 只读取数）分层：本文件走 Milvus + embedding（与 PG 依赖隔离），故单列一文件。
嵌入复用 bge-large-zh-v1.5（``cost.embed.embed_texts``）；建库见 ``cost.bill_index``。
"""
from __future__ import annotations

from config import COST_BILL_COLLECTION, DEFAULTS

_OUTPUT_FIELDS = ["code", "name", "unit", "feature", "chapter", "doc_id", "spec_version", "cast_type"]

# 候选池：dense 先多召回这么多条，供 structural 重排后截到 top_k（实测 gold 均在 dense 前列，池给足即可）。
CANDIDATE_POOL = 30

# 结构约束：附属/措施项类型标记。这些 token 出现在清单名里 = 该项是「本体之外的附属/措施项」
# （模板/钢筋/脚手架…各自独立成清单项）。查询未提及对应标记 → 该候选与查询意图错位、应压到本体之后。
# 实测修「现浇混凝土圈梁→圈梁模板」「矩形柱→柱钢筋」这类 dense 错排（替代劣化的 reranker，见 E4）。
STRUCTURAL_MARKERS = ("模板", "钢筋", "脚手架", "支撑", "支架", "拆除", "泵送", "超高")

# 现浇/预制消歧：预制柱(010509 矩形柱)与现浇柱(010502 矩形柱)同名、索引 chapter 同，dense 无从区分。
# 建库期已从 caption 派生 cast_type；查询未提及「预制/装配」→ 该预制候选与意图错位、下压到现浇之后
# （房建 BIM 构件默认现浇）。见 notebooks E6。
PREFAB_WORDS = ("预制", "装配")


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


def _type_penalty(query: str, name: str) -> int:
    """候选名相对查询的「附属/措施类型错位」罚分（纯函数，便于单测）。

    罚 = 候选名里**查询未提及**的 STRUCTURAL_MARKERS 个数。「钢筋混凝土」是材料词、非「要钢筋项」
    的意图，故查询与候选名都先把「钢筋混凝土」归一为「混凝土」再判「钢筋」标记，避免误罚本体。

    参数：query —— 构件描述；name —— 候选清单名。
    返回：int 罚分（0 = 类型对齐，越大越错位）。
    """
    q = (query or "").replace("钢筋混凝土", "混凝土")
    nm = (name or "").replace("钢筋混凝土", "混凝土")
    return sum(1 for m in STRUCTURAL_MARKERS if m in nm and m not in q)


def _prefab_penalty(query: str, cand: dict) -> int:
    """预制/装配候选相对查询的错位罚分（纯函数，便于单测）。

    候选 cast_type 为「预制/装配」而查询未提及 → 罚 1（房建默认现浇，预制需 query 明示）。
    依赖建库期派生的 ``cast_type`` 字段（见 cost.bill_index.cast_type）。

    参数：query —— 构件描述；cand —— 候选 dict（含 cast_type）。
    返回：int 罚分（0 = 对齐 / 候选非预制）。
    """
    cast = cand.get("cast_type") or ""
    if cast in PREFAB_WORDS and not any(w in (query or "") for w in PREFAB_WORDS):
        return 1
    return 0


def _structural_reorder(query: str, candidates: list[dict]) -> list[dict]:
    """按类型对齐罚分**稳定重排**候选：罚分低者靠前，同罚分保持原(dense/rerank)序（纯函数，便于单测）。

    罚分 = 附属/措施类型错位（_type_penalty，模板/钢筋…）+ 现浇预制错位（_prefab_penalty）。只下压
    「查询没要的附属/措施/预制项」，对本体/同类候选零扰动——故是对 dense 的最小修正。
    给每条候选挂 ``type_penalty``/``prefab_penalty`` 字段供观测。
    """
    def _penalty(c: dict) -> int:
        return _type_penalty(query, c.get("name") or "") + _prefab_penalty(query, c)

    ranked = sorted(enumerate(candidates), key=lambda ic: (_penalty(ic[1]), ic[0]))
    out = []
    for _, c in ranked:
        c["type_penalty"] = _type_penalty(query, c.get("name") or "")
        c["prefab_penalty"] = _prefab_penalty(query, c)
        out.append(c)
    return out


def _prefix_filter(code_prefixes: list[str] | None) -> str:
    """把专业 code 前缀列表编成 Milvus 布尔过滤表达式（纯函数，便于单测）。

    清单 9 位码前 2 位 = 专业域（01 建筑/03 安装/04 市政/05 园林…）。房建项目只在相关专业内
    匹配，剔除跨专业噪声（实测全专业库里建筑查询被拽到市政「砌筑方沟」/园林「花架梁」）。

    参数：code_prefixes —— 专业前缀列表（如 ['01','03']）；None/空 → 不过滤。
    返回：Milvus filter 表达式（如 ``code like "01%" or code like "03%"``）；无前缀返回空串。
    """
    prefixes = [p.strip() for p in (code_prefixes or []) if p and p.strip()]
    return " or ".join(f'code like "{p}%"' for p in prefixes)


def search_bill(
    query: str,
    top_k: int = 10,
    collection_name: str = COST_BILL_COLLECTION,
    structural: bool = True,
    candidate_pool: int = CANDIDATE_POOL,
    code_prefixes: list[str] | None = None,
    milvus_host: str = DEFAULTS["milvus_host"],
    milvus_port: int = DEFAULTS["milvus_port"],
    embed_url: str = DEFAULTS["embed_url"],
    embed_model_id: str = DEFAULTS["embed_model_id"],
) -> list[dict]:
    """构件描述 → 清单候选（dense 召回 + 结构约束重排）。

    流程：embedding(query) → Milvus COSINE 召回候选池（structural 时取 ``max(top_k, candidate_pool)`` 条，
    否则 top_k）→ structural 稳定重排（按附属/措施类型 + 现浇预制对齐下压查询没要的项）→ 截 top_k。

    > cross-encoder 精排（rerank）2026-06-17 实测在「构件描述 × 极短清单名」上劣化 dense（E4），已随
    > 规范条文检索 RAG 一并移除；保留确定性的 structural 重排。

    参数：
        query (str): 构件/做法的自然语言描述（如「C30 现浇钢筋混凝土矩形柱」）。
        top_k (int): 返回候选数。
        collection_name (str): bill_spec_kb collection 名（按国标版本路由，见 config.SPEC_REGISTRY）。
        structural (bool): 是否结构约束重排（**默认 True**）——按附属/措施类型 + 现浇预制对齐稳定下压
            查询没要的模板/钢筋/预制项（确定性、对本体零扰动；见 E4/E9）。
        candidate_pool (int): structural 重排前 dense 召回的候选池大小。
        code_prefixes (list[str] | None): 专业 code 前缀过滤（如 ['01','03'] 只在建筑+安装内召回）；
            None=全专业。剔除跨专业噪声（全专业库里建筑查询会被市政/园林项挤出 top-k，见 eval 2013）。
        milvus_host/milvus_port/embed_url/embed_model_id: Milvus 与嵌入服务参数。
    返回：
        list[dict]: 候选清单项（code/name/unit/feature/chapter/doc_id/spec_version/cast_type + score），
        按最终排序降序；collection 不存在抛 ValueError（语义化「索引未就绪」，由端点映射 503）。
    """
    from pymilvus import MilvusClient

    from cost.embed import embed_texts

    client = MilvusClient(uri=f"http://{milvus_host}:{milvus_port}")
    if not client.has_collection(collection_name):
        raise ValueError(f"清单向量库 {collection_name} 未就绪（先 cost.bill_index 建库）")

    vector = embed_texts([query], embed_url, embed_model_id, 1)[0]
    # structural 需多召回候选池再重排，末尾统一截 top_k
    pool_limit = max(top_k, candidate_pool) if structural else top_k
    search_kwargs = {
        "collection_name": collection_name,
        "data": [vector],
        "limit": pool_limit,
        "output_fields": _OUTPUT_FIELDS,
        "search_params": {"metric_type": "COSINE", "params": {"ef": 64}},
    }
    expr = _prefix_filter(code_prefixes)            # 专业域收窄（剔跨专业噪声）
    if expr:
        search_kwargs["filter"] = expr
    results = client.search(**search_kwargs)
    candidates = _shape_hits(results[0]) if results else []
    if structural and candidates:
        candidates = _structural_reorder(query, candidates)        # 类型/现浇预制对齐稳定重排
    return candidates[:top_k]
