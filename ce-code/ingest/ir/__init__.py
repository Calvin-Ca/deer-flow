"""ingest.ir —— 摄取流水线的 **IR（中间表示）契约**（@dataclass + to_dict/from_dict）。

  document  Document / Block —— 解析层产物（统一元素流）。
  chunk     Chunk / Reference / Provenance —— 切分层产物（语义树节点·单一真值）。
  profile   ParseProfile —— 解析/切分流水线配置（贯穿配置）。

> 规范条文检索 RAG 已移除（2026-06-18 重构）：原 feature/query/retrieval/context 等检索 IR 随之删除，
> 本包只保留 PDF→chunks 摄取所需的三类契约。
"""
from __future__ import annotations

from ingest.ir.chunk import Chunk, Provenance, Reference, EXPANDABLE_REF_TYPES
from ingest.ir.document import Block, Document
from ingest.ir.profile import ParseProfile

__all__ = [
    "Document", "Block",
    "Chunk", "Reference", "Provenance", "EXPANDABLE_REF_TYPES",
    "ParseProfile",
]
