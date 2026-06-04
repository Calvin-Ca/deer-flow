"""retrieval 配置与路径解析（检索引擎共享）。

这里集中原先散落在 ``service/server.py`` 与 ``scripts/05/06/10`` 各处的
默认配置、规范代号别名、store 目录 / Milvus collection 命名规则——重构前它们
被复制了三四份，极易漂移。**取值与重构前的 server.py 完全一致，保证行为不变。**
"""
from __future__ import annotations

from pathlib import Path

# ── 依赖服务默认地址（与 server.py DEFAULTS 一致）─────────────────────────────
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
COLLECTION_PREFIX = "building_code"

# 规范代号别名 → vector_store 目录名（与重构前 server.py STANDARD_ALIASES 完全一致）
STANDARD_ALIASES: dict[str, str] = {
    "gb50016": "GB_50016-20142018",
    "gb50016-2014": "GB_50016-20142018",
    "gb50016-20142018": "GB_50016-20142018",
    "GB_50016-20142018": "GB_50016-20142018",
}


def collection_name(store_name: str) -> str:
    """store 目录名 → Milvus collection 名（只含字母/数字/下划线）。

    与重构前 server.py ``_collection_name`` / 05 CLI 推断逻辑一致。
    """
    return f"{COLLECTION_PREFIX}_{store_name}".lower().replace("-", "_")


def resolve_store_dir(standard: str, vector_store_root: Path) -> tuple[Path, str]:
    """规范代号 → (store 目录绝对路径, 规范化 store 名)。

    仅解析路径，不检查目录是否存在（由调用方按需校验，便于服务端给出
    "索引未就绪" 这类语义化错误）。未知代号抛 ``ValueError``。
    """
    store_name = STANDARD_ALIASES.get(standard) or STANDARD_ALIASES.get(standard.lower())
    if not store_name:
        raise ValueError(
            f"未知规范代号: {standard!r}，支持: {sorted(set(STANDARD_ALIASES))}"
        )
    return Path(vector_store_root) / store_name, store_name
