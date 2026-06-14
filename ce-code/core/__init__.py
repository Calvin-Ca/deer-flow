"""core —— 贯穿全层的 **IR 契约包**（不含编排，不依赖任何业务层）。

各层只认这里的中间表示（IR，全部 ``@dataclass`` + ``to_dict/from_dict``）：
  document  Document / Block —— 解析层产物（统一元素流）。
  chunk     Chunk / Reference / Provenance —— 切分层产物（语义树节点·单一真值）。
  feature   ChunkFeature —— 表征层产物（一种可被检索的投影）。
  query     RetrievalQuery —— 检索入参契约。
  retrieval RetrievedChunk —— 单条检索命中（含对外契约 to_response）。
  context   KnowledgeContext —— 一次检索的完整结果集（= /search 返回体）。
  profile   ParseProfile —— 解析/索引流水线配置（贯穿配置，非 IR）。
"""
from __future__ import annotations

from core.chunk import Chunk, Provenance, Reference, EXPANDABLE_REF_TYPES
from core.context import KnowledgeContext
from core.document import Block, Document
from core.feature import ChunkFeature
from core.profile import ParseProfile
from core.query import RetrievalQuery
from core.retrieval import RetrievedChunk

__all__ = [
    "Document", "Block",
    "Chunk", "Reference", "Provenance", "EXPANDABLE_REF_TYPES",
    "ChunkFeature",
    "RetrievalQuery", "RetrievedChunk", "KnowledgeContext",
    "ParseProfile",
]
