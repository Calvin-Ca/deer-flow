#!/usr/bin/env python3
"""建筑规范 RAG —— 任务服务统一入口（常驻 HTTP，端口 8101）。

qa（/qa）与 compliance（/compliance）合并到单一进程，共用端口 8101。

端点：
  GET  /health      健康检查（含知识服务 / LLM 地址）
  POST /qa          检索 + 结构化生成（code-qa skill）
  POST /compliance  项目级合规检查（compliance-check skill）

启动：
  cd ce-services && uv run python main.py
  # 或：uv run uvicorn main:app --host 0.0.0.0 --port 8101
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from fastapi import FastAPI  # noqa: E402

from common.config import KNOWLEDGE_URL, LLM_URL  # noqa: E402
from compliance.router import router as compliance_router  # noqa: E402
from qa.router import router as qa_router  # noqa: E402

app = FastAPI(title="Building Code RAG · Task Services", version="3.0.0")
app.include_router(qa_router)
app.include_router(compliance_router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "tasks",
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
        "routes": ["/qa", "/compliance"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
