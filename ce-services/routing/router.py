"""路由/编排端点 —— ``POST /route``（前置路由 T-A1）+ ``POST /orchestrate``（复合编排 T-A4）。

``/route``：确定性能力分流 + 形态判定（无 LLM），给 query → RouteDecision，供编排器/agent 分流前置。
``/orchestrate``：单一意图直派能力层 / 复合意图（EH-01）32b 拆解 → 子任务回 ① 路由 → 派发 → 综合。
"""
from __future__ import annotations

import logging

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routing.orchestrator import orchestrate
from routing.prerouter import route

logger = logging.getLogger("ce-services.routing")

router = APIRouter(tags=["routing"])


class RouteRequest(BaseModel):
    """前置路由请求体。

    字段：
        query —— 用户自然语言请求。
        has_project_context —— 调用方已知是否挂了 BOQ/算量数据（覆盖文本推断）；缺省 None 则只看文本。
    """

    query: str = Field(..., description="用户自然语言请求")
    has_project_context: bool | None = Field(
        None, description="是否已挂项目上下文（BOQ/算量）；None 则由文本推断")


@router.post("/route")
def route_endpoint(req: RouteRequest) -> dict:
    """确定性前置路由：返回 capability 落点 + 四维信号 + 形态标记（供下游消费，本端点不执行能力）。

    返回：``RouteDecision.as_meta()`` —— ``{capability, source_type, needs_calc, needs_context,
      intent_count, feature_complete, caliber_complete, clarify, matched, reasons}``。
    """
    decision = route(req.query, has_project_context=req.has_project_context)
    logger.info("/route cap=%s clarify=%s src=%s ctx=%s calc=%s",
                decision.capability, decision.clarify, decision.source_type,
                decision.needs_context, decision.needs_calc)
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
