"""compliance 路由 —— 项目级合规检查编排。

由 main.py（统一入口）或 compliance/server.py（独立测试）挂载。
调用方保证 sys.path 已含 ce-services 根目录。
"""
from __future__ import annotations

import logging
import time
import uuid

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL
from compliance.orchestration import compliance_check

logger = logging.getLogger("services.compliance")
router = APIRouter()


class ComplianceRequest(BaseModel):
    project: str = Field(..., description="项目自由文本描述")
    standard: str = "gb50016"
    skip_reflection: bool = False


@router.post("/compliance")
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
