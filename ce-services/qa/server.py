#!/usr/bin/env python3
"""建筑规范 RAG —— qa 任务服务（常驻 HTTP，端口 8102，独立进程）。

职责：检索 + Qwen3 结构化生成（code-qa skill 的默认路径）。它是知识服务（:8100）的
**纯 HTTP 客户端**——通过 ``common.knowledge_client`` 打 /search 拿裸条款，再用
``qa.generation.answer`` 调 Qwen3 生成结构化回答。本进程不 import retrieval、不连
Milvus / 向量库。

为什么生成留在 server 端：强制引用、强条/推荐区分、无依据拒答是硬约束，必须确定可
复现，不下放自由 agent 推理（详见知识层重构计划）。

端点：
  GET  /health   健康检查（含知识服务 / LLM 地址）
  POST /qa       检索 + 结构化生成

启动（服务器上）：
  cd services
  uv run python qa/server.py
  # 或： uv run uvicorn qa.server:app --host 0.0.0.0 --port 8102
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# 让 `python qa/server.py` 与 `uvicorn qa.server:app` 两种启动方式都能 import common/qa
_SERVICES_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICES_ROOT))

from common import knowledge_client  # noqa: E402
from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL  # noqa: E402
from qa.generation import answer  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("services.qa")


class QARequest(BaseModel):
    query: str = Field(..., description="自然语言查询")
    standard: str = "gb50016"
    top_k: int = 20
    skip_rerank: bool = False


app = FastAPI(title="Building Code RAG · QA Service", version="3.0.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "qa",
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
    }


def _retrieve(req: QARequest, rid: str) -> dict:
    """调知识服务 /search，把其 HTTP 错误透传为本服务对应状态码。"""
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


@app.post("/qa")
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8102)
