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
from pydantic import BaseModel, Field

import config
from cost import bill_match, query as cost_query

router = APIRouter(tags=["cost"])


class BillMatchRequest(BaseModel):
    """/bill/match 请求体：构件描述 → 清单候选召回。"""

    query: str = Field(..., description="构件/做法自然语言描述，如「C30 现浇钢筋混凝土矩形柱」")
    spec: str = Field(..., description="国标版本（必填，无默认）：2013 / 2024。按版本路由到对应清单库，避免跨版本串库")
    top_k: int = Field(10, ge=1, le=50, description="返回候选数")
    structural: bool = Field(True, description="是否结构约束重排（默认开——按类型/现浇预制对齐下压查询没要的模板/钢筋/预制项）")
    code_prefixes: list[str] | None = Field(None, description="专业 code 前缀过滤，如 ['01','03'] 只在建筑+安装内召回；不传=全专业")


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
def price_compose_endpoint(region: str, code: str, spec: str, on_date: date | None = None) -> dict:
    """组价取数：清单项 → 适用定额 → 工料机含量 + 信息价单价（含小计）。

    参数：region —— 地区（如 深圳）；code —— 清单编码（9 位）；
      spec —— **国标版本（query，必填）**：2013 / 2024。按版本隔离 bill_spec 取数，避免跨版本同码串库；
      on_date —— 计价期（query，ISO 日期，可选；缺省每资源取最新可用信息价期）。
    返回：见 ``cost.query.compose_price``；未知 spec→400；该版本组价数据未就绪→501；清单项不存在→404；
      PG 不可达→503。未命中信息价的工料机 ``price_status="no_source"``（红线：只建议不定稿，交 HITL 询价）。
    """
    try:
        cfg = config.resolve_spec(spec)
    except ValueError as exc:                             # 缺省/未知 spec
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not cfg["supports_compose"]:                       # 该版本组价数据（定额/价格/映射）未就绪
        raise HTTPException(status_code=501,
                            detail=f"{cfg['label']} 组价数据未就绪，暂仅支持清单匹配 /bill/match")
    try:
        with cost_query.connect() as conn:
            result = cost_query.compose_price(conn, region, code, on_date,
                                              spec_versions=cfg["bill_spec_versions"])
    except psycopg.OperationalError as exc:               # PG 不可达/认证失败
        raise HTTPException(status_code=503, detail=f"造价库不可达: {exc}") from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"清单项 {code} 不存在（{cfg['label']}）")
    return result


@router.post("/bill/match")
def bill_match_endpoint(req: BillMatchRequest) -> dict:
    """构件描述 → 清单候选召回（dense 向量检索 bill_spec_kb）。

    参数：req —— ``{query, spec, top_k}``（见 BillMatchRequest）。spec 按 SPEC_REGISTRY 路由到对应版本 collection。
    返回：``{"query", "spec", "count", "candidates": [{code,name,unit,feature,chapter,doc_id,spec_version,score}...]}``，
      按相似度降序。**知识层只召回候选**，选码由任务层在候选内决策（红线：只建议不定稿）。
      未知 spec→400；向量库未就绪 / Milvus / 嵌入服务不可达→503。
    """
    try:
        cfg = config.resolve_spec(req.spec)               # 国标版本路由（缺省/未知 → 400）
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        candidates = bill_match.search_bill(req.query, req.top_k,
                                            collection_name=cfg["bill_collection"],
                                            structural=req.structural,
                                            code_prefixes=req.code_prefixes)
    except ValueError as exc:                              # collection 未就绪
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:                               # Milvus / 嵌入服务不可达
        raise HTTPException(status_code=503, detail=f"清单召回服务不可达: {exc}") from exc
    return {"query": req.query, "spec": req.spec, "count": len(candidates), "candidates": candidates}


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
