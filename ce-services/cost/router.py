"""CostAgent 路由 —— ``POST /cost/compose``：构件 → 选码 → 组价取数。

流水线在 ``cost.orchestration.compose``（bill_match → select_code → price_compose）；本层只做
HTTP 编排 + 异常→状态码映射 + 可观测性 meta。选码红线（need_review / 不造码）在 selection 层，
组价取数缺口（no_source / 2013 未就绪）在 orchestration 层透传，本层原样冒泡到响应。
"""
from __future__ import annotations

import logging
import time

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.config import LLM_MODEL_ID, LLM_URL
from cost import orchestration
from cost.pricing import UnitPriceInput, compute_unit_price

logger = logging.getLogger("ce-services.cost")

router = APIRouter(tags=["cost-agent"])


class PricingRates(BaseModel):
    """组价费率块（可选）—— 给定则 ``/cost/compose`` 末步对每条定额算综合单价（确定性、不入 LLM）。

    字段：management_fee_rate / profit_rate —— 管理费率 / 利润率（%，库内无须 HITL 给定）；
      risk_rate —— 风险费率（%，默认 0）；fee_base —— 取费基数（labor / labor_machine / lmm，必填）；
      tax_rate —— 增值税率（%，可选）。缺该块时 ``/cost/compose`` 维持 P1 行为（仅选码 + 取数，不算钱）。
    """

    management_fee_rate: float = Field(..., description="管理费率 %")
    profit_rate: float = Field(..., description="利润率 %")
    risk_rate: float = Field(0.0, description="风险费率 %，默认 0")
    fee_base: str = Field(..., description="取费基数：labor / labor_machine / lmm")
    tax_rate: float | None = Field(None, description="增值税率 %，可选")


class CostComposeRequest(BaseModel):
    """CostAgent 组价请求体。

    字段：
        description —— 构件/做法自然语言描述。
        spec —— 国标版本（2013/2024）；**必填**，按版本隔离清单库/组价取数，不设默认避免错版串库。
        region —— 地区（如「深圳」），用于信息价/定额取数。
        top_k —— 清单候选召回数。
        rates —— 可选费率块（``PricingRates``）；给定则末步算综合单价，否则维持 P1 仅选码+取数。
    """

    description: str = Field(..., description="构件/做法描述")
    spec: str = Field(..., description="国标版本 2013/2024")
    region: str = Field("深圳", description="地区，默认深圳")
    top_k: int = 10
    rates: PricingRates | None = Field(None, description="可选费率块，给定则算综合单价")


@router.post("/cost/compose")
def cost_compose_endpoint(req: CostComposeRequest) -> dict:
    """构件描述 → 候选召回 → LLM 选码 → 组价取数。

    参数：req —— CostComposeRequest（description / spec / region / top_k）。
    返回：``{description, spec, region, candidates_count, selection, code, price, price_status, meta}``；
      知识服务未知 spec→400 / 不可达→503、组价清单不存在→404、LLM 不可达→503 经 HTTPException 映射。
      选码 need_review 与 price_status（skipped / 未就绪 / ok）原样返回，由调用方据此走 HITL。
    """
    t0 = time.perf_counter()
    try:
        result = orchestration.compose(
            req.description, req.spec, req.region, LLM_URL, LLM_MODEL_ID, top_k=req.top_k,
            rates=req.rates.model_dump() if req.rates else None,
        )
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=code, detail=f"依赖服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"依赖服务不可达（知识服务 :8100 / LLM :8099）: {exc}") from exc
    except ValueError as exc:  # call_qwen3 的 json.JSONDecodeError（ValueError 子类）
        raise HTTPException(status_code=502, detail=f"LLM 选码输出非合法 JSON: {exc}") from exc

    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("/cost/compose spec=%s region=%s candidates=%d code=%s need_review=%s price_status=%s (%.0fms)",
                req.spec, req.region, result.get("candidates_count", 0), result.get("code"),
                result.get("selection", {}).get("need_review"), result.get("price_status"), elapsed_ms)
    result["meta"] = {"elapsed_ms": round(elapsed_ms), "top_k": req.top_k}
    return result


@router.post("/cost/unit-price")
def cost_unit_price_endpoint(req: UnitPriceInput) -> dict:
    """综合单价计算原语（P2 ``compute_unit_price`` 的 HTTP 表面）——人材机费 + 费率 → 综合单价 →（可选）含税。

    参数：req —— ``UnitPriceInput``；FastAPI 据其 pydantic schema 自动校验，非法入参（负数 / NaN/Inf /
      缺取费基数 / 多余字段）直接 422——这就是「算钱那步的 pydantic 闸门」，无论谁/哪条路径调用都被拦在边界。
    返回：``compute_unit_price`` 结果（综合单价 + 六项构成 + 综合合价 + 可选含税 + 溯源 + 红线声明）。
      纯确定性、不入 LLM；费率由调用方给定，本端点不杜撰、不内置地区默认。
    """
    return compute_unit_price(req)
