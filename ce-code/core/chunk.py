"""Chunk IR —— 切分层（splitter/）产物 = 规范语义树的一个节点 = **单一真值**。

替代旧 ``schema.Node``（TypedDict）为显式 ``@dataclass``。承载文档原生目录还原出的层级
结构（parent/child）、建树时一次算定的「固有事实」（引用图 / 祖先链）、溯源指针
（provenance），以及表征层挂的多种「语义投影」（``features``）。**粒度视图与各检索索引
都是它的派生**（index 层 ``view(chunks, granularity)`` 选层 emit，不切树）。

身份约定（承旧设计）：``node_path`` 即 chunk 身份（``chunk_id ≡ node_path``，见 ``chunk_id``
属性别名）——本集合（单规范）内唯一，直接作树边 / 引用边的引用键；**保留 node_path 字段名**
以维持对外 HTTP 契约（``/clause/{standard}/{path}``、``/search`` 返回字段）与「结构地址 = id」
语义，不重命名为 chunk_id。消费方**勿假定恒为数字号**（亦可为「附录E」/「前言」等标题路径）。

设计转向（2026-06-12）：**无强条 / 法律强制机制**——语气（应/宜/可/严禁）降级为 ``modal``
表征（core.feature），只作可选召回通道，不做全局置顶排序。

溯源：MinerU 原始内容留在阶段 0 缓存 ``data/parsed/<std>/auto/*_content_list.json``（不可变），
本 Chunk 靠 ``provenance.block_idx`` 回指原始块；``block_idx == []`` 即「未接地空骨架」
（目录列了条目但正文未抽到块）的**单一真值**（见 ``is_grounded``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.feature import ChunkFeature

# 节点**种类**（kind），**与深度正交**：层级深度由 level 单独承载，chunk_type 不编码"第几层"，
# 也不由 node_path 派生。建树末按**树结构**二分（纯看"有无子节点"）：
#   container  有子节点的容器（章 / 节 / 附录根；不单独 emit 检索单元，靠 small-to-big 回补）
#   leaf       叶·检索单元（条款 / 总则 / 无编号正文段 / 附录条款；粒度视图 emit 这层）
# 其余为契约占位（建树阶段未产出）。
ChunkType = Literal[
    "container", "leaf",
    "document", "paragraph", "table", "figure", "formula",
]
RefType = Literal["strong", "weak", "exclude", "cross_standard"]

# 引用图正向扩展白名单：strong / cross_standard 才参与「命中 A 自动拉 B」；weak 可选、
# exclude 禁止正向扩展。索引/检索层据此判断（index._expandable_refs / hybrid 扩展）。
EXPANDABLE_REF_TYPES: frozenset[str] = frozenset({"strong", "cross_standard"})


@dataclass
class Reference:
    """引用边（分型 + 方向）—— 引用图（GraphRAG 底座）的一条边。

    字段：
        to   被引目标：本规范条款号 "5.2.1"，或跨规范 "GB 50116-2013"。
        type RefType：strong(应符合·必拉) / weak(参见·可选) / exclude(禁止扩展) /
             cross_standard(跨规范召回)。
    """

    to: str
    type: str = "strong"

    def to_dict(self) -> dict:
        return {"to": self.to, "type": self.type}

    @classmethod
    def from_dict(cls, d: dict) -> "Reference":
        return cls(to=d.get("to", ""), type=d.get("type", "strong"))


@dataclass
class Provenance:
    """节点溯源 —— 回指构成本节点的 MinerU 原始块（阶段 0 缓存不可变）。

    字段：
        source_file MinerU content_list.json 路径（相对 data/parsed/）。
        block_idx   本节点聚合的原始块下标列表；**为 [] 即"未接地空骨架"**（无真身可溯）。
        page        涉及页码（1-base）。
        bbox        可选版面框 [{"page": int, "box": [...]}]，供 PDF 高亮。
    """

    source_file: str = ""
    block_idx: list[int] = field(default_factory=list)
    page: list[int] = field(default_factory=list)
    bbox: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {"source_file": self.source_file,
                     "block_idx": self.block_idx, "page": self.page}
        if self.bbox:
            out["bbox"] = self.bbox
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        return cls(
            source_file=d.get("source_file", ""),
            block_idx=list(d.get("block_idx") or []),
            page=list(d.get("page") or []),
            bbox=list(d.get("bbox") or []),
        )


@dataclass
class Chunk:
    """规范语义树的一个节点（单一真值）。字段分组见模块 docstring。"""

    # ── 标识 ──
    node_path: str
    standard_id: str = ""
    # ── 结构（树形）──
    chunk_type: str = "leaf"          # ChunkType：container（有子）/ leaf（无子·检索单元）
    level: int = 0                    # 还原出的目录树深度（1-base，根=1；沿 parent 上溯算定）
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    title: str = ""
    content: str = ""                 # 仅自身正文（不含子节点），作检索载荷
    # ── 固有事实（建树时一次算定）──
    ancestor_titles: list[str] = field(default_factory=list)
    ancestor_paths: list[str] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    referenced_by: list[str] = field(default_factory=list)
    # ── 结构层审计 ──
    node_path_source: str = ""        # number / text_level / inherited / lexicon（后两者占位）
    node_path_confidence: float = 1.0
    # ── 溯源 ──
    provenance: Provenance = field(default_factory=Provenance)
    # ── 表格/图示（过渡：待表征层转 table_struct 等）──
    tables: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    # ── 表征层 ──
    features: dict[str, ChunkFeature] = field(default_factory=dict)

    # -- 派生 --------------------------------------------------------------

    @property
    def chunk_id(self) -> str:
        """chunk 身份别名（``chunk_id ≡ node_path``）。"""
        return self.node_path

    def is_grounded(self) -> bool:
        """是否「已接地」= provenance 有原始块（block_idx 非空）。

        未接地空骨架（目录列了条目但正文从未抽到块）content 空、对检索是死单元；
        index 层 view 据此剔除「未接地 leaf」（见 index/manager）。
        """
        return bool(self.provenance.block_idx)

    def feature_text(self, kind: str, fallback: str = "") -> str:
        """取某表征的文本（缺失回退）。"""
        f = self.features.get(kind)
        return f.text if (f is not None and f.text) else fallback

    def expandable_refs(self) -> list[str]:
        """可正向扩展的目标 path 列表（strong / cross_standard）。"""
        return [r.to for r in self.references
                if r.to and r.type in EXPANDABLE_REF_TYPES]

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict:
        """JSON 友好 dict（落 chunks.json）。features/references/provenance 递归展开。"""
        return {
            "node_path": self.node_path,
            "standard_id": self.standard_id,
            "chunk_type": self.chunk_type,
            "level": self.level,
            "parent_id": self.parent_id,
            "children_ids": self.children_ids,
            "title": self.title,
            "content": self.content,
            "ancestor_titles": self.ancestor_titles,
            "ancestor_paths": self.ancestor_paths,
            "references": [r.to_dict() for r in self.references],
            "referenced_by": self.referenced_by,
            "node_path_source": self.node_path_source,
            "node_path_confidence": self.node_path_confidence,
            "provenance": self.provenance.to_dict(),
            "tables": self.tables,
            "images": self.images,
            "features": {k: v.to_dict() for k, v in self.features.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        """从 chunks.json 的一条还原 Chunk（缺字段取默认）。"""
        return cls(
            node_path=d.get("node_path", ""),
            standard_id=d.get("standard_id", ""),
            chunk_type=d.get("chunk_type", "leaf"),
            level=int(d.get("level", 0)),
            parent_id=d.get("parent_id"),
            children_ids=list(d.get("children_ids") or []),
            title=d.get("title", ""),
            content=d.get("content", ""),
            ancestor_titles=list(d.get("ancestor_titles") or []),
            ancestor_paths=list(d.get("ancestor_paths") or []),
            references=[Reference.from_dict(r) for r in (d.get("references") or [])],
            referenced_by=list(d.get("referenced_by") or []),
            node_path_source=d.get("node_path_source", ""),
            node_path_confidence=float(d.get("node_path_confidence", 1.0)),
            provenance=Provenance.from_dict(d.get("provenance") or {}),
            tables=list(d.get("tables") or []),
            images=list(d.get("images") or []),
            features={k: ChunkFeature.from_dict(v)
                      for k, v in (d.get("features") or {}).items()},
        )
