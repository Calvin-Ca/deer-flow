"""表征注册表 —— 阶段 2 表征层（PRD §3.1 多表征 / §3.2 流水线阶段 2）。

把节点树（``nodes.json``，建树产物）投影成多种「可被检索的样子」：每个表征是节点意义的
一个投影面，检索是多表征的可组合并集。注册表 ``REGISTRY`` 把 ``ReprKind`` 映射到产函数，
可插拔、可开关、可消融——profile 的 ``reprs`` 列表决定本次启用哪些。

T8（第 1 步）先落**免费 4 项**（无 LLM、不加载模型、不联网）：
  raw（返回原文）/ sparse（BM25 词项）/ dense（待嵌入正文）/ context_aug（拼祖先链）。
table_struct / modal / condition（规则）与 summary / questions（LLM）推到第 4 步——
未注册的 kind 在 ``attach`` 里被跳过，不报错（前向兼容）。

向量归属：dense / context_aug 只产**待嵌入文本**，向量由索引期 04_build_index 用
embedding 模型统一计算（模型「唯一 owner」在检索栈，表征层不加载）。

入口：``enrich(nodes, enabled)`` 原地给每个节点挂 ``reprs``；阶段 2 表征 runner（T5 把
extract/build.py 重定位为此）读 nodes.json → enrich → 写富化节点，与本注册表收口为同一入口。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent  # ce-code/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reprs import context_aug, dense, raw, sparse  # noqa: E402

# ReprKind → 产函数（免费 4 项）。新增表征在此登记即并入并集，无需改建索引主流程。
REGISTRY: dict[str, callable] = {
    m.KIND: m.build for m in (raw, sparse, dense, context_aug)
}

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
        fn = REGISTRY.get(kind)
        if fn is None:
            continue  # table_struct/modal/condition/summary/questions 等未注册 → 第 4 步补
        reprs[kind] = fn(node)
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
