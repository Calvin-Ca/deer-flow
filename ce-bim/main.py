#!/usr/bin/env python3
"""BIM 底座层 —— 服务统一入口（常驻 HTTP，端口 8102）。

项目 BIM 模型的单一 owner，对外只暴露 BIM 原语（/model/*，按 GlobalId 寻址），
被各消费方（ce-cost 算量计价 / 审图 / FM）以纯 HTTP 客户端复用（见 ``ce-bim/PRD.md``）。
底座自身**不依赖** :8100 / :8101，可独立启动。

端点：
  GET  /health                 健康检查（含存储后端）
  POST /model/ingest           上传 IFC → 存储 + 解析建索引
  GET  /model/{id}             取 IFC 原件
  GET  /model/{id}/elements    列构件（带 GlobalId）
  GET  /model/{id}/element/{guid}  单构件详情
  POST /model/{id}/quantity    按 GlobalId 批量取量
  GET  /model/{id}/spatial     空间结构树

启动：
  cd ce-bim && uv run python main.py
  # 或：uv run uvicorn main:app --host 0.0.0.0 --port 8102
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

from api.model_router import router as model_router  # noqa: E402
from common import config  # noqa: E402
from parse.ifc_parser import parser_health  # noqa: E402
from store.store import get_store  # noqa: E402

app = FastAPI(title="Building Code · BIM Foundation", version="0.1.0")
app.include_router(model_router)


@app.get("/health")
def health() -> dict:
    """健康检查（含依赖探活）。

    功能：上报服务状态 + 三项依赖探活——存储后端（local 可写 / MinIO 连通）、IFC 解析依赖
          （ifcopenshell 可用性）、端点清单，供依赖此底座的消费方/运维探活。
    参数：无。
    返回：``{status, service, store, parser, routes}``；任一依赖不健康时 ``status`` 为 ``degraded``。
    """
    try:
        store = get_store().health()
    except Exception as exc:  # noqa: BLE001 — MinioStore 构造即连库，可能在此抛
        store = {"backend": config.store_backend(), "ok": False, "error": str(exc)}

    parser = parser_health()
    healthy = store.get("ok", False) and parser.get("available", False)
    return {
        "status": "ok" if healthy else "degraded",
        "service": "bim",
        "store": store,
        "parser": parser,
        "routes": [
            "/model/ingest",
            "/model/{id}",
            "/model/{id}/elements",
            "/model/{id}/element/{guid}",
            "/model/{id}/quantity",
            "/model/{id}/spatial",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
