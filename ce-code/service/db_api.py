"""DB REST + MCP entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException

from cost.query import resolve_dsn
from db.service import DbService
from service.db_mcp_server import mcp as db_mcp

_svc = DbService()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    async with db_mcp.session_manager.run():
        yield


app = FastAPI(title="ce-db", version="1.0.0", lifespan=_lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ce-db", "target": resolve_dsn().split("@")[-1]}


@app.get("/bill/{code}")
def bill_get(code: str, spec: str) -> dict[str, Any]:
    try:
        result = _svc.bill_get(code, spec=spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"清单编码 {code} 不存在")
    return result


@app.get("/bills")
def bill_list(spec: str, code_prefix: str | None = None, chapter: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    try:
        return _svc.bill_list(spec=spec, code_prefix=code_prefix, chapter=chapter, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/quota/{region}/{code}")
def quota_get(region: str, code: str) -> dict[str, Any]:
    result = _svc.quota_get(region=region, code=code)
    if result is None:
        raise HTTPException(status_code=404, detail=f"定额子目 {code}（{region}）不存在")
    return result


@app.get("/quotas")
def quota_list(
    region: str = "深圳",
    spec: str | None = None,
    bill_code: str | None = None,
    chapter: str | None = None,
    quota_code_prefix: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    try:
        return _svc.quota_list(
            region=region,
            spec=spec,
            bill_code=bill_code,
            chapter=chapter,
            quota_code_prefix=quota_code_prefix,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/price/query")
def price_query(
    name: str,
    region: str = "深圳",
    period: str | None = None,
    category: str | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    return _svc.price_query(name=name, region=region, period=period, category=category, top_k=top_k)


@app.get("/price/suggest")
def price_suggest(
    name: str,
    region: str = "深圳",
    category: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    return _svc.price_suggest(name=name, region=region, category=category, top_k=top_k)


@app.get("/price/compose/{region}/{code}")
def price_compose(region: str, code: str, spec: str, on_date: date | None = None) -> dict[str, Any]:
    try:
        result = _svc.price_compose(code=code, spec=spec, region=region, on_date=on_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"清单编码 {code} 不存在")
    return result


@app.get("/fee-rates")
def fee_rates(
    region: str = "深圳",
    fee_category: str | None = None,
    fee_name: str | None = None,
    applicable: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return _svc.fee_rate_lookup(
        region=region,
        fee_category=fee_category,
        fee_name=fee_name,
        applicable=applicable,
        limit=limit,
    )


@app.get("/price-composition")
def price_composition(composite: str | None = None, spec_version: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return _svc.price_composition_get(composite=composite, spec_version=spec_version, limit=limit)


@app.get("/aux-table")
def aux_table_get(doc_id: str, caption: str, chapter: str | None = None) -> dict[str, Any]:
    result = _svc.aux_table_get(doc_id=doc_id, caption=caption, chapter=chapter)
    if result is None:
        raise HTTPException(status_code=404, detail="辅助表不存在")
    return result


@app.get("/aux-tables")
def aux_table_list(
    spec_version: str | None = None,
    doc_id: str | None = None,
    kind: str | None = None,
    chapter: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return _svc.aux_table_list(spec_version=spec_version, doc_id=doc_id, kind=kind, chapter=chapter, limit=limit)


@app.get("/resources")
def resource_lookup(name: str, category: str | None = None, unit: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    return _svc.resource_lookup(name=name, category=category, unit=unit, limit=limit)


app.mount("/", db_mcp.streamable_http_app())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8102)
