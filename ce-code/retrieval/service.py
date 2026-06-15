"""检索服务 —— 对外统一检索入口（RetrievalQuery → KnowledgeContext），传输无关。

收口检索原语的「业务编排」（不含 HTTP）：规范代号 → store 解析 → 混合检索 / 引用扩展 / 单条
直取，产 IR（KnowledgeContext / RetrievedChunk）。HTTP 层（service/knowledge_api）只做 IR↔JSON
翻译 + 异常→状态码映射；CLI（tools/）亦复用本类。

异常约定（由调用方映射）：未知规范代号 → ValueError（HTTP 400）；store 未就绪 →
FileNotFoundError（HTTP 503）。
"""
from __future__ import annotations

from pathlib import Path

from config import DEFAULTS, collection_name, resolve_store_dir
from ir.context import KnowledgeContext
from ir.query import RetrievalQuery
from ir.retrieval import RetrievedChunk
from index import metadata_index
from retrieval.hybrid_retriever import HybridRetriever
from retrieval.rrf import expand_references


class RetrievalService:
    """检索原语编排（混合检索 / 引用扩展 / 单条直取）。"""

    def __init__(self, vector_store_root: Path, defaults: dict | None = None) -> None:
        self.root = Path(vector_store_root)
        self.defaults = defaults or DEFAULTS

    def _resolve(self, standard: str) -> tuple[Path, str]:
        """规范代号 → (store 目录, 规范化名)；未就绪抛 FileNotFoundError。"""
        store_dir, store_name = resolve_store_dir(standard, self.root)
        if not store_dir.exists():
            raise FileNotFoundError(str(store_dir))
        return store_dir, store_name

    def search(self, query: RetrievalQuery) -> KnowledgeContext:
        """混合检索：RetrievalQuery → KnowledgeContext（含各阶段 stats）。"""
        store_dir, store_name = self._resolve(query.standard)
        d = self.defaults
        hybrid = HybridRetriever(
            store_dir, collection_name(store_name),
            milvus_host=d["milvus_host"], milvus_port=d["milvus_port"],
            embed_url=d["embed_url"], embed_model_id=d["embed_model_id"],
        )
        stats: dict = {}
        results = hybrid.retrieve(query, stats=stats)
        return KnowledgeContext(query=query.text, standard=store_name, results=results, meta=stats)

    def expand(self, node_paths: list[str], standard: str) -> tuple[str, int, list[RetrievedChunk]]:
        """对种子 node_path 做一跳引用扩展，返回 (store 名, 种子数, 新增命中)。"""
        store_dir, store_name = self._resolve(standard)
        metadata = metadata_index.load(store_dir)
        seed_set = set(node_paths)
        seeds = [m for m in metadata if m.get("node_path") in seed_set]
        expanded = expand_references(seeds, metadata)
        added = [RetrievedChunk.from_row(c) for c in expanded
                 if c.get("node_path") not in seed_set]
        return store_name, len(seeds), added

    def get_clause(self, standard: str, node_path: str) -> tuple[str, dict | None]:
        """按 node_path 直取单条，返回 (store 名, 行 dict 或 None)。"""
        store_dir, store_name = self._resolve(standard)
        return store_name, metadata_index.get_clause(store_dir, node_path)
