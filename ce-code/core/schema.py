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

# 结构层 node_type = 节点**种类**（kind），**与深度正交**：层级深度由 node_level（真实
# 目录树层级）单独承载，node_type 不编码"第几层"，也**不由 node_path 派生**——这样不假定
# 文档一定有"章/节/条"原生层级（造价定额表等套不上固定档名，见 PRD 跨文档适配）。
# 当前 TocSplitter 建树末按**树结构**二分（纯看"有无子节点"，见 tree_builder._assign_node_type）：
#   container  有子节点的容器（章 / 节 / 附录根等目录骨架，不单独 emit 检索单元，靠 small-to-big 回补）
#   leaf       叶节点·检索单元（条款 / 总则 / 无编号正文段 / 附录条款均归此，粒度视图 emit 这层）
# "是不是附录"等语义不进 node_type（它能从 node_path 前缀 "附录X" 直接读出，避免与 node_path 冗余）。
# 其余（document / paragraph / table / figure / formula）为**契约占位**，建树阶段未产出
# （表格/图示暂存 Node.tables / images 字段，待表征层转 table_struct，故 table / figure 未实装）。
NodeType = Literal[
    "container", "leaf",
    "document", "paragraph", "table", "figure", "formula",
]
RefType = Literal["strong", "weak", "exclude", "cross_standard"]
ScopeStatus = Literal["extracted", "unknown"]

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
        block_idx   本节点聚合的原始块下标列表；**为 [] 即"未接地空骨架"**（目录列了条目
                    但正文未抽到对应块，无真身可溯）——这是判定该状态的**单一真值**（不另设标记）。
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
        ── 标识 ──
        node_path      节点在文档原生层级里的**结构地址** = 节点 id：条款号 "5.3.4"/附录号
                       "附录E"/"E.1.1"，**或**无编号标题路径（"前言"/"术语和定义"）。本规范内
                       唯一，直接作树边 / 引用边的引用键（2026-06-14 由 clause_path 改名，并废
                       node_id——全层统一以 node_path 为键，不再存 ``standard_id#path`` 冗余 id）。
                       消费方**勿假定恒为数字号**。
        standard_id    规范标识；与 node_path 一起构成**跨规范全局身份**（单文件/单集合内
                       node_path 已唯一，跨规范才需带 standard_id 消歧）。
        ── 结构（树形）──
        node_type      NodeType = 节点**种类**（kind），**与深度正交**：container（容器·有子）
                       / leaf（叶·检索单元·无子）。建树末纯按"有无子节点"判定（见
                       tree_builder._assign_node_type），**不由 node_path 派生**——深度交给
                       node_level，"是否附录"等语义按需读 node_path 前缀。
        node_level     节点在**还原出的目录树**里的深度（1-base，根=1）= 沿 parent_id 上溯的
                       祖先数 +1，建树时算定（_attach_ancestors）。**不取** node_path 号段数：
                       中间层级缺节点时按真实父链算（5.3 缺 → 5.3.4 的父是 5、深度 2），也适配
                       无"章/节/条"原生层级的文档（曾名 level，2026-06-14 改名 + 改语义）。
        parent_id / children_ids   树形边（存被引节点的 node_path；粒度视图 + small-to-big 全靠它）。
        title / content   content 仅自身正文文本（确有副本，作检索载荷），不含子节点；
                       与 provenance 只存指针**并行不悖**（指针用于回指版面/被丢信息）。
                       （注：无独立 page 字段——展示页 = ``provenance.page[0]``，由索引/展示处
                       按需派生；未接地空骨架 provenance.page 为空 = 无正文即无页，见 2026-06-14
                       决策：不为无真身节点造目录页码。）
        ── 固有事实（建树时一次算定，唯一、不可多表达）──
        ancestor_titles / ancestor_paths   祖先标题链 / 路径链（沿 parent_id 上溯·去范式）。
        references(list[Reference]) / referenced_by(list[str])  引用图正反向边——**类型
                       不对称**：正向带分型 {to,type}，反向仅裸 node_path 列表（按需自查类型）。
        ── 结构层审计（编号驱动建树的溯源，见 PRD §3.1）──
        path_source    路径来源标签：node_path **怎么定出来的**——已实装 number（命中条号
                       正则·置信 1.0）/ text_level（无编号靠标题文字兜底·0.6）；inherited /
                       lexicon 为枚举占位，**未实装**。**与"是否接地"正交**：未接地空骨架
                       （目录有条目但正文未抽到块）不在此打标，由 ``provenance.block_idx == []``
                       判定（schema.Provenance 已声明该不变式），故空骨架仍保留 number /
                       text_level 的真实来源、不被覆盖。
        path_confidence 低置信进 03 抽查。
        ── 溯源（回指 MinerU 原始块）──
        provenance(Provenance)  本节点由哪些原始块构成，链回阶段 0 缓存。
        ── 表征层 ──
        reprs          dict[ReprKind, Representation]（可空、可部分）。
    返回：无（TypedDict，作字典契约）。
    """
    # 标识
    node_path: str
    standard_id: str
    # 结构（树形）
    node_type: NodeType
    node_level: int
    parent_id: str | None
    children_ids: list[str]
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


def new_node(standard_id: str, node_path: str, node_type: str = "", **kw: object) -> Node:
    """构造带默认值的空节点（其余字段由结构层 / 表征层逐步填）。

    参数：
        standard_id (str): 规范标识。
        node_path (str): 条款号或标题路径（= 节点 id，本规范内唯一）。
        node_type (str): 节点种类（NodeType）；缺省空串，建树末按结构判定
            （_assign_node_type）——故创建时通常不传。
        **kw: 覆盖默认值的字段；``node_level`` 缺省 0，由建树 _attach_ancestors
            按真实父链深度算定。
    返回：
        Node: 填好标识/结构默认、固有事实为空、reprs 为空 dict 的节点。
    """
    node: Node = {
        "node_path": node_path,
        "standard_id": standard_id,
        "node_type": node_type,  # type: ignore[typeddict-item]
        "node_level": int(kw.pop("node_level", 0)),
        "parent_id": None,
        "children_ids": [],
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
