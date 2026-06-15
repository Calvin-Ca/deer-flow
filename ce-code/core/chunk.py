"""Chunk IR —— 切分层（splitter/）产物 = 规范语义树的一个节点 = **单一真值**。

替代旧 ``schema.Node``（TypedDict）为显式 ``@dataclass``。承载文档原生目录还原出的层级
结构（parent/child）、建树时一次算定的「固有事实」（引用图 / 祖先链）、溯源指针
（provenance）。**粒度视图与各检索索引都是它的派生**（index 层 ``view(chunks, granularity)``
选层 emit，不切树）。表征（``features``）**不挂 Chunk**——结构真值与语义投影分离，由表征层产成
sidecar（``node_path → {kind: ChunkFeature}``）于索引期现算、不落 chunks.json（见 core.feature）。

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

# 节点**种类**（kind）不落字段：容器/叶是纯派生事实——「有子节点（children_ids 非空）= 容器，
# 不单独 emit 检索单元、靠 small-to-big 回补；无子 = 叶·检索单元，粒度视图 emit 这层」。
# 消费方直接判 children_ids 空否（见 index.manager.view），勿存冗余 chunk_type 字段。
# 树**深度**同理派生（= len(ancestor_paths)+1），亦不落字段。
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
    # 种类（容器/叶）与深度均不落字段，为纯派生：容器 ⟺ children_ids 非空；深度 = len(ancestor_paths)+1。
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

    # -- 派生 --------------------------------------------------------------

    @property
    def chunk_id(self) -> str:
        """chunk 身份别名（``chunk_id ≡ node_path``）。"""
        return self.node_path

    def is_grounded(self) -> bool:
        """是否「已接地」= provenance 有原始块（block_idx 非空）。

        未接地空骨架（目录列了条目但正文从未抽到块）content 空、对检索是死单元；
        index 层 view 据此剔除「未接地的无子节点（叶）」（见 index/manager）。
        """
        return bool(self.provenance.block_idx)

    def expandable_refs(self) -> list[str]:
        """可正向扩展的目标 path 列表（strong / cross_standard）。"""
        return [r.to for r in self.references
                if r.to and r.type in EXPANDABLE_REF_TYPES]

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict:
        """JSON 友好 dict（落 chunks.json）。references/provenance 递归展开（表征不落盘）。"""
        return {
            "node_path": self.node_path,
            "standard_id": self.standard_id,
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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        """从 chunks.json 的一条还原 Chunk（缺字段取默认）。"""
        return cls(
            node_path=d.get("node_path", ""),
            standard_id=d.get("standard_id", ""),
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
        )
