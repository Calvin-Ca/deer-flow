"""ChunkFeature IR —— 表征层（feature/）的产物：一个 Chunk 的「一种可被检索的样子」。

替代旧 ``schema.Representation``（dict/TypedDict）为显式 ``@dataclass``。一个 ChunkFeature =
一个语义投影面（原文 / 向量 / 稀疏词项 / 摘要 / 语气 / 条件 …）；检索是多投影的可组合并集。
表征**不挂 Chunk**（结构真值与语义投影分离，不落 chunks.json）——表征层逐个产出，汇成
**sidecar** ``FeatureSidecar``（``node_path → {kind: ChunkFeature}``），由编排层对检索单元现算、
沿索引链路传递给各索引器消费（见 ``feature.pipeline.build_features`` / ``feature_text``）。

向量归属：``dense`` / ``context_aug`` 只产**待嵌入文本**（``text``），向量（``vector``）由索引期
（``index/``）用 embedding 模型统一计算——模型唯一 owner 在检索栈，表征层不加载模型。

本模块**不 import chunk**（避免循环）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 表征种类（旧 schema.ReprKind）。免费 4 项 + 规则/LLM 扩展项（后续批次占位）。
FeatureKind = Literal[
    "raw", "dense", "sparse", "context_aug",
    "table_struct", "modal", "condition", "summary", "questions",
    "keyword", "graph",
]

# 语气受控词表（modal 表征用）。纯语言学情态，**无任何法律含义**——只是可选召回/过滤通道。
Modal = Literal["必须", "严禁", "不应", "不得", "应", "宜", "可", ""]

ScopeStatus = Literal["extracted", "unknown"]


@dataclass
class ChunkFeature:
    """一个 Chunk 的单个表征（语义投影）。

    字段（按 ``kind`` 取用，未用字段留默认）：
        kind   表征类型（FeatureKind）；与 sidecar 的内层键一致，冗余存便于审计。
        text   进 embedding / BM25 的文本形态（raw / sparse / context_aug / summary / questions）。
        vector dense 向量（dense / context_aug / summary / questions；索引期填）。
        data   结构化载荷（table_struct→表体、condition→谓词、modal→{"all":[...]}）。
        meta   审计/附属信息（来源、置信度等）。
    """

    kind: str
    text: str = ""
    vector: list[float] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON 友好 dict（省略空 vector，省体积）。"""
        out: dict = {"kind": self.kind}
        if self.text:
            out["text"] = self.text
        if self.vector:
            out["vector"] = self.vector
        if self.data:
            out["data"] = self.data
        if self.meta:
            out["meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "ChunkFeature":
        """从 dict 还原（缺字段取默认）。"""
        return cls(
            kind=d.get("kind", ""),
            text=d.get("text", ""),
            vector=list(d.get("vector") or []),
            data=dict(d.get("data") or {}),
            meta=dict(d.get("meta") or {}),
        )


# 表征 sidecar（表征不挂 Chunk，独立成图沿索引链路传递）：
#   FeatureMap     单节点表征：kind → ChunkFeature
#   FeatureSidecar 全量：node_path → FeatureMap
FeatureMap = dict[str, ChunkFeature]
FeatureSidecar = dict[str, FeatureMap]


def feature_text(feats: FeatureMap, kind: str, fallback: str = "") -> str:
    """从单节点表征字典取某表征文本（缺失 / 空文本则回退 fallback）。

    承旧 ``Chunk.feature_text``——表征移出 Chunk 后，改作用于 sidecar 的单节点 FeatureMap。

    参数：
        feats (FeatureMap): 单节点表征字典（kind → ChunkFeature）。
        kind (str): 要取的表征种类。
        fallback (str): 缺失或文本为空时的回退值。
    返回：
        str: 该表征文本，或 fallback。
    """
    f = feats.get(kind)
    return f.text if (f is not None and f.text) else fallback
