#!/usr/bin/env python3
"""compliance 独立启动入口（单独测试用）。

生产环境请用 ce-services/main.py（:8101 统一入口，qa + compliance 共进程）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from fastapi import FastAPI  # noqa: E402

from common.config import KNOWLEDGE_URL, LLM_URL  # noqa: E402
from compliance.router import router  # noqa: E402

app = FastAPI(title="Building Code RAG · Compliance Service (standalone)", version="3.0.0")
app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "compliance",
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
