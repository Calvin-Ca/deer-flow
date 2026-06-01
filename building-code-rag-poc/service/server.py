#!/usr/bin/env python3
"""建筑规范 RAG —— 常驻 HTTP 服务（方案 A）。

为什么存在：
  deer-flow 的沙箱只把 ``skills/`` 挂进 agent 可见范围，``building-code-rag-poc/``
  （venv、05/06/10 脚本、向量索引数据）都不在其中。把检索/合规能力做成常驻
  HTTP 服务后，skill 侧只需用标准库 urllib 打 HTTP，沙箱内**零依赖、零 venv、
  零数据目录**，彻底摆脱"硬编码宿主路径 + 动态加载 POC 脚本"的脆弱假设。

可观测性（方案 A 核心）：
  - 服务端按 ``request_id`` 打分步日志（参数提取 / 各阶段命中数 / 生成耗时）；
  - 响应 JSON 带 ``meta`` 字段（bm25_hits、vector_hits、expanded、final、
    mandatory、elapsed_ms 等），skill 调用方可在前端那一个 bash 步骤里看到过程摘要。

端点：
  GET  /health                 健康检查（含已就绪的 standard 列表）
  POST /retrieve               单查询检索 + Qwen3 结构化生成（对应 building-code-rag skill）
  POST /compliance             项目级合规检查（对应 compliance-check skill）

启动（服务器上）：
  cd /mnt/nvme/calvin/code/deer-flow/building-code-rag-poc
  .venv/bin/python -m uvicorn service.server:app --host 0.0.0.0 --port 8100
  # 或： .venv/bin/python service/server.py
"""
from __future__ import annotations

import importlib.util
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ── 路径与默认配置 ────────────────────────────────────────────────────────────
_SERVICE_DIR = Path(__file__).resolve().parent
_POC_ROOT = _SERVICE_DIR.parent
_POC_SCRIPTS = _POC_ROOT / "scripts"
_VECTOR_STORE = _POC_ROOT / "data" / "vector_store"

DEFAULTS = {
    "milvus_host": "localhost",
    "milvus_port": 19530,
    "embed_url": "http://localhost:8097",
    "embed_model_id": "/model",
    "llm_url": "http://localhost:8099",
    "llm_model_id": "qwen3-8b",
    "top_k": 20,
}

