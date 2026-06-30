"""Norm-QA 路由 —— ``POST /norm/qa``：造价规范条文检索 + 带引用生成。

流水线：用户问题 → ``knowledge_client.search``（打 :8100 /search 检索造价规范条文）→
``generation.answer``（Qwen3 结构化带引用作答）。检索逻辑不在本层（知识服务唯一 owner），
本层只做编排 + 异常→状态码映射 + 可观测性 meta。
"""
from __future__ import annotations

import logging
import time

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common import knowledge_client
from common.config import LLM_MODEL_ID, LLM_URL
from norm import generation
from norm.standard_router import resolve_standard

logger = logging.getLogger("ce-services.norm")

router = APIRouter(tags=["norm-qa"])


class NormQARequest(BaseModel):
    """Norm-QA 请求体。

    字段：
        query —— 造价/计量/计价类自然语言问题。
        standard —— 规范代号（config.STANDARD_ALIASES，如 gb50854-2024 / gb50500-2013）；**可选 hint**，
                    服务端用 ``standard_router`` 按问题类型确定性地定 family（计量→50854/计价→50500/
                    安装→50856），仅当确定性零命中时回退此 hint（见 T-A2）。错版/选错族由确定性夺回，
                    不再由调用方说了算。
        top_k —— 检索召回条数。
        skip_rerank —— 跳过 cross-encoder 精排（调试用）。
    """

    query: str = Field(..., description="造价规范自然语言问题")
    standard: str | None = Field(
        None, description="规范代号 hint（可选），如 gb50854-2024；服务端按问题类型确定性定族，仅零命中时回退此值")
    top_k: int = 15
    skip_rerank: bool = False


@router.post("/norm/qa")
def norm_qa_endpoint(req: NormQARequest) -> dict:
    """检索造价规范条文 + Qwen3 带引用作答。

    参数：req —— NormQARequest（query / standard / top_k / skip_rerank）。
    返回：``{answer, cited_clauses, uncertain_aspects, out_of_scope_warnings, meta}``；
      知识服务未知规范→400 / 索引未就绪→503、LLM 不可达→502 经 HTTPException 映射。
    """
    t0 = time.perf_counter()
    # ⓪ 规范选择确定化（T-A2）：按问题类型确定性定 family，把选哪部规范从弱模型手里夺回。
    #    req.standard 仅作 hint；family 与 hint 冲突时确定性夺回（治标准漂移 50854→50500）。
    resolution = resolve_standard(req.query, hint=req.standard)
    standard = resolution.standard
    if resolution.overrode_hint:
        logger.warning("standard 漂移拦截：hint=%s → 确定性夺回 %s（intent=%s, matched=%s）",
                       req.standard, standard, resolution.intent, resolution.matched)

    # ① 检索：打知识服务 /search（异常透传其状态码）
    try:
        search_resp = knowledge_client.search(
            req.query, standard=standard, top_k=req.top_k, skip_rerank=req.skip_rerank,
        )
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=code, detail=f"知识服务检索失败: {detail}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"知识服务不可达: {exc}") from exc

    clauses = search_resp.get("clauses", [])
    retrieve_ms = (time.perf_counter() - t0) * 1000
    if not clauses:
        # 无召回：不喂空上下文给 LLM 编答案，直接返回"无依据"
        return {
            "answer": "未在所选造价规范中检索到与问题相关的条文，无法作答。本回答仅供参考，不替代专业造价审核。",
            "cited_clauses": [],
            "uncertain_aspects": ["检索零召回，可能问题超出该规范范围或需换用其它规范"],
            "out_of_scope_warnings": [],
            "meta": {"standard": standard, "retrieved": 0,
                     "retrieve_ms": round(retrieve_ms),
                     "standard_resolution": resolution.as_meta(),
                     "search_meta": search_resp.get("meta", {})},
        }

    # ② 生成：检索条文 → 结构化带引用回答
    t1 = time.perf_counter()
    try:
        result = generation.answer(req.query, clauses, LLM_URL, LLM_MODEL_ID)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"LLM 生成服务不可达: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 子类
        raise HTTPException(status_code=502, detail=f"LLM 输出非合法 JSON: {exc}") from exc
    generate_ms = (time.perf_counter() - t1) * 1000

    logger.info("/norm/qa standard=%s (hint=%s src=%s) retrieved=%d retrieve=%.0fms generate=%.0fms",
                standard, req.standard, resolution.source, len(clauses), retrieve_ms, generate_ms)
    result["meta"] = {
        "standard": standard,
        "retrieved": len(clauses),
        "retrieve_ms": round(retrieve_ms),
        "generate_ms": round(generate_ms),
        "standard_resolution": resolution.as_meta(),
        "search_meta": search_resp.get("meta", {}),
    }
    return result
