#!/usr/bin/env python3
"""建筑规范 RAG —— 知识服务（常驻 HTTP，端口 8100）。

职责单一：**只对底层知识库提供检索原语**（裸检索 / 引用扩展 / 单条款直取）。生成
（问答）与编排（合规）已拆到顶层 ``services/`` 任务层（qa :8102 / compliance :8101），
它们作为本服务的纯 HTTP 客户端，HTTP 调本服务的 /search。检索逻辑来自 ``retrieval``
包——本文件只做 HTTP ↔ 函数调用的翻译 + 可观测性日志，是 retrieval + rerank 的唯一
进程内 owner（索引、rerank 模型只在此加载一份）。

为什么是 HTTP 服务：
  deer-flow 沙箱只把 ``skills/`` 挂进 agent 可见范围，``ce-code/``
  （venv、向量索引数据、retrieval 代码）都不在其中。做成常驻服务后，沙箱内的 skill
  与服务器上的任务层都只需打 HTTP，沙箱侧**零依赖、零 venv、零数据目录**。

端点（原语）：
  GET  /health                       健康检查（含已就绪的 standard 列表）
  POST /search                       裸检索（条款 + meta，无生成）—— qa/compliance/算量/审图复用
  POST /expand                       对给定 clause_path 做引用图扩展
  GET  /clause/{standard}/{path}     单条款直取

启动（服务器上）：
  cd /mnt/nvme/calvin/code/deer-flow/ce-code
  .venv/bin/python service/server.py
  # 或： .venv/bin/python -m uvicorn service.server:app --host 0.0.0.0 --port 8100
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# 让 `python service/server.py` 与 `uvicorn service.server:app` 两种启动方式都能
# import retrieval / service（POC 根加入 sys.path）
_SERVICE_DIR = Path(__file__).resolve().parent
_POC_ROOT = _SERVICE_DIR.parent
sys.path.insert(0, str(_POC_ROOT))

from retrieval.config import (  # noqa: E402
    DEFAULTS,
    STANDARD_ALIASES,
    collection_name,
    resolve_store_dir,
)
from retrieval.engine import expand_references, get_clause, load_metadata, search  # noqa: E402

_VECTOR_STORE = _POC_ROOT / "data" / "vector_store"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ce-code.service")


# ── 路径解析 ──────────────────────────────────────────────────────────────────

def _resolve(standard: str) -> tuple[Path, str]:
    """规范代号 → (store 目录, 规范化名)；语义化错误用 HTTP 状态码表达。"""
    try:
        store_dir, store_name = resolve_store_dir(standard, _VECTOR_STORE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not store_dir.exists():
        # 服务端配置问题，调用方无法在沙箱里补救——明确说清楚
        raise HTTPException(
            status_code=503,
            detail=(
                f"向量索引未就绪（{store_dir} 不存在）。这是服务端配置问题，"
                f"请在服务器上构建索引：uv run pipeline/04_build_index.py --standard {standard}"
            ),
        )
    return store_dir, store_name


# ── 请求模型 ──────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., description="自然语言查询")
    standard: str = "gb50016"
    top_k: int = DEFAULTS["top_k"]
    skip_rerank: bool = False


class ExpandRequest(BaseModel):
    clause_paths: list[str] = Field(..., description="种子条款号列表")
    standard: str = "gb50016"


app = FastAPI(title="Building Code RAG · Retrieval Service", version="2.0.0")


# ── 检索步骤（/search 用，含逐条日志可观测性）────────────────────────────────

def _run_search(req: SearchRequest, rid: str) -> tuple[list[dict], dict, str, float]:
    store_dir, store_name = _resolve(req.standard)
    logger.info("[%s] search standard=%s top_k=%d query=%r", rid, store_name, req.top_k, req.query)

    stats: dict = {}
    t0 = time.perf_counter()
    try:
        clauses = search(
            query=req.query,
            store_dir=store_dir,
            milvus_host=DEFAULTS["milvus_host"],
            milvus_port=DEFAULTS["milvus_port"],
            collection_name=collection_name(store_name),
            embed_url=DEFAULTS["embed_url"],
            embed_model_id=DEFAULTS["embed_model_id"],
            top_k=req.top_k,
            bm25_top_k=req.top_k * 2,
            vector_top_k=req.top_k * 2,
            skip_rerank=req.skip_rerank,
            stats=stats,
        )
    except Exception as exc:
        logger.exception("[%s] 检索失败", rid)
        raise HTTPException(status_code=500, detail=f"检索失败: {exc}") from exc

    retrieve_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "[%s] 检索完成 bm25=%s vector=%s merged=%s expanded=%s final=%s (%.0fms)",
        rid, stats.get("bm25_hits"), stats.get("vector_hits"), stats.get("merged"),
        stats.get("expanded"), stats.get("final"), retrieve_ms,
    )
    _log_clauses(rid, clauses)
    return clauses, stats, store_name, retrieve_ms


def _log_clauses(rid: str, clauses: list[dict]) -> None:
    """逐条输出：汇总行 + 列表头 + 每条条款（溯源可观测性）。"""
    logger.info("[%s] ── 命中 %d 条", rid, len(clauses))
    logger.info("[%s]   条款号         页码      来源        引用关系", rid)
    for c in clauses:
        refs_raw = c.get("references_to", [])
        refs = json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
        ref_str = ""
        if refs:
            preview = refs[:4]
            tail = f"+{len(refs) - 4}" if len(refs) > 4 else ""
            ref_str = f"  引用→[{', '.join(preview)}{tail}]"
        logger.info(
            "[%s]   %-10s  p.%-4d  %-10s%s",
            rid, c.get("clause_path", "?"),
            c.get("page", 0), c.get("_source", ""), ref_str,
        )


# ── 端点 ──────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    ready = sorted(
        {name for name in set(STANDARD_ALIASES.values())
         if (_VECTOR_STORE / name).exists()}
    )
    return {
        "status": "ok",
        "service": "retrieval",
        "ready_standards": ready,
        "vector_store": str(_VECTOR_STORE),
        "deps": {k: DEFAULTS[k] for k in ("embed_url", "llm_url", "milvus_host", "milvus_port")},
    }


@app.post("/search")
def search_endpoint(req: SearchRequest) -> dict:
    """裸检索：只返回条款 + meta，不做生成。供算量/审图等 agent 自由组合。"""
    rid = uuid.uuid4().hex[:8]
    clauses, stats, store_name, retrieve_ms = _run_search(req, rid)
    return {
        "query": req.query,
        "standard": store_name,
        "retrieved_clauses_count": len(clauses),
        "clauses": clauses,
        "meta": {
            "request_id": rid,
            **stats,
            "retrieve_ms": round(retrieve_ms),
            "elapsed_ms": round(retrieve_ms),
        },
    }


@app.post("/expand")
def expand_endpoint(req: ExpandRequest) -> dict:
    """对给定 clause_path 做一跳引用图扩展，返回新增的关联条款。"""
    rid = uuid.uuid4().hex[:8]
    store_dir, store_name = _resolve(req.standard)
    metadata = load_metadata(store_dir)
    seed_set = set(req.clause_paths)
    seeds = [m for m in metadata if m.get("clause_path") in seed_set]
    expanded = expand_references(seeds, metadata)
    added = [c for c in expanded if c.get("clause_path") not in seed_set]
    logger.info("[%s] /expand standard=%s seeds=%d → 新增 %d 条",
                rid, store_name, len(seeds), len(added))
    return {
        "standard": store_name,
        "seeds": req.clause_paths,
        "expanded_clauses": added,
        "meta": {"request_id": rid, "seeds": len(seeds), "added": len(added)},
    }


@app.get("/clause/{standard}/{clause_path}")
def clause_endpoint(standard: str, clause_path: str) -> dict:
    """按条款号直取单条款。"""
    store_dir, store_name = _resolve(standard)
    clause = get_clause(store_dir, clause_path)
    if clause is None:
        raise HTTPException(status_code=404, detail=f"条款 {clause_path} 不存在于 {store_name}")
    return {"standard": store_name, "clause_path": clause_path, "clause": clause}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
