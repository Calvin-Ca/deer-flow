"""粒度视图 —— 索引期在节点树上选「投影深度」的**确定性视图函数**（PRD §3.1 ③ / §3.2）。

粒度不是切树，而是 ``view(nodes, index_granularity) → 检索单元``：节点树（阶段 1，
``nodes.json``）一次建好且完整保留，索引层（04，T4）按 ``index_granularity`` 只把**某一层**
的节点 emit 进向量 / BM25 索引。换粒度做实验 = 改 profile 重跑阶段 3，不重建树。

检索期 small-to-big（T9）靠命中单元的 ``parent_id`` 上探父节点回补整条/整节上下文，
故视图选「小粒度匹配」、返回时「大粒度回补」，两者解耦——视图只管 emit 哪些单元。

**最小切片（T7）**：先只实现 ``clause`` 层；``section`` / ``paragraph`` 留后补
（emit 哪些节点是确定性规则，加层即加一个分支，不影响建树与索引骨架）。
"""
from __future__ import annotations

# index_granularity → emit 该粒度的 node_type（种类）集合。
# clause 粒度 emit node_type=="leaf" 的节点（叶·检索单元：GB 50016 的 "5.3.4"、总则
#   "1.0.1"、附录条款均归此）；container（容器章/节/附录根）等骨架不单独 emit，靠
#   small-to-big 上探回补。注：index_granularity 是用户轴名、node_type 是结构种类，二者不同名属正常。
_LEAF_NODE_TYPES: frozenset[str] = frozenset({"leaf"})


def view(nodes: list[dict], index_granularity: str = "clause") -> list[dict]:
    """在节点树上选定粒度视图，返回该层的检索单元（索引期纯函数）。

    功能：把完整节点树投影到单一索引粒度——只挑该粒度的节点作检索单元 emit，树本身
        不改动。确定性、可重放：同一棵树 + 同一 granularity → 同一组单元。

    参数：
        nodes (list[dict]): 节点树（schema.Node 形态，来自 nodes.json）。
        index_granularity (str): 粒度视图——section | clause | paragraph。
            **最小切片仅支持 clause**；其余抛 NotImplementedError 待后补。

    返回：
        list[dict]: 选中的检索单元（节点 dict 的子集，保持原引用，不拷贝）。
            每个单元带 node_path / parent_id / content 等，供 04 建行；
            parent_id 是检索期 small-to-big 上探的锚点。

    异常：
        NotImplementedError: index_granularity 为 section / paragraph（最小切片未实现）。
        ValueError: index_granularity 取值非法。
    """
    if index_granularity == "clause":
        return [n for n in nodes if n.get("node_type") in _LEAF_NODE_TYPES]
    if index_granularity in ("section", "paragraph"):
        raise NotImplementedError(
            f"index_granularity={index_granularity!r} 后补；T7 最小切片先只做 clause 层 emit"
        )
    raise ValueError(
        f"未知 index_granularity={index_granularity!r}（应为 section | clause | paragraph）"
    )
