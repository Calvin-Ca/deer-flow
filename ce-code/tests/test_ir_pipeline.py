"""IR + 流水线行为单测 —— 无 IO、无外部服务，锁住分层重构后的契约与端到端行为。

覆盖：
  - IR JSON 往返（Document / Chunk / ChunkFeature；含嵌套 references/provenance/features）；
  - RetrievedChunk 对外契约（from_row ↔ to_response 字段名逐字保持）；
  - 合成 Document → toc 切分 → 表征 → 粒度视图 全链路（解析模型/切法/表征经 factory）；
  - index.view 空骨架过滤（未接地 leaf 不 emit）；
  - RRF 合并 + 引用扩展数值行为（行为保持）。

运行（从 ce-code 根，单行）：
  uv run python tests/test_ir_pipeline.py        # 独立跑（stdlib assert）
  uv run pytest tests/test_ir_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 从 ce-code 根可 import 各层

import parser
import splitter
import feature
from core import (
    Block, Chunk, Document, ParseProfile, Provenance, Reference, RetrievedChunk,
)
from index.manager import chunk_to_row, view
from retrieval.rrf import expand_references, merge_results


# ── IR 往返 ──────────────────────────────────────────────────────────────────

def test_chunk_round_trip():
    c = Chunk(node_path="5.3.4", standard_id="GB", chunk_type="leaf", level=3, parent_id="5.3",
              title="燃气", content="应符合 5.2.1",
              references=[Reference("5.2.1", "strong"), Reference("9.9", "weak")],
              provenance=Provenance(source_file="x.json", block_idx=[12], page=[40]))
    feature.attach(c)
    assert Chunk.from_dict(c.to_dict()).to_dict() == c.to_dict()
    assert c.chunk_id == "5.3.4" and c.is_grounded()
    assert c.expandable_refs() == ["5.2.1"]  # 仅 strong 入扩展


def test_document_round_trip():
    doc = Document(standard_id="GB", source_file="x.json", blocks=[
        Block(type="text", text="总则", page=1, block_idx=0, text_level=1),
        Block(type="table", text="表1", page=2, block_idx=1, body=[["a", "b"]]),
    ])
    assert Document.from_dict(doc.to_dict()).to_dict() == doc.to_dict()
    # text_level 仅标题块写键（消费方靠 in 判是否标题）
    assert "text_level" in doc.blocks[0].to_dict() and "text_level" not in doc.blocks[1].to_dict()


def test_retrieved_chunk_contract():
    row = {"node_path": "5.3.4", "parent_id": "5.3", "granularity": "clause", "standard_id": "GB",
           "content": "x", "node_level": 3, "page": 40, "references_to": ["5.2.1"],
           "has_tables": False, "has_images": False, "_source": "both", "_rrf_score": 0.5}
    resp = RetrievedChunk.from_row(row).to_response()
    for k in ["node_path", "parent_id", "granularity", "standard_id", "content", "node_level",
              "page", "references_to", "has_tables", "has_images", "_source", "_rrf_score"]:
        assert k in resp, f"缺契约字段 {k}"
    assert resp["references_to"] == ["5.2.1"] and resp["_source"] == "both"


# ── 流水线行为（解析→切分→表征→视图）────────────────────────────────────────

_ITEMS = [
    {"type": "text", "text": "目录", "text_level": 1, "page_idx": 0},
    {"type": "list", "list_items": ["1 总则 …… 1", "5 防火分区 …… 5", "5.3 防火分区 …… 6"], "page_idx": 0},
    {"type": "text", "text": "1 总则", "text_level": 1, "page_idx": 1},
    {"type": "text", "text": "1.0.1 为预防火灾，制定本规范", "page_idx": 1},
    {"type": "text", "text": "5 防火分区", "text_level": 1, "page_idx": 5},
    {"type": "text", "text": "5.3 防火分区与建筑构件", "text_level": 2, "page_idx": 6},
    {"type": "text", "text": "5.3.4 燃气管道应符合本规范第 5.3 条的规定", "page_idx": 6},
    {"type": "table", "table_caption": ["表5.3.4"],
     "table_body": "<table><tr><td>类别</td><td>限值</td></tr></table>", "page_idx": 6},
]


def _build_tree():
    doc = parser.factory.select("mineru").adapt(_ITEMS, standard_id="GB 50016", source_file="GB/auto/x.json")
    prof = ParseProfile()
    chunks = splitter.factory.select("toc").split(
        doc, max_depth=prof.toc_max_depth, subsplit=prof.subsplit).chunks
    feature.enrich(chunks, list(ParseProfile().features))
    return chunks


def test_pipeline_tree_and_decoupled_clause():
    by = {c.node_path: c for c in _build_tree()}
    # 目录镜像树：1 / 5 / 5.3；建条解耦 → 1.0.1 并入 '1' 正文（非独立节点）
    assert set(by) == {"1", "5", "5.3"}
    assert by["5"].chunk_type == "container" and by["1"].chunk_type == "leaf"
    assert "1.0.1" in by["1"].content
    assert by["5.3"].ancestor_paths == ["5"]
    assert "防火分区" in by["5.3"].features["context_aug"].text


def test_view_clause_and_row():
    chunks = _build_tree()
    units = view(chunks, "clause")
    # 接地 leaf = 1 与 5.3（container 5 不 emit）
    assert {u.node_path for u in units} == {"1", "5.3"}
    r53 = [chunk_to_row(u, "clause") for u in units if u.node_path == "5.3"][0]
    assert r53["has_tables"] is True and r53["node_level"] == 2


def test_view_skips_ungrounded_skeleton():
    grounded = Chunk(node_path="5.3.4", chunk_type="leaf", provenance=Provenance(block_idx=[1]))
    skeleton = Chunk(node_path="7.1.1", chunk_type="leaf", provenance=Provenance(block_idx=[]))
    container = Chunk(node_path="5", chunk_type="container", provenance=Provenance(block_idx=[0]))
    units = view([grounded, skeleton, container], "clause")
    assert [u.node_path for u in units] == ["5.3.4"]  # 空骨架 + 容器均不 emit


# ── RRF / 引用扩展（行为保持）─────────────────────────────────────────────────

def test_rrf_merge_and_expand():
    # 两路结果均带 references_to（真实下行：bm25/向量行都从 metadata/Milvus 带出引用边）
    refs = ["5.2.1", "GB 50116-2013"]
    bm = [{"node_path": "5.3.4", "content": "a", "_bm25_score": 2.0, "_source": "bm25", "references_to": refs}]
    ve = [{"node_path": "5.3.4", "content": "a", "_vector_score": 0.9, "_source": "vector", "references_to": refs}]
    merged = merge_results(bm, ve)
    assert merged[0]["node_path"] == "5.3.4" and merged[0]["_source"] == "both"
    exp = expand_references(merged, [{"node_path": "5.2.1", "content": "ref", "references_to": []}])
    paths = [e["node_path"] for e in exp]
    assert "5.2.1" in paths               # 本规范内引用拉取
    assert "GB 50116-2013" not in paths    # 跨规范不在 metadata，跳过


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok   {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} 通过")
