"""表征层运行核心 —— 注册表 + enrich（PRD §3.2 阶段 2）。

把 Chunk 树投影成多种「可被检索的样子」：每个表征是节点意义的一个投影面，检索是多表征的
可组合并集。``REGISTRY`` 把 ``FeatureKind`` 映射到**表征实例**（Feature 子类），可插拔、可开关、
可消融——profile 的 ``features`` 列表决定本次启用哪些。

免费 4 项（raw / sparse / dense / context_aug）默认注册并启用；keyword / graph 占位亦注册（启用即
显式 NotImplementedError）；table_struct / modal / condition / summary / questions 未建文件，
未注册的 kind 在 ``attach`` 里被安全跳过（前向兼容）。

入口：``build_features(chunks, enabled)`` 产 **sidecar**（``node_path → {kind: ChunkFeature}``）——
表征**不挂 Chunk**（结构真值与语义投影分离、不落 chunks.json），由编排层于索引期对检索单元现算、
沿索引链路传给各索引器消费（见 ``ir.feature.feature_text`` / ``index.manager.build_index``）。
"""
from __future__ import annotations

from ir.chunk import Chunk
from ir.feature import FeatureMap, FeatureSidecar
from ir.profile import DEFAULT_FEATURES
from feature.base import Feature
from feature.bm25 import Bm25Feature
from feature.context_aug import ContextAugFeature
from feature.dense import DenseFeature
from feature.graph import GraphFeature
from feature.keyword import KeywordFeature
from feature.raw import RawFeature

# FeatureKind → 表征实例。新增表征 = new 一个 Feature 子类并 register。
REGISTRY: dict[str, Feature] = {}


def register(feat: Feature) -> None:
    """登记一个表征实例（kind 重复则报错，避免静默覆盖）。"""
    if feat.kind in REGISTRY:
        raise ValueError(f"表征 kind={feat.kind!r} 已注册，勿重复登记")
    REGISTRY[feat.kind] = feat


# 注册免费 4 项（无状态、单例复用）+ 占位 2 项（启用即报错）。
for _f in (RawFeature(), Bm25Feature(), DenseFeature(), ContextAugFeature(),
           KeywordFeature(), GraphFeature()):
    register(_f)

# 缺省启用集 = profile 默认免费 4 项（**不含**占位的 keyword/graph）。
DEFAULT_ENABLED: tuple[str, ...] = tuple(DEFAULT_FEATURES)


def build_one(chunk: Chunk, enabled: list[str] | tuple[str, ...] | None = None) -> FeatureMap:
    """产单个 Chunk 的表征字典（按 enabled 选启用的表征；未注册 kind 跳过）——**不挂 Chunk**。

    参数：
        chunk (Chunk): 节点。
        enabled: 启用的 FeatureKind 列表；None → DEFAULT_ENABLED（免费 4 项）。
    返回：
        FeatureMap: kind → ChunkFeature（该节点的所有表征）。
    """
    enabled = enabled if enabled is not None else DEFAULT_ENABLED
    feats: FeatureMap = {}
    for kind in enabled:
        feat = REGISTRY.get(kind)
        if feat is None:
            continue  # table_struct/modal/condition/summary/questions 等未注册 → 后续批次补
        feats[kind] = feat.build(chunk)
    return feats


def build_features(
    chunks: list[Chunk], enabled: list[str] | tuple[str, ...] | None = None
) -> FeatureSidecar:
    """产表征 sidecar（阶段 2 运行核心）：node_path → {kind: ChunkFeature}，**不挂 Chunk**。

    参数：
        chunks (list[Chunk]): 节点列表（通常已是 view 选出的检索单元，按需算）。
        enabled: 启用的 FeatureKind 列表；None → DEFAULT_ENABLED。
    返回：
        FeatureSidecar: node_path → FeatureMap（各节点表征），供索引器消费。
    """
    return {c.node_path: build_one(c, enabled) for c in chunks}
