"""Norm-QA 路由 —— ``POST /norm/qa``：造价规范条文检索 + 带引用生成。

编排内核在 ``norm.pipeline.answer_query``（resolve_standard→检索→零召回/生成→校验闸→meta），
本层只做 HTTP 编排 + 异常→状态码映射。内核与复合编排器共用同一实现；旧任务层 MCP façade 不再作为主入口挂载。
"""
from __future__ import annotations

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.config import LLM_MODEL_ID, LLM_URL
from norm import pipeline

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
    """检索造价规范条文 + Qwen3 带引用作答（编排内核见 ``norm.pipeline.answer_query``）。

    参数：req —— NormQARequest（query / standard / top_k / skip_rerank）。
    返回：``{answer, cited_clauses, uncertain_aspects, out_of_scope_warnings, meta}``；
      知识服务未知规范→400 / 索引未就绪→503、LLM 不可达或输出非法→502 经 HTTPException 映射。
    """
    try:
        return pipeline.answer_query(
            req.query, standard_hint=req.standard, llm_url=LLM_URL, model_id=LLM_MODEL_ID,
            top_k=req.top_k, skip_rerank=req.skip_rerank,
        )
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=code, detail=f"知识服务检索失败: {detail}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"依赖服务不可达（知识服务 :8100 / LLM）: {exc}") from exc
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 子类（LLM 输出非法 JSON）
        raise HTTPException(status_code=502, detail=f"LLM 输出非合法 JSON: {exc}") from exc
