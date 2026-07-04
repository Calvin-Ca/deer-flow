"""路由/编排端点 —— ``POST /route``（前置路由 T-A1）+ ``POST /orchestrate``（复合编排 T-A4）。

``/route``：确定性能力分流 + 形态判定（无 LLM），给 query → RouteDecision，供编排器/agent 分流前置。
``/orchestrate``：单一意图直派能力层 / 复合意图（EH-01）32b 拆解 → 子任务回 ① 路由 → 派发 → 综合。
"""
from __future__ import annotations

import logging

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routing.intent_fallback import route_hybrid
from routing.orchestrator import orchestrate
from routing.prerouter import route

logger = logging.getLogger("ce-services.routing")

router = APIRouter(tags=["routing"])


class RouteRequest(BaseModel):
    """前置路由请求体。

    字段：
        query —— 用户自然语言请求。
        has_project_context —— 调用方已知是否挂了 BOQ/算量数据（覆盖文本推断）；缺省 None 则只看文本。
        use_llm_fallback —— 是否启用意图混合路由的 LLM 兜底（低置信时 32b 补能力分类）；
          缺省 None 走**纯确定性**（默认关，保端点无 LLM、金标可回归），显式 True 才启用兜底。
    """

    query: str = Field(..., description="用户自然语言请求")
    has_project_context: bool | None = Field(
        None, description="是否已挂项目上下文（BOQ/算量）；None 则由文本推断")
    use_llm_fallback: bool = Field(
        False, description="低置信时启用 32b LLM 兜底分类（默认关，保端点纯确定性）")


@router.post("/route")
def route_endpoint(req: RouteRequest) -> dict:
    """前置路由：返回 capability 落点 + 四维信号 + 形态标记 + 置信度（供下游消费，本端点不执行能力）。

    默认**纯确定性**（无 LLM，金标 routing_eval 可回归）；``use_llm_fallback=true`` 时对低置信
    query（落 norm 兜底、只泛词/纯默认）调 32b 兜底补能力分类（红线闸仍确定性重推，见 intent_fallback）。
    返回：``RouteDecision.as_meta()`` —— 含 ``route_confidence``（high/low）与 ``route_source``
      （deterministic/llm_fallback）。
    """
    if req.use_llm_fallback:
        decision = route_hybrid(req.query, has_project_context=req.has_project_context,
                                use_llm_fallback=True)
    else:
        decision = route(req.query, has_project_context=req.has_project_context)
    logger.info("/route cap=%s clarify=%s src=%s conf=%s route_src=%s",
                decision.capability, decision.clarify, decision.source_type,
                decision.route_confidence, decision.route_source)
    return decision.as_meta()


class OrchestrateRequest(BaseModel):
    """复合编排请求体。

    字段：
        query —— 用户请求（可为复合，如「先套定额、再算合价、并判断合规」）。
        has_project_context —— 是否已挂 BOQ/算量（透传前置路由）。
    """

    query: str = Field(..., description="用户请求（可复合）")
    has_project_context: bool | None = Field(None, description="是否已挂项目上下文（BOQ/算量）")


@router.post("/orchestrate")
def orchestrate_endpoint(req: OrchestrateRequest) -> dict:
    """复合编排（T-A4）：单一意图直派 / 复合意图 32b 拆解→子任务回①路由→派发→综合。

    返回：单一 ``{mode:"single", route, result}``；复合 ``{mode:"compound", route, subtasks, answer}``。
      拆解/综合 LLM 降级安全（失败→单任务/确定性拼接），单个子任务失败不拖垮整单。
    返回 502 仅当能力层依赖（知识服务/LLM）整体不可达且未被内部降级吸收。
    """
    try:
        return orchestrate(req.query, has_project_context=req.has_project_context)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"依赖服务不可达（知识服务 :8100 / LLM）: {exc}") from exc
