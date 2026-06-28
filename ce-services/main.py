#!/usr/bin/env python3
"""任务层 —— 任务服务统一入口（常驻 HTTP，端口 8101）。

聚焦深圳房建组价，两条主线共进程：**Norm-QA**（造价规范问答）+ **CostAgent**（构件 → 选码 →
组价取数）。均为知识服务 :8100 的纯 HTTP 客户端——Norm-QA 打 /search 取规范条文，CostAgent 打
/bill/match 选码 + /price/compose 组价取数。防火 RAG 消费方 qa/compliance 已退役。

端点：
  GET  /health        健康检查（含知识服务 / LLM 地址）
  POST /norm/qa       造价规范条文检索 + Qwen3 带引用作答（norm-qa）
  POST /cost/compose  构件描述 → 候选召回 → LLM 选码 → 组价取数（cost-agent，P1 选码闭环；传 rates 则算综合单价）
  POST /cost/unit-price 综合单价计算原语（P2，确定性算钱、pydantic 闸门、不入 LLM）
  POST /cost/session/start          可中断组价 HITL 会话：跑到首个闸或 done（langgraph 图 + provenance 信封）
  POST /cost/session/{id}/resume    以用户决策续跑会话到下个闸或 done
  GET  /cost/session/{id}/state     读会话持久化状态（已钉编码 / override / audit_log，可跨会话恢复）

三条 session 端点 = HITL 主线：编排为 ce-services 独立 langgraph 图（可暂停可恢复），每数字带结构化来源
（provenance），闸门处暂停等人介入；与 /cost/compose 端到端「简单场景」旧路并存（判据=是否需 HITL/可审计）。

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
from cost.router import router as cost_router  # noqa: E402
from norm.router import router as norm_router  # noqa: E402

app = FastAPI(title="CE Task Services · Norm-QA + CostAgent", version="4.0.0")
app.include_router(norm_router)
app.include_router(cost_router)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "tasks",
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
        "routes": [
            "/norm/qa", "/cost/compose", "/cost/unit-price",
            "/cost/session/start", "/cost/session/{id}/resume", "/cost/session/{id}/state",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
