#!/usr/bin/env python3
"""任务层 —— 任务服务统一入口（常驻 HTTP，端口 8101）。

聚焦深圳房建组价，两条主线共进程：**Norm-QA**（造价规范问答）+ **CostAgent**（构件 → 选码 →
组价取数）。均为 ce-code 知识层的纯 HTTP 客户端——Norm-QA 打 ce-rag（默认 :8100）取规范条文，
CostAgent 打 ce-rag 召回清单候选 + ce-db（默认 :8102）取结构化真值。防火 RAG 消费方 qa/compliance 已退役。

端点：
  GET  /health        健康检查（含知识服务 / LLM 地址）
  POST /route         前置路由（T-A1）：确定性能力分流 + 形态判定（无 LLM，供编排器/agent 分流前置）
  POST /orchestrate   复合编排（T-A4）：单一直派 / 复合 32b 拆解→子任务回①路由→派发→综合
  POST /norm/qa       造价规范条文检索 + Qwen3 带引用作答（norm-qa）
  POST /cost/compose  构件描述 → 候选召回 → LLM 选码 → 组价取数（cost-agent，P1 选码闭环；传 rates 则算综合单价）
  POST /cost/unit-price 综合单价计算原语（P2，确定性算钱、pydantic 闸门、不入 LLM）
  POST /cost/rollup     总造价汇总原语（§13，分部分项+措施/其他/规费→可选税金→总造价，确定性、pydantic 闸门）
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
from fastapi.routing import APIRoute  # noqa: E402

from common.config import DB_URL, KNOWLEDGE_URL, LLM_URL, RAG_URL  # noqa: E402
from cost.router import router as cost_router  # noqa: E402
from norm.router import router as norm_router  # noqa: E402
from routing.router import router as routing_router  # noqa: E402


app = FastAPI(title="CE Task Services · Norm-QA + CostAgent", version="4.0.0")
# 注册与 /health 清单共用同一份 router 列表（单一事实源）：新增 router 只改这里，清单自动跟上。
# 不从 app.routes 反推——fastapi≥0.139 include_router 改懒挂载（app.routes 里是 _IncludedRouter
# 包装、不摊平也不暴露内部 routes），isinstance(APIRoute) 清单会归零；APIRouter.routes 是历代稳定 API。
APP_ROUTERS = (routing_router, norm_router, cost_router)
for _router in APP_ROUTERS:
    app.include_router(_router)


@app.get("/health")
def health() -> dict:
    """健康检查 + 路由清单。

    返回：``{status, service, rag_url, db_url, knowledge_url, llm_url, routes}``——routes 从 ``APP_ROUTERS`` 各
      router 的 ``routes`` **动态生成**（含真实 path 参数名如 ``{task_id}``），新增端点自动出现、
      不再手维护漏列（如曾漏 rewind）。
    """
    routes = sorted(
        r.path for router in APP_ROUTERS for r in router.routes
        if isinstance(r, APIRoute) and r.path not in ("/health", "/openapi.json")
    )
    return {
        "status": "ok",
        "service": "tasks",
        "rag_url": RAG_URL,
        "db_url": DB_URL,
        "knowledge_url": KNOWLEDGE_URL,
        "llm_url": LLM_URL,
        "routes": routes,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8101)
