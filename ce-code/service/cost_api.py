"""造价取数 HTTP 端点（知识层 :8100，与规范检索同进程、不同 router）。

职责：把 ``cost.query`` 的关系库取数原语包成 HTTP。本文件只做 HTTP ↔ 调用翻译 +
异常→状态码映射；SQL/取数逻辑在 ``cost.query``。规范条文检索原语在
``service.knowledge_api``（走 Milvus/BM25），造价取数走 PG，二者依赖隔离、共用 :8100
（由 knowledge_api ``include_router(cost_router)`` 挂载）。

端点：
  GET /quota/{region}/{code}            定额子目直取（子目字段 + 工料机含量）
  GET /price/compose/{region}/{code}    组价取数（清单 → 定额 → 工料机含量 + 信息价单价）

独立启动（仅造价端点，调试用，从 ce-code 根）：
  .venv/bin/python -m service.cost_api
正式：随 ``service.knowledge_api`` 一起对外 :8100。
"""
from __future__ import annotations

from datetime import date

import psycopg
from fastapi import APIRouter, FastAPI, HTTPException

from cost import query as cost_query

router = APIRouter(tags=["cost"])


@router.get("/quota/{region}/{code}")
def quota_endpoint(region: str, code: str) -> dict:
    """按地区 + 定额子目编号直取定额（含工料机含量）。

    参数：region —— 地区（如 深圳）；code —— 定额子目编号（如 010001-3）。
    返回：``{"item": {...}, "resources": [...]}``；子目不存在→404；PG 不可达→503。
    """
    try:
        with cost_query.connect() as conn:
            result = cost_query.get_quota(conn, region, code)
    except psycopg.OperationalError as exc:                # PG 不可达/认证失败
        raise HTTPException(status_code=503, detail=f"造价库不可达: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"定额子目 {code}（{region}）不存在")
    return result


@router.get("/price/compose/{region}/{code}")
def price_compose_endpoint(region: str, code: str, on_date: date | None = None) -> dict:
    """组价取数：清单项 → 适用定额 → 工料机含量 + 信息价单价（含小计）。

    参数：region —— 地区（如 深圳）；code —— 清单编码（GB 50854 9 位）；
      on_date —— 计价期（query，ISO 日期，可选；缺省每资源取最新可用信息价期）。
    返回：见 ``cost.query.compose_price``；清单项不存在→404；PG 不可达→503。
      未命中信息价的工料机 ``price_status="no_source"``（红线：只建议不定稿，交 HITL 询价）。
    """
    try:
        with cost_query.connect() as conn:
            result = cost_query.compose_price(conn, region, code, on_date)
    except psycopg.OperationalError as exc:               # PG 不可达/认证失败
        raise HTTPException(status_code=503, detail=f"造价库不可达: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"清单项 {code} 不存在（bill_spec）")
    return result


# ── 独立 app（调试单跑用；正式挂载见 knowledge_api）──────────────────────────────
app = FastAPI(title="Cost · 造价取数", version="0.1.0")
app.include_router(router)


@app.get("/health")
def health() -> dict:
    """健康检查（不连库，仅报服务存活 + DSN 的 host/库名，密码不外泄）。"""
    return {"status": "ok", "service": "cost", "target": cost_query.resolve_dsn().split("@")[-1]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8100)