# 规范代号别名 → vector_store 目录名（与旧 retrieve.py 保持一致）
STANDARD_ALIASES: dict[str, str] = {
    "gb50016": "GB_50016-20142018",
    "gb50016-2014": "GB_50016-20142018",
    "gb50016-20142018": "GB_50016-20142018",
    "GB_50016-20142018": "GB_50016-20142018",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("building-code-rag.service")


# ── POC 模块懒加载（进程内只加载一次）──────────────────────────────────────────

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MODULES: dict[str, Any] = {}


def _mod(key: str, filename: str):
    if key not in _MODULES:
        _MODULES[key] = _load_module(f"poc_{key}", _POC_SCRIPTS / filename)
    return _MODULES[key]


def _resolve_store_dir(standard: str) -> tuple[Path, str]:
    store_name = STANDARD_ALIASES.get(standard) or STANDARD_ALIASES.get(standard.lower())
    if not store_name:
        raise HTTPException(
            status_code=400,
            detail=f"未知规范代号: {standard!r}，支持: {sorted(set(STANDARD_ALIASES))}",
        )
    store_dir = _VECTOR_STORE / store_name
    if not store_dir.exists():
        # 这是**服务端配置问题**，不是调用方能在沙箱里补救的——明确说清楚
        raise HTTPException(
            status_code=503,
            detail=(
                f"向量索引未就绪（{store_dir} 不存在）。这是服务端配置问题，"
                f"请在服务器上构建索引：uv run scripts/04_build_index.py --standard {standard}"
            ),
        )
    return store_dir, store_name


def _collection_name(store_name: str) -> str:
    return f"building_code_{store_name}".lower().replace("-", "_")


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────

class RetrieveRequest(BaseModel):
    query: str = Field(..., description="自然语言查询")
    standard: str = "gb50016"
    top_k: int = DEFAULTS["top_k"]
    skip_rerank: bool = False


class ComplianceRequest(BaseModel):
    project: str = Field(..., description="项目自由文本描述")
    standard: str = "gb50016"
    skip_reflection: bool = False


app = FastAPI(title="Building Code RAG Service", version="1.0.0")


@app.get("/health")
def health() -> dict:
    ready = sorted(
        {
            name
            for name in set(STANDARD_ALIASES.values())
            if (_VECTOR_STORE / name).exists()
        }
    )
    return {
        "status": "ok",
        "ready_standards": ready,
        "vector_store": str(_VECTOR_STORE),
        "deps": {k: DEFAULTS[k] for k in ("embed_url", "llm_url", "milvus_host", "milvus_port")},
    }


@app.post("/retrieve")
def retrieve(req: RetrieveRequest) -> dict:
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    store_dir, store_name = _resolve_store_dir(req.standard)
    logger.info("[%s] /retrieve standard=%s top_k=%d query=%r", rid, store_name, req.top_k, req.query)

    retrieve_mod = _mod("retrieve", "05_retrieve.py")
    generate_mod = _mod("generate", "06_generate.py")
    collection = _collection_name(store_name)

    # 1. 检索（stats 回填各阶段命中数）
    stats: dict[str, int] = {}
    try:
        clauses = retrieve_mod.retrieve(
            query=req.query,
            store_dir=store_dir,
            milvus_host=DEFAULTS["milvus_host"],
            milvus_port=DEFAULTS["milvus_port"],
            collection_name=collection,
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

    t_retrieved = time.perf_counter()
    logger.info(
        "[%s] 检索完成 bm25=%s vector=%s merged=%s expanded=%s final=%s 强条=%s (%.0fms)",
        rid, stats.get("bm25_hits"), stats.get("vector_hits"), stats.get("merged"),
        stats.get("expanded"), stats.get("final"), stats.get("mandatory"),
        (t_retrieved - t0) * 1000,
    )

    # 逐条输出：汇总行 + 列表头 + 每条条款
    n_mandatory = sum(1 for c in clauses if c.get("is_mandatory"))
    logger.info("[%s] ── 命中 %d 条（强条 %d）", rid, len(clauses), n_mandatory)
    # 列表头：字符位与数据行对齐（以 ★强条 为基准：indent=3, tag=3, sep=2）
    logger.info("[%s]   标识   条款号         页码      来源        引用关系", rid)
    for c in clauses:
        refs_raw = c.get("references_to", [])
        refs: list[str] = (
            json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
        )
        mandatory_tag = "★强条" if c.get("is_mandatory") else "  推荐"
        ref_str = ""
        if refs:
            preview = refs[:4]
            tail = f"+{len(refs)-4}" if len(refs) > 4 else ""
            ref_str = f"  引用→[{', '.join(preview)}{tail}]"
        logger.info(
            "[%s]   %s  %-10s  p.%-4d  %-10s%s",
            rid, mandatory_tag, c.get("clause_path", "?"),
            c.get("page", 0), c.get("_source", ""), ref_str,
        )

    # 2. 生成（Qwen3 结构化回答）
    try:
        user_msg = generate_mod.build_user_message(req.query, clauses)
        response = generate_mod.call_qwen3(
            generate_mod.SYSTEM_PROMPT,
            user_msg,
            DEFAULTS["llm_url"],
            DEFAULTS["llm_model_id"],
        )
    except Exception as exc:
        logger.exception("[%s] 生成失败", rid)
        raise HTTPException(
            status_code=500,
            detail=f"生成失败: {exc}（检索到 {len(clauses)} 条，可重试）",
        ) from exc

    t_done = time.perf_counter()
    logger.info("[%s] 生成完成 (%.0fms) 总耗时 %.0fms", rid, (t_done - t_retrieved) * 1000, (t_done - t0) * 1000)

    return {
        "query": req.query,
        "standard": store_name,
        "retrieved_clauses_count": len(clauses),
        "mandatory_clauses_count": stats.get("mandatory", 0),
        "response": response,
        "meta": {
            "request_id": rid,
            **stats,
            "retrieve_ms": round((t_retrieved - t0) * 1000),
            "generate_ms": round((t_done - t_retrieved) * 1000),
            "elapsed_ms": round((t_done - t0) * 1000),
        },
    }


@app.post("/compliance")
def compliance(req: ComplianceRequest) -> dict:
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    store_dir, store_name = _resolve_store_dir(req.standard)
    logger.info("[%s] /compliance standard=%s skip_reflection=%s project=%r",
                rid, store_name, req.skip_reflection, req.project)

    check_mod = _mod("compliance", "10_compliance_check.py")
    try:
        report = check_mod.compliance_check(
            description=req.project,
            store_dir=store_dir,
            llm_url=DEFAULTS["llm_url"],
            model_id=DEFAULTS["llm_model_id"],
            skip_reflection=req.skip_reflection,
        )
    except Exception as exc:
        logger.exception("[%s] 合规检查失败", rid)
        raise HTTPException(status_code=500, detail=f"合规检查失败: {exc}") from exc

    elapsed = round((time.perf_counter() - t0) * 1000)
    n_dims = len(report.get("dimensions", []))
    logger.info("[%s] 合规检查完成 维度=%d 强条=%s (%.0fms)",
                rid, n_dims, report.get("mandatory_clauses_total"), elapsed)

    report["meta"] = {
        "request_id": rid,
        "standard": store_name,
        "dimensions_count": n_dims,
        "elapsed_ms": elapsed,
    }
    return report


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
