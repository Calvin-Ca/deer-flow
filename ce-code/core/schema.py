"""节点 schema —— 结构层 / 表征层 / 检索层的**单一真值契约**。

对应 PRD §3.1（节点树 + 多表征 + 粒度视图）。三层只认这里的字段定义：
- **结构层**产出节点树（保留 parent/child + 引用图/祖先链等「固有事实」），落
  ``data/structured/<std>/<profile>/nodes.json`` 作单一真值；
- **表征层**往每个节点挂多种「语义投影」（``reprs``，键为 ``ReprKind``）；
- **检索层**只读，按 ``index_granularity`` 在树上选粒度视图，向量 / BM25 / 引用图
  邻接都是节点树的派生产物，不再 ``json.loads`` 重解析。

设计转向（2026-06-12）：**废弃「强条 / 法律强制」整套机制**——无
``is_mandatory_clause``、无强条召回安全闸、无 v1 兼容桥。语气（应/宜/可/严禁）
降级为 ``modal`` 表征，只作可选召回通道，不做全局置顶排序（见 PRD §3.4）。

**溯源**：MinerU 原始内容原封不动留在阶段 0 缓存
``data/parsed/<std>/auto/*_content_list.json``（不可变·只跑一次）；本 schema 的节点
是其派生物，靠 ``Node.provenance`` 的 ``block_idx`` 回指原始块，使「改算法重派生、
核对低置信节点、PDF 高亮」都不必重跑 MinerU。
"""
from __future__ import annotations

from typing import Literal, TypedDict

# ── 受控词表 ───────────────────────────────────────────────────────────────────

# 结构层 node_type：前 5 个是目录层级节点，后 4 个是挂在条款下的内容/切片节点
NodeType = Literal[
    "document", "chapter", "section", "clause", "subitem", "appendix",
    "paragraph", "table", "figure", "formula",
]
RefType = Literal["strong", "weak", "exclude", "cross_standard"]
ScopeStatus = Literal["extracted", "unknown"]
NodeStatus = Literal["active", "superseded", "abolished"]

# 表征注册表的种类（PRD §3.1 多表征表）。免费表征 + 波3 LLM 表征(summary/questions)
ReprKind = Literal[
    "raw", "dense", "sparse", "context_aug",
    "table_struct", "modal", "condition", "summary", "questions",
]

# 语气受控词表（modal 表征用）。纯语言学情态，**无任何法律含义**——
# 它只是一个可选召回/过滤通道（query 带强制意图时启用），不参与排序。
Modal = Literal["必须", "严禁", "不应", "不得", "应", "宜", "可", ""]

# 引用图正向扩展白名单：strong / cross_standard 才参与「命中 A 自动拉 B」；
# weak 可选、exclude 禁止正向扩展。检索层据此判断，见 retrieval/engine.expand。
EXPANDABLE_REF_TYPES: frozenset[str] = frozenset({"strong", "cross_standard"})


# ── 子结构 ─────────────────────────────────────────────────────────────────────

class Reference(TypedDict):
    """引用边（分型 + 方向）。

    功能：表达「本节点引用了谁」，是引用图（GraphRAG 底座）的一条边。
    字段：
        to   被引目标：本规范条款号 "5.2.1"，或跨规范 "GB 50116-2013"。
        type RefType：strong(应符合·必拉) / weak(参见·可选) / exclude(禁止扩展) /
             cross_standard(跨规范召回)。
    """
    to: str
    type: RefType


class Condition(TypedDict):
    """适用范围里的单条结构化谓词（condition 表征用）。

    功能：把散文条件（"建筑高度大于 27m"）抽成可机器判定的三元组，供条件召回。
    字段：
        field 字段名，如 height_m / underground / building_type / area_m2。
        op    比较算子：> >= < <= == != in。
        value 阈值/取值。
    """
    field: str
    op: str
    value: object


class ConditionData(TypedDict):
    """``condition`` 表征的结构化载荷（适用范围）。

    功能：节点的适用范围谓词集合，供算量/审图/合规做条件匹配召回。
    字段：
        building_types  适用建筑类型列表。
        height_range_m  适用高度区间（米），无则 None。
        area_range_m2   适用面积区间（㎡），无则 None。
        conditions      其余结构化谓词（Condition 列表）。
        scope_status    extracted / unknown（抽不准 → 保守召回，宁可多召）。
    """
    building_types: list[str]
    height_range_m: list[float] | None
    area_range_m2: list[float] | None
    conditions: list[Condition]
    scope_status: ScopeStatus


class TableData(TypedDict):
    """``table_struct`` 表征的结构化载荷（可查询表格）。

    功能：把表格存成矩形二维结构，支持「给定行列条件取值」。
    字段：
        table_id 表格标识。
        caption  表题。
        header   表头行。
        rows     表体（已展开 colspan/rowspan 的矩形网格）。
        page     页码。
    """
    table_id: str
    caption: str
    header: list[str]
    rows: list[list[str]]
    page: int


class Provenance(TypedDict, total=False):
    """节点溯源 —— 回指构成本节点的 MinerU 原始块（阶段 0 缓存不可变）。

    功能：把派生节点链回 ``data/parsed/<std>/auto/*_content_list.json`` 的原始元素，
        使「改算法重派生、核对低置信/合成节点、PDF 高亮、恢复被丢弃信息」都不必
        重跑 MinerU。原始文本不复制进 nodes.json，只存轻量指针（``total=False``）。
    字段：
        source_file MinerU content_list.json 路径（相对 data/parsed/）。
        block_idx   本节点聚合的原始块下标列表；synthesized（凭空合成）节点为 []。
        page        涉及页码（1-base），便于定位。
        bbox        可选版面框 [{"page": int, "box": [x0,y0,x1,y1]}]，供 PDF 高亮。
    返回：无（TypedDict，作字典契约）。
    """
    source_file: str
    block_idx: list[int]
    page: list[int]
    bbox: list[dict]


