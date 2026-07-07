"""RAG REST + MCP entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import STANDARD_ALIASES
from rag.service import RagService
from service.rag_mcp_server import mcp as rag_mcp

_ROOT = Path(__file__).resolve().parent.parent
_VECTOR_STORE = _ROOT / "data" / "vector_store"
_svc = RagService(_VECTOR_STORE)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    async with rag_mcp.session_manager.run():
        yield


app = FastAPI(title="ce-rag", version="1.0.0", lifespan=_lifespan)


class ClauseSearchRequest(BaseModel):
    query: str = Field(..., description="自然语言问题")
    standard: str = Field(..., description="规范代号")
    top_k: int = Field(15, ge=1, le=50)
    skip_rerank: bool = False


class ExpandRequest(BaseModel):
    standard: str
    node_paths: list[str]


class BillMatchRequest(BaseModel):
    description: str
    spec: str
    top_k: int = Field(10, ge=1, le=50)
    code_prefixes: list[str] | None = None


class AuxSearchRequest(BaseModel):
    query: str
    spec: str | None = None
    chapter: str | None = None
    kind: str | None = None
    top_k: int = Field(10, ge=1, le=50)


class PriceRuleSearchRequest(BaseModel):
    query: str
    spec_version: str | None = None
    region: str | None = None
    top_k: int = Field(10, ge=1, le=50)


class EvidenceRequest(BaseModel):
    query: str
    corpus: str
    top_k: int = Field(10, ge=1, le=50)
    standard: str | None = None
    spec: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, Any]:
    ready = sorted({name for name in set(STANDARD_ALIASES.values()) if (_VECTOR_STORE / name).exists()})
    return {"status": "ok", "service": "ce-rag", "ready_standards": ready, "vector_store": str(_VECTOR_STORE)}


@app.post("/search/clause")
def search_clause(req: ClauseSearchRequest) -> dict[str, Any]:
    try:
        return _svc.search_clause(req.query, standard=req.standard, top_k=req.top_k, skip_rerank=req.skip_rerank)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/expand/clauses")
def expand_clauses(req: ExpandRequest) -> dict[str, Any]:
    try:
        return _svc.expand_clause_refs(standard=req.standard, node_paths=req.node_paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/clause/{standard}/{node_path}")
def get_clause(standard: str, node_path: str) -> dict[str, Any]:
    try:
        result = _svc.get_clause(standard=standard, node_path=node_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result["clause"] is None:
        raise HTTPException(status_code=404, detail=f"条款 {node_path} 不存在于 {result['standard']}")
    return result


@app.post("/search/bill-match")
def match_bill(req: BillMatchRequest) -> dict[str, Any]:
    try:
        return _svc.match_bill_item(req.description, spec=req.spec, top_k=req.top_k, code_prefixes=req.code_prefixes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/search/aux-table")
def search_aux_table(req: AuxSearchRequest) -> dict[str, Any]:
    try:
        return _svc.search_aux_table(req.query, spec=req.spec, chapter=req.chapter, kind=req.kind, top_k=req.top_k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/search/price-rule")
def search_price_rule(req: PriceRuleSearchRequest) -> dict[str, Any]:
    try:
        return _svc.search_price_rule(req.query, spec_version=req.spec_version, region=req.region, top_k=req.top_k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/retrieve/evidence")
def retrieve_evidence(req: EvidenceRequest) -> dict[str, Any]:
    try:
        return _svc.retrieve_evidence(
            req.query,
            corpus=req.corpus,
            top_k=req.top_k,
            standard=req.standard,
            spec=req.spec,
            filters=req.filters,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


app.mount("/", rag_mcp.streamable_http_app())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
