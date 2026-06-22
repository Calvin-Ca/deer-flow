#!/usr/bin/env python3
"""任务层 CostAgent —— 任务服务统一入口（常驻 HTTP，端口 8101）。

聚焦深圳房建组价：CostAgent（构件 → 选码 → 组价）为唯一主线。规范 RAG 消费方
qa（/qa）/ compliance（/compliance）已退役（知识层后端 /search /clause 已删）。

端点：
  GET  /health        健康检查（含知识服务 / LLM 地址）
  POST /cost/compose  构件描述 → 候选召回 → LLM 选码 → 组价（cost-agent）

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

app = FastAPI(title="CostAgent · Task Services", version="4.0.0")

# cost 路由在 P1 选码闭环就位后挂载（cost/router.py）：
# from cost.router import router as cost_router
# app.include_router(cost_router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "tasks",
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
        "routes": [],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
