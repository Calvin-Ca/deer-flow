#!/usr/bin/env python3
"""建筑规范 RAG —— 合规服务（常驻 HTTP，端口 8101，独立进程）。

项目级合规检查是一条多步确定性流水线（参数提取 → 并行检索 → 逐维度去重判定 →
反思校验），漏强条=事故，必须 server 端可控。它是知识服务（:8100）的**纯 HTTP
客户端**：检索经 ``common.knowledge_client`` 打 /search，不 import retrieval、不连
Milvus / 向量库；判定 / 反思直接调 Qwen3 vLLM。

端点：
  GET  /health        健康检查（含知识服务 / LLM 地址）
  POST /compliance    项目级合规检查（compliance-check skill）

启动（服务器上）：
  cd services
  uv run python compliance/server.py
  # 或： uv run uvicorn compliance.server:app --host 0.0.0.0 --port 8101
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

# 让 `python compliance/server.py` 与 `uvicorn compliance.server:app` 都能 import common/compliance
_SERVICES_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICES_ROOT))

from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL  # noqa: E402
from compliance.orchestration import compliance_check  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("services.compliance-service")


class ComplianceRequest(BaseModel):
    project: str = Field(..., description="项目自由文本描述")
    standard: str = "gb50016"
    skip_reflection: bool = False


app = FastAPI(title="Building Code RAG · Compliance Service", version="3.0.0")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "compliance",
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
    }


@app.post("/compliance")
def compliance(req: ComplianceRequest) -> dict:
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    logger.info("[%s] /compliance standard=%s skip_reflection=%s project=%r",
                rid, req.standard, req.skip_reflection, req.project)

    try:
        report = compliance_check(
            description=req.project,
            standard=req.standard,
            llm_url=LLM_URL,
            model_id=LLM_MODEL_ID,
            skip_reflection=req.skip_reflection,
        )
    except requests.HTTPError as exc:
        # 知识服务 /search 的错误（未知规范 400 / 索引未就绪 503）透传
        resp = exc.response
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        logger.warning("[%s] 知识服务返回 %s: %s", rid, resp.status_code, detail)
        raise HTTPException(status_code=resp.status_code, detail=detail) from exc
    except requests.RequestException as exc:
        logger.exception("[%s] 无法连接知识服务", rid)
        raise HTTPException(
            status_code=503,
            detail=f"无法连接知识服务 {KNOWLEDGE_URL}: {exc}（请确认 :8100 检索服务已启动）",
        ) from exc
    except Exception as exc:
        logger.exception("[%s] 合规检查失败", rid)
        raise HTTPException(status_code=500, detail=f"合规检查失败: {exc}") from exc

    elapsed = round((time.perf_counter() - t0) * 1000)
    n_dims = len(report.get("dimensions", []))
    logger.info("[%s] 合规检查完成 维度=%d 强条=%s (%.0fms)",
                rid, n_dims, report.get("mandatory_clauses_total"), elapsed)

    report["meta"] = {
        "request_id": rid,
        "standard": req.standard,
        "dimensions_count": n_dims,
        "elapsed_ms": elapsed,
    }
    return report


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
