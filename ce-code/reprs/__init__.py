"""表征注册表 —— 阶段 2 表征层（PRD §3.1 多表征 / §3.2 流水线阶段 2）。

把节点树（``nodes.json``，切分产物）投影成多种「可被检索的样子」：每个表征是节点意义的
一个投影面，检索是多表征的可组合并集。注册表 ``REGISTRY`` 把 ``ReprKind`` 映射到
**表征实例**（``Representation`` 子类，见 base.py），可插拔、可开关、可消融——profile 的
``reprs`` 列表决定本次启用哪些。

T8（第 1 步）先落**免费 4 项**（无 LLM、不加载模型、不联网）：
  raw（返回原文）/ sparse（BM25 词项）/ dense（待嵌入正文）/ context_aug（拼祖先链）。
table_struct / modal / condition（规则）与 summary / questions（LLM）推到第 4 步——
``register`` 新实例即并入并集，未注册的 kind 在 ``attach`` 里被跳过，不报错（前向兼容）。

入口：``enrich(nodes, enabled)`` 原地给每个节点挂 ``reprs``；阶段 2 表征 runner（T5 把
extract/build.py 退役后，由 04 / 独立 runner 调本入口）读 nodes.json → enrich → 写富化节点。
"""
from __future__ import annotations

from reprs.base import Representation
from reprs.context_aug import ContextAugRepr
from reprs.dense import DenseRepr
from reprs.raw import RawRepr
from reprs.sparse import SparseRepr

# ReprKind → 表征实例。新增表征 = new 一个 Representation 子类并 register（有状态的如
# LLM summary/questions 在其 __init__ 注入依赖后再 register），无需改建索引主流程。
REGISTRY: dict[str, Representation] = {}


def register(repr_: Representation) -> None:
    """登记一个表征实例到 REGISTRY（kind 重复则报错，避免静默覆盖）。

    参数：
        repr_ (Representation): 表征实例（其 ``kind`` 作注册键）。
    返回：
        无。
    """
    if repr_.kind in REGISTRY:
        raise ValueError(f"表征 kind={repr_.kind!r} 已注册，勿重复登记")
    REGISTRY[repr_.kind] = repr_


# 注册免费 4 项（无状态、单例复用）。
for _r in (RawRepr(), SparseRepr(), DenseRepr(), ContextAugRepr()):
    register(_r)

# 缺省启用集 = 已注册的全部（免费 4 项）；profile.reprs 缺省与此一致（见 parse_profile）。
DEFAULT_ENABLED: tuple[str, ...] = tuple(REGISTRY)


def attach(node: dict, enabled: list[str] | tuple[str, ...] | None = None) -> dict:
    """原地给单个节点挂 reprs（按 enabled 选启用的表征；未注册 kind 跳过）。

    参数：
        node (dict): schema.Node。
        enabled: 启用的 ReprKind 列表；None → DEFAULT_ENABLED（免费 4 项）。
    返回：
        dict: 同一 node（reprs 字段已填，便于链式）。
    """
    enabled = enabled if enabled is not None else DEFAULT_ENABLED
    reprs = node.setdefault("reprs", {})
    for kind in enabled:
        repr_ = REGISTRY.get(kind)
        if repr_ is None:
            continue  # table_struct/modal/condition/summary/questions 等未注册 → 第 4 步补
        reprs[kind] = repr_.build(node)
    return node


def enrich(nodes: list[dict], enabled: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    """原地给节点树每个节点挂 reprs（阶段 2 运行核心）。

    参数：
        nodes (list[dict]): 节点树（nodes.json 读出）。
        enabled: 启用的 ReprKind 列表；None → DEFAULT_ENABLED。
    返回：
        list[dict]: 同一 nodes（各节点 reprs 已填）。
    """
    for n in nodes:
        attach(n, enabled)
    return nodes
