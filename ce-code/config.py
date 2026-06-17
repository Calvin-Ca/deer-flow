"""共享运行配置（层中立）—— 依赖服务地址 / 规范代号别名 / store 目录 + collection 命名。

承旧 ``retrieval/config.py``：把散落在 server / build / eval 的默认配置、规范别名、store 目录与
Milvus collection 命名规则收口一处（重构前被复制三四份，极易漂移）。**取值与旧版逐字一致**，
保证行为不变。index / retrieval / service 各层共享 import（从 ce-code 根：``import config``）。
"""
from __future__ import annotations

from pathlib import Path

# ── 依赖服务默认地址 ─────────────────────────────────────────────────────────
DEFAULTS: dict = {
    "milvus_host": "localhost",
    "milvus_port": 19530,
    "embed_url": "http://localhost:8097",
    "embed_model_id": "/model",
    "llm_url": "http://localhost:8099",
    "llm_model_id": "qwen3-8b",
    "top_k": 20,
}

RERANK_MODEL = "BAAI/bge-reranker-large"
EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度
COLLECTION_PREFIX = "building_code"

# 造价清单向量库（bill_spec → Milvus，供 /bill/match 构件→清单候选召回）。复用规范轨同一
# embedding（bge-large-zh-v1.5 @ embed_url, dim 1024），不新部署服务；BGE-M3 sparse 混检留后续。
COST_BILL_COLLECTION = "cost_bill_spec_kb"

# 规范代号别名 → vector_store 目录名（与旧 server.py STANDARD_ALIASES 完全一致）
STANDARD_ALIASES: dict[str, str] = {
    "gb50016": "GB_50016-20142018",
    "gb50016-2014": "GB_50016-20142018",
    "gb50016-20142018": "GB_50016-20142018",
    "GB_50016-20142018": "GB_50016-20142018",
}


def collection_name(store_name: str) -> str:
    """store 目录名 → Milvus collection 名（只含字母/数字/下划线）。与旧版推断逻辑一致。"""
    return f"{COLLECTION_PREFIX}_{store_name}".lower().replace("-", "_")


def resolve_store_dir(standard: str, vector_store_root: Path) -> tuple[Path, str]:
    """规范代号 → (store 目录绝对路径, 规范化 store 名)。

    仅解析路径，不检查目录是否存在（由调用方按需校验，便于服务端给出「索引未就绪」语义化错误）。
    未知代号抛 ``ValueError``。
    """
    store_name = STANDARD_ALIASES.get(standard) or STANDARD_ALIASES.get(standard.lower())
    if not store_name:
        raise ValueError(
            f"未知规范代号: {standard!r}，支持: {sorted(set(STANDARD_ALIASES))}"
        )
    return Path(vector_store_root) / store_name, store_name
