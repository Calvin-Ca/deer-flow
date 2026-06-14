"""RRF 合并 + 引用图扩展 —— 在「索引行 dict」层操作（承旧 engine，数值逻辑逐字保持）。

  merge_results       BM25 + 向量两路结果 RRF 合并去重（k=60，去重键 node_path）。
  expand_references   沿可扩展引用边（strong / cross_standard）一跳扩展命中集。

去重键用 ``node_path``（废 node_id 后即节点 id，本集合内唯一）。引用解析按 ``references_to``
（被引目标的 node_path），跨规范引用查不到自动跳过。**保持旧行为不变**——hybrid 调用此处，
召回与重构前一致。
"""
from __future__ import annotations


def merge_results(bm25_results: list[dict], vector_results: list[dict]) -> list[dict]:
    """RRF 合并去重（k=60，去重键 node_path）。

    同一 node_path 在两路命中即合并；合并后按 RRF 分降序，每条挂 ``_rrf_score``。
    """
    k = 60  # RRF 常数
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for rank, item in enumerate(bm25_results):
        nid = item["node_path"]
        scores[nid] = scores.get(nid, 0) + 1 / (k + rank + 1)
        items[nid] = item

    for rank, item in enumerate(vector_results):
        nid = item["node_path"]
        scores[nid] = scores.get(nid, 0) + 1 / (k + rank + 1)
        if nid not in items:
            items[nid] = item
        else:
            items[nid]["_source"] = "both"
            items[nid]["_vector_score"] = item.get("_vector_score")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results = []
    for nid, rrf_score in ranked:
        item = dict(items[nid])
        item["_rrf_score"] = rrf_score
        results.append(item)
    return results


def expand_references(results: list[dict], metadata: list[dict], max_depth: int = 1) -> list[dict]:
    """沿可扩展引用边（strong / cross_standard）一跳扩展命中集。

    ``references_to`` 存被引目标 node_path（如 "5.2.1"），引用解析与去重都按 node_path。
    跨规范引用（cross_standard，如 "GB 50116-2013"）不在本规范 metadata 内 → 查不到自动跳过。
    """
    meta_by_path = {m["node_path"]: m for m in metadata}
    existing_ids = {r.get("node_path") for r in results}
    expanded = list(results)

    frontier = list(results)
    for _ in range(max_depth):
        next_frontier = []
        for item in frontier:
            for ref_path in item.get("references_to", []):
                ref_item = meta_by_path.get(ref_path)
                if ref_item is None or ref_item.get("node_path") in existing_ids:
                    continue
                new_item = dict(ref_item)
                new_item["_source"] = "ref_expand"
                new_item["_rrf_score"] = 0.0
                expanded.append(new_item)
                existing_ids.add(ref_item.get("node_path"))
                next_frontier.append(new_item)
        frontier = next_frontier

    return expanded
