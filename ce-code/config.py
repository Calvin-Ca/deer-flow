"""共享运行配置（层中立）—— 依赖服务地址 / 规范代号别名 / store 目录 + collection 命名。

承旧 ``retrieval/config.py``：把散落在 server / build / eval 的默认配置、规范别名、store 目录与
Milvus collection 命名规则收口一处（重构前被复制三四份，极易漂移）。**取值与旧版逐字一致**，
保证行为不变。index / retrieval / service 各层共享 import（从 ce-code 根：``import config``）。
"""
from __future__ import annotations

import os
import re
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
# cross-encoder 精排所用 GPU——**单卡**。新版 FlagEmbedding 的 FlagReranker 默认吃光所有可见 GPU、
# 每次 compute_score 跨多卡 spawn 多进程池（起池就 ~21s），给几十个候选重排纯属灾难性 overhead。
# 钉死单卡 → 走单进程、不起池，P95 从分钟级降回亚秒级。
# 默认 cuda:2（本机 0/1/3 被 vLLM 占满、仅 2 有空闲）；GPU 分配变动时用 env CE_RERANK_DEVICE 覆盖，
# 不必改代码。所选卡需 ≥~2G 空闲（bge-reranker-large fp16），否则 OOM→静默 fallback RRF（精排失效）。
RERANK_DEVICE = os.environ.get("CE_RERANK_DEVICE", "cuda:2")
# 精排已拆为独立 GPU 服务（service.rerank_api，默认 :8098），知识层检索栈改 HTTP 调用（同 embedding :8097
# 的做法）——把唯一的本地 GPU 消费者移出 :8100，知识服务本身即纯 CPU。RERANK_MODEL/RERANK_DEVICE 现由
# 该 rerank 服务进程读取；检索侧只认 RERANK_URL。服务不可达 → 检索栈静默 fallback RRF（精排失效、召回不变）。
RERANK_URL = os.environ.get("CE_RERANK_URL", "http://localhost:8095")
RERANK_TIMEOUT = float(os.environ.get("CE_RERANK_TIMEOUT", "30"))  # 留足冷启动首推理余量（默认 15 太紧）
EMBED_MODEL = "BAAI/bge-large-zh-v1.5"
EMBED_DIM = 1024  # bge-large-zh-v1.5 输出维度
COLLECTION_PREFIX = "building_code"

# 造价清单向量库（bill_spec → Milvus，供 /search/bill-match 构件→清单候选召回）。复用规范轨同一
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
        "bill_doc_ids": ["GB-50854", "GB-50856"],                # 建库源 doc_id（房建 + 安装）
        "bill_spec_versions": ["GB/T 50854-2024", "GB/T 50856-2024"],  # 房建 + 安装 清单
        "supports_compose": True,                                # 2024 有定额/价格/映射，组价可出数
    },
    "2013": {
        "label": "GB 50854-2013 房屋建筑与装饰（旧版）",
        "bill_collection": "cost_bill_spec_kb_2013",
        "bill_doc_ids": ["GB-50854-2013"],
        "bill_spec_versions": ["GB/T 50854-2013"],
        "supports_compose": True,   # 定额/信息价/费率是深圳现行地方标准、与清单国标版本解耦，2013 清单同样套现行 SJG，组价可出数；仅「同码不同义」码需人工消歧（见上）
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

# 规范代号别名 → vector_store 目录名（resolve_store_dir 查表；store 名 = build.py safe_std(standard_id)）。
# 防火轨保留历史条目；造价规范问答（Norm-QA，2026-06-22）新增计量计价 5 部规范——store 名取
# build 期 --standard-id 的 safe_std（连字符→下划线），与 data/structured/chunks/<dir> 同名。
STANDARD_ALIASES: dict[str, str] = {
    # ── 防火规范（历史，与旧 server.py 一致）──
    "gb50016": "GB_50016-20142018",
    "gb50016-2014": "GB_50016-20142018",
    "gb50016-20142018": "GB_50016-20142018",
    "GB_50016-20142018": "GB_50016-20142018",
    # ── 造价计量计价规范（Norm-QA，2013/2024 双版隔离，同 9 位码不同义靠版本号区分）──
    "gb50500-2013": "GB_50500_2013_建设工程工程量清单计价规范",
    "gb50500-2024": "GB_T50500_2024_建设工程工程量清单计价标准",
    "gb50854-2013": "GB_50854_2013_房屋建筑与装饰工程工程量计算规范",
    "gb50854-2024": "GB_T50854_2024_房屋建筑与装饰工程工程量计算标准",
    "gb50856-2024": "GB_T50856_2024_通用安装工程工程量计算标准",
}


def collection_name(store_name: str) -> str:
    """store 名 → Milvus collection 名（**ASCII 安全**：只含字母/数字/下划线）。

    Milvus collection 名不许非 ASCII，而造价规范 store 名含中文（如 GB_T50854_2024_房屋…）；故把
    非 [a-z0-9] 字符（含中文、连字符）整段折成单下划线再去首尾下划线——保留各规范 ASCII 前缀
    （GB_50854_2013 / GB_T50856_2024…）即可保证 per-标准唯一。纯 ASCII 名（如防火 GB_50016-20142018）
    结果与旧版逐字一致（无回归）。
    """
    slug = re.sub(r"[^a-z0-9]+", "_", store_name.lower()).strip("_")
    return f"{COLLECTION_PREFIX}_{slug}"


def resolve_store_dir(standard: str, vector_store_root: Path,
                      profile: str = "default") -> tuple[Path, str]:
    """规范代号 → (store 目录绝对路径, 规范化 store 名)。

    store 布局为 ``vector_store/<store_name>/<profile>/``（build.py 落盘位置）；本函数优先返回
    该嵌套目录，若不存在则回退扁平 ``vector_store/<store_name>/``（兼容防火 gb50016 等旧扁平索引）。
    未知代号抛 ``ValueError``；目录是否就绪由调用方按需校验（便于给出「索引未就绪」503）。
    """
    store_name = STANDARD_ALIASES.get(standard) or STANDARD_ALIASES.get(standard.lower())
    if not store_name:
        raise ValueError(
            f"未知规范代号: {standard!r}，支持: {sorted(set(STANDARD_ALIASES))}"
        )
    base = Path(vector_store_root) / store_name
    nested = base / profile
    return (nested if nested.exists() else base), store_name
