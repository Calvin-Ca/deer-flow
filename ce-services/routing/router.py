"""前置路由端点 —— ``POST /route``：确定性能力分流 + 形态判定（T-A1）。

无状态、无 LLM、一把出结果：给 query（+ 可选「是否已挂项目上下文」）→ RouteDecision。供
（未来）复合编排器 T-A4 / lead-agent 在调用具体能力前做确定性分流落点与形态标记。检索/生成均不在本层。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

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
