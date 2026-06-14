"""对外检索编排 —— 在 RetrievalService 之上加可观测性（承旧 server._run_search / _log_clauses）。

职责：请求级编排 + 日志/计时/request_id，产**对外响应 dict**（字段名逐字保持旧契约）。检索数值
逻辑全在检索层（retrieval.RetrievalService），本层不碰；HTTP 层（knowledge_api）只调本层方法 +
做异常→状态码映射。CLI（tools/）亦可复用。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from core.query import RetrievalQuery
from retrieval.service import RetrievalService
from utils.logger import get_logger

logger = get_logger("ce-code.service")


class KnowledgeRetrieveService:
    """检索编排 + 可观测性（返回响应 dict）。"""

    def __init__(self, vector_store_root: Path) -> None:
        self.svc = RetrievalService(vector_store_root)

    def search(self, query: RetrievalQuery) -> dict:
        """混合检索 → 响应 dict（meta 含 request_id + 计时）。"""
        rid = uuid.uuid4().hex[:8]
        logger.info("[%s] search standard=%s top_k=%d query=%r",
                    rid, query.standard, query.top_k, query.text)
        t0 = time.perf_counter()
        ctx = self.svc.search(query)
        retrieve_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "[%s] 检索完成 bm25=%s vector=%s merged=%s expanded=%s final=%s (%.0fms)",
            rid, ctx.meta.get("bm25_hits"), ctx.meta.get("vector_hits"), ctx.meta.get("merged"),
            ctx.meta.get("expanded"), ctx.meta.get("final"), retrieve_ms,
        )
        self._log_clauses(rid, ctx.results)
        resp = ctx.to_response()
        resp["meta"] = {"request_id": rid, **ctx.meta,
                        "retrieve_ms": round(retrieve_ms), "elapsed_ms": round(retrieve_ms)}
        return resp

    def expand(self, node_paths: list[str], standard: str) -> dict:
        """引用扩展 → 响应 dict（expanded_clauses = 新增命中）。"""
        rid = uuid.uuid4().hex[:8]
        store_name, n_seeds, added = self.svc.expand(node_paths, standard)
        logger.info("[%s] /expand standard=%s seeds=%d → 新增 %d 条",
                    rid, store_name, n_seeds, len(added))
        return {
            "standard": store_name,
            "seeds": node_paths,
            "expanded_clauses": [a.to_response() for a in added],
            "meta": {"request_id": rid, "seeds": n_seeds, "added": len(added)},
        }

    def get_clause(self, standard: str, node_path: str) -> dict:
        """单条直取 → (store 名, 行 dict 或 None) 经 knowledge_api 映射（None → 404）。"""
        store_name, clause = self.svc.get_clause(standard, node_path)
        return {"standard": store_name, "node_path": node_path, "clause": clause}

    @staticmethod
    def _log_clauses(rid: str, results: list) -> None:
        """逐条输出溯源可观测性（承旧 server._log_clauses）。"""
        logger.info("[%s] ── 命中 %d 条", rid, len(results))
        logger.info("[%s]   条款号         页码      来源        引用关系", rid)
        for c in results:
            refs = c.references_to
            ref_str = ""
            if refs:
                preview = refs[:4]
                tail = f"+{len(refs) - 4}" if len(refs) > 4 else ""
                ref_str = f"  引用→[{', '.join(preview)}{tail}]"
            logger.info("[%s]   %-10s  p.%-4d  %-10s%s",
                        rid, c.node_path or "?", c.page, c.source, ref_str)
