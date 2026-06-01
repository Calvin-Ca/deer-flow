#!/usr/bin/env python3
"""建筑规范 RAG —— 合规服务（常驻 HTTP，端口 8101，独立进程）。

从原检索服务里拆出：项目级合规检查是一条多步确定性流水线（参数提取 → 并行检索
→ 逐维度去重判定 → 反思校验），漏强条=事故，必须 server 端可控。它 import retrieval
做检索、import service.orchestration 做编排，与检索服务（:8100）解耦、独立伸缩。

端点：
  GET  /health        健康检查
  POST /compliance    项目级合规检查（compliance-check skill）

启动（服务器上）：
  cd /mnt/nvme/calvin/code/deer-flow/ce-code
  .venv/bin/python service/compliance_server.py
  # 或： .venv/bin/python -m uvicorn service.compliance_server:app --host 0.0.0.0 --port 8101
"""
from __future__ import annotations

import logging
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

_SERVICE_DIR = Path(__file__).resolve().parent
_POC_ROOT = _SERVICE_DIR.parent
sys.path.insert(0, str(_POC_ROOT))

from retrieval.config import DEFAULTS, STANDARD_ALIASES, resolve_store_dir  # noqa: E402
from service.orchestration import compliance_check  # noqa: E402

_VECTOR_STORE = _POC_ROOT / "data" / "vector_store"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ce-code.compliance-service")


def _resolve(standard: str) -> tuple[Path, str]:
    try:
        store_dir, store_name = resolve_store_dir(standard, _VECTOR_STORE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not store_dir.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"向量索引未就绪（{store_dir} 不存在）。这是服务端配置问题，"
                f"请在服务器上构建索引：uv run pipeline/04_build_index.py --standard {standard}"
            ),
        )
    return store_dir, store_name


class ComplianceRequest(BaseModel):
    project: str = Field(..., description="项目自由文本描述")
    standard: str = "gb50016"
    skip_reflection: bool = False


app = FastAPI(title="Building Code RAG · Compliance Service", version="2.0.0")


@app.get("/health")
def health() -> dict:
    ready = sorted(
        {name for name in set(STANDARD_ALIASES.values())
         if (_VECTOR_STORE / name).exists()}
    )
    return {
        "status": "ok",
        "service": "compliance",
        "ready_standards": ready,
        "deps": {k: DEFAULTS[k] for k in ("embed_url", "llm_url", "milvus_host", "milvus_port")},
    }


@app.post("/compliance")
def compliance(req: ComplianceRequest) -> dict:
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    store_dir, store_name = _resolve(req.standard)
    logger.info("[%s] /compliance standard=%s skip_reflection=%s project=%r",
                rid, store_name, req.skip_reflection, req.project)

    try:
        report = compliance_check(
            description=req.project,
            store_dir=store_dir,
            llm_url=DEFAULTS["llm_url"],
            model_id=DEFAULTS["llm_model_id"],
            skip_reflection=req.skip_reflection,
        )
    except Exception as exc:
        logger.exception("[%s] 合规检查失败", rid)
        raise HTTPException(status_code=500, detail=f"合规检查失败: {exc}") from exc

    elapsed = round((time.perf_counter() - t0) * 1000)
    n_dims = len(report.get("dimensions", []))
    logger.info("[%s] 合规检查完成 维度=%d 强条=%s (%.0fms)",
                rid, n_dims, report.get("mandatory_clauses_total"), elapsed)

    report["meta"] = {
        "request_id": rid,
        "standard": store_name,
        "dimensions_count": n_dims,
        "elapsed_ms": elapsed,
    }
    return report


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
