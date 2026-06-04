"""qa 路由 —— 检索 + Qwen3 结构化生成。

由 main.py（统一入口）或 qa/server.py（独立测试）挂载。
调用方保证 sys.path 已含 ce-services 根目录。
"""
from __future__ import annotations

import logging
import time
import uuid

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common import knowledge_client
from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL
from qa.generation import answer

logger = logging.getLogger("services.qa")
router = APIRouter()


class QARequest(BaseModel):
    query: str = Field(..., description="自然语言查询")
    standard: str = "gb50016"
    top_k: int = 20
    skip_rerank: bool = False


def _retrieve(req: QARequest, rid: str) -> dict:
    try:
        return knowledge_client.search(
            query=req.query,
            standard=req.standard,
            top_k=req.top_k,
            skip_rerank=req.skip_rerank,
        )
    except requests.HTTPError as exc:
        resp = exc.response
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        logger.warning("[%s] 知识服务 /search 返回 %s: %s", rid, resp.status_code, detail)
        raise HTTPException(status_code=resp.status_code, detail=detail) from exc
    except requests.RequestException as exc:
        logger.exception("[%s] 无法连接知识服务", rid)
        raise HTTPException(
            status_code=503,
            detail=f"无法连接知识服务 {KNOWLEDGE_URL}: {exc}（请确认 :8100 检索服务已启动）",
        ) from exc


@router.post("/qa")
def qa_endpoint(req: QARequest) -> dict:
    rid = uuid.uuid4().hex[:8]
    logger.info("[%s] /qa standard=%s top_k=%d query=%r", rid, req.standard, req.top_k, req.query)

    result = _retrieve(req, rid)
    clauses = result.get("clauses", [])
    km = result.get("meta", {})
    retrieve_ms = km.get("retrieve_ms") or 0

    t1 = time.perf_counter()
    try:
        response = answer(req.query, clauses, LLM_URL, LLM_MODEL_ID)
    except Exception as exc:
        logger.exception("[%s] 生成失败", rid)
        raise HTTPException(
            status_code=500,
            detail=f"生成失败: {exc}（检索到 {len(clauses)} 条，可重试）",
        ) from exc

    generate_ms = (time.perf_counter() - t1) * 1000
    logger.info(
        "[%s] 生成完成 (%.0fms) 总耗时 %.0fms", rid, generate_ms, retrieve_ms + generate_ms
    )

    return {
        "query": req.query,
        "standard": result.get("standard", req.standard),
        "retrieved_clauses_count": len(clauses),
        "mandatory_clauses_count": result.get("mandatory_clauses_count", 0),
        "response": response,
        "meta": {
            "request_id": rid,
            "knowledge_request_id": km.get("request_id"),
            "retrieve_ms": round(retrieve_ms),
            "generate_ms": round(generate_ms),
            "elapsed_ms": round(retrieve_ms + generate_ms),
        },
    }
