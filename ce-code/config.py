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

# ── 国标版本严格隔离 ─────────────────────────────────────────────────────────
# 2013 与 2024 两套清单计量国标**同 9 位码不同义**（如 011702006：2013=矩形梁模板 / 2024 体系不同；
# 010502001 现浇矩形柱两版编码虽同但口径有别），混用会串库。故造价知识库调用须**显式指定国标版本**
# （spec ∈ {"2013","2024"}），按下表路由到对应 Milvus collection / PG 过滤（spec_version 集）。
# 任务层 CostAgent 调用前提示用户输入所用国标版本，再透传 spec。背景见 notebooks E6–E9。
SPEC_REGISTRY: dict[str, dict] = {
    "2024": {
        "label": "GB/T 50854-2024 房屋建筑与装饰（现行）",
        "bill_collection": COST_BILL_COLLECTION,                 # cost_bill_spec_kb
        "bill_spec_versions": ["GB/T 50854-2024", "GB/T 50856-2024"],  # 房建 + 安装 清单
        "supports_compose": True,                                # 2024 有定额/价格/映射，组价可出数
    },
    "2013": {
        "label": "GB 50854-2013 房屋建筑与装饰（旧版）",
        "bill_collection": "cost_bill_spec_kb_2013",
        "bill_spec_versions": ["GB/T 50854-2013"],
        "supports_compose": False,  # 2013 组价数据（定额/价格/清单→定额映射）未就绪，见 notebooks/BACKLOG Phase 2
    },
}


def resolve_spec(spec: str) -> dict:
    """国标版本号（"2013"/"2024"）→ SPEC_REGISTRY 配置。

    **spec 必填、无默认**：缺省或未知都抛 ValueError（逼调用方显式选版本，避免错版静默串库）。
    参数：spec —— 国标版本号字符串。
    返回：该版本的路由配置 dict（bill_collection / bill_spec_versions / supports_compose / label）。
    """
    if not spec:
        raise ValueError(
            f"须指定国标版本 spec（{sorted(SPEC_REGISTRY)}）——调用造价知识库前请确认所用国标版本（2013/2024）"
        )
    cfg = SPEC_REGISTRY.get(str(spec).strip())
    if cfg is None:
        raise ValueError(f"未知国标版本 spec={spec!r}，支持: {sorted(SPEC_REGISTRY)}")
    return cfg

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