# ── 表征（语义投影）────────────────────────────────────────────────────────────

class Representation(TypedDict, total=False):
    """节点的一种「可被检索的样子」= 一个语义投影面。

    功能：把同一节点投影成多种可检索形态（原文 / 向量 / 摘要 / 语气 / 条件 …）；
        检索是多表征的可组合并集。表征层注册表逐个产出，可插拔、可消融。字段按
        ``kind`` 取用，``total=False`` 允许只填相关字段。
    字段：
        kind   表征类型（ReprKind）；通常与 ``Node.reprs`` 的键一致，冗余存便于审计。
        text   进 embedding / BM25 的文本形态（raw / context_aug / summary / questions）。
        vector dense 向量（dense / summary / questions）。
        data   结构化载荷（table_struct→TableData、condition→ConditionData、
               modal→{"all": [...]} 等）。
        meta   审计/附属信息（来源、置信度等）。
    返回：无（TypedDict，作字典契约）。
    """
    kind: ReprKind
    text: str
    vector: list[float]
    data: dict
    meta: dict


# ── 节点（语义树的一个节点）────────────────────────────────────────────────────

class Node(TypedDict, total=False):
    """规范语义树的一个节点 = **单一真值**。

    功能：承载文档原生目录还原出的层级结构（parent/child）、建树时一次算定的
        「固有事实」（引用图 / 祖先链），以及表征层挂的多种「语义投影」（reprs）。
        粒度视图与各检索索引都是它的派生。``total=False`` 允许结构层/表征层分阶段填。
    字段：
        ── 标识与版本 ──
        node_id        稳定 id：条文号有则用，无则标题路径（如 "GB50016#5.3.4"）。
        standard_id / version / effective_date / status(NodeStatus)。
        ── 结构（树形）──
        node_type      NodeType。
        clause_path    条款号 "5.3.4" 或标题路径。
        level          = clause_path 号段数，**不取 MinerU text_level**（后者不可靠）。
        parent_id / children_ids   树形边（粒度视图 + small-to-big 全靠它）。
        page / title / content     content 仅自身正文，不含子节点。
        ── 固有事实（建树时一次算定，唯一、不可多表达）──
        ancestor_titles / ancestor_paths   祖先标题链 / 路径链。
        references(list[Reference]) / referenced_by(list[str])  引用图正反向边。
        ── 结构层审计（编号驱动建树的溯源，见 PRD §3.1）──
        path_source    number / text_level / inherited / synthesized / lexicon。
        path_confidence 低置信进 03 抽查。
        ── 溯源（回指 MinerU 原始块）──
        provenance(Provenance)  本节点由哪些原始块构成，链回阶段 0 缓存。
        ── 表征层 ──
        reprs          dict[ReprKind, Representation]（可空、可部分）。
    返回：无（TypedDict，作字典契约）。
    """
    # 标识与版本
    node_id: str
    standard_id: str
    version: str
    effective_date: str
    status: NodeStatus
    # 结构（树形）
    node_type: NodeType
    clause_path: str
    level: int
    parent_id: str | None
    children_ids: list[str]
    page: int
    title: str
    content: str
    # 固有事实
    ancestor_titles: list[str]
    ancestor_paths: list[str]
    references: list[Reference]
    referenced_by: list[str]
    # 结构层审计
    path_source: str
    path_confidence: float
    # 溯源
    provenance: Provenance
    # 表征层
    reprs: dict[str, Representation]


# ── 工厂 / 默认值 ──────────────────────────────────────────────────────────────

def empty_condition() -> ConditionData:
    """未抽取的适用范围谓词：scope_status=unknown → 进保守召回（宁可多召不可漏）。

    参数：无。
    返回：ConditionData，各字段空、scope_status="unknown"（供 condition 表征兜底）。
    """
    return {
        "building_types": [],
        "height_range_m": None,
        "area_range_m2": None,
        "conditions": [],
        "scope_status": "unknown",
    }


def new_node(standard_id: str, clause_path: str, node_type: str, **kw: object) -> Node:
    """构造带默认值的空节点（其余字段由结构层 / 表征层逐步填）。

    参数：
        standard_id (str): 规范标识。
        clause_path (str): 条款号或标题路径。
        node_type (str): 节点类型（NodeType）。
        **kw: 覆盖默认值的字段；``node_id`` 缺省由 ``<standard_id>#<clause_path>``
            合成，``level`` 缺省由 clause_path 号段数推导。
    返回：
        Node: 填好标识/结构默认、固有事实为空、reprs 为空 dict 的节点。
    """
    node_id = str(kw.pop("node_id", "") or "") or f"{standard_id}#{clause_path}"
    level = int(kw.pop("level", clause_path.count(".") + 1))  # "5.3.1"→3 / "5"→1 / "前言"→1
    node: Node = {
        "node_id": node_id,
        "standard_id": standard_id,
        "version": "",
        "effective_date": "",
        "status": "active",
        "node_type": node_type,  # type: ignore[typeddict-item]
        "clause_path": clause_path,
        "level": level,
        "parent_id": None,
        "children_ids": [],
        "page": 0,
        "title": "",
        "content": "",
        "ancestor_titles": [],
        "ancestor_paths": [],
        "references": [],
        "referenced_by": [],
        "path_source": "",
        "path_confidence": 1.0,
        "provenance": {"source_file": "", "block_idx": [], "page": []},
        "reprs": {},
    }
    node.update(kw)  # type: ignore[typeddict-item]
    return node
