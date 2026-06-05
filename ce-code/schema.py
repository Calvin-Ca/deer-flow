"""v2 条款 schema —— 解析层 / 构建层 / 检索层的**单一真值契约**。

对应 PRD §1。三层只认这里的字段定义:解析层产出 schema-conformant 条款,
构建层是 canonical store 与 schema 的唯一 owner,检索层只读、不再 ``json.loads``
重解析。

过渡策略(方案 B):规范轨真值落 ``data/structured/*_clauses.json``(关系库的
逻辑形态,P0 不起 PG),向量/BM25/引用图邻接都是它的派生产物。索引重建前,
``to_v1_compat()`` 把 v2 条款桥回旧字段(``is_mandatory`` / ``references_to``),
让现有 engine / metadata 不崩。
"""
from __future__ import annotations

from typing import Literal, TypedDict

# ── 受控词表 ───────────────────────────────────────────────────────────────────

ModalStrength = Literal["必须", "严禁", "不应", "不得", "应", "宜", "可", ""]
RefType = Literal["strong", "weak", "exclude", "cross_standard"]
ScopeStatus = Literal["extracted", "unknown"]
ClauseStatus = Literal["active", "superseded", "abolished"]

# 黑体强条桥接用:这些语气词在旧 is_mandatory 里被当作"强制",过渡期保守保留,
# 避免重建索引前强条召回回退(漏强条=事故)。
_HARD_MODAL: frozenset[str] = frozenset({"必须", "严禁", "不应", "不得"})

# strong / cross_standard 才参与引用图正向扩展(命中 A 自动拉 B);
# weak 可选、exclude 禁止正向扩展。检索层据此判断,见 retrieval/engine.expand。
EXPANDABLE_REF_TYPES: frozenset[str] = frozenset({"strong", "cross_standard"})


# ── 子结构 ─────────────────────────────────────────────────────────────────────

class Reference(TypedDict):
    to: str          # 本规范条款号 "5.2.1",或跨规范 "GB 50116-2013"
    type: RefType


class Condition(TypedDict):
    field: str       # height_m / underground / building_type / area_m2 ...
    op: str          # > >= < <= == != in
    value: object


class ApplicableScope(TypedDict):
    building_types: list[str]
    height_range_m: list[float] | None
    area_range_m2: list[float] | None
    conditions: list[Condition]
    scope_status: ScopeStatus   # extracted / unknown(抽不准→保守召回)


class TableRepr(TypedDict):
    table_id: str
    caption: str
    header: list[str]
    rows: list[list[str]]        # 结构化 body,支持给定行列条件取值
    is_mandatory_clause: bool    # 继承所属条款的黑体强制性
    page: int


# ── 条款(叶子节点 = 一个 chunk)──────────────────────────────────────────────

class Clause(TypedDict):
    standard_id: str
    version: str
    effective_date: str
    status: ClauseStatus
    clause_path: str
    level: int
    page: int
    ancestor_titles: list[str]        # 祖先标题链(章/节),向量化拼上下文
    modal_strength: ModalStrength     # 语气:必须/严禁/不应/不得/应/宜/可
    is_mandatory_clause: bool         # 黑体法律强制(召回率盯死的是它,≠ 语气)
    applicable_scope: ApplicableScope
    references: list[Reference]       # 引用边(分型),替代旧扁平 references_to
    referenced_by: list[str]          # 反向边(本规范内)
    content: str
    tables: list[TableRepr]
    formulas: list[dict]              # 预留:LaTeX + 变量语义/单位
    figures: list[dict]               # 原图 + VLM 描述


# ── 工厂 / 默认值 ──────────────────────────────────────────────────────────────

def empty_scope() -> ApplicableScope:
    """未抽取的适用范围:scope_status=unknown → 进保守召回(宁可多召不可漏)。"""
    return {
        "building_types": [],
        "height_range_m": None,
        "area_range_m2": None,
        "conditions": [],
        "scope_status": "unknown",
    }


# ── 向后兼容桥(重建索引前的过渡)─────────────────────────────────────────────

def to_v1_compat(clause: dict) -> dict:
    """给 v2 条款补回 v1 旧字段,使旧 engine / metadata / Milvus 行不崩。

    - ``is_mandatory``:黑体强条 **或** 硬语气(必须/严禁/不应/不得)。过渡期
      取并集,保证重建索引前强条召回不回退。
    - ``references_to``:摊平 ``references`` 的所有 ``to``(含 weak,过渡期宁可多
      扩展不漏);跨规范引用不进(本规范内条款号才能在 metadata 里命中)。

    索引重建、engine 切到 v2 字段后,本函数即可退役。
    """
    out = dict(clause)
    modal = clause.get("modal_strength", "")
    out["is_mandatory"] = bool(clause.get("is_mandatory_clause")) or modal in _HARD_MODAL
    refs = clause.get("references", [])
    out["references_to"] = [
        r["to"] for r in refs
        if r.get("type") != "cross_standard" and r.get("to") != clause.get("clause_path")
    ]
    return out
