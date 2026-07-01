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

from cost import orchestration
from cost.pricing import RollupInput, UnitPriceInput, compute_unit_price, rollup_cost

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
            req.description, req.spec, req.region, top_k=req.top_k,
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


class SessionStartRequest(BaseModel):
    """HITL 组价会话起始请求体。

    字段：feature —— 构件/做法描述（必填）；spec —— 国标版本 2013/2024（缺则首个闸为 setup 录入）；
      region —— 地区（默认深圳）；period —— 信息价期号；price_source —— 信息价来源（local/online/manual）；
      rates —— 可选费率块（给定则末步算综合单价）；quantity —— 可选工程量 Q（给定则 quantity_gate 自动过）；
      features —— 多构件描述列表（给定则逐件办，优先于 feature）。
    """

    feature: str | None = Field(None, description="单构件/做法描述（features 给定时可省）")
    features: list[str] | None = Field(None, description="多构件描述列表，逐件外层循环办，优先于 feature")
    spec: str | None = Field(None, description="国标版本 2013/2024，缺则 setup 闸采集")
    region: str = Field("深圳", description="地区，默认深圳")
    period: str | None = Field(None, description="信息价期号（年月）")
    price_source: str | None = Field(None, description="信息价来源 local/online/manual")
    rates: dict | None = Field(None, description="可选费率块")
    quantity: float | None = Field(None, gt=0, description="可选工程量 Q（清单数量，>0，仅单构件预供），缺则 quantity_gate 录入")


class SessionResumeRequest(BaseModel):
    """HITL 会话恢复请求体 —— 闸的用户决策。

    字段：decision —— confirm 闸传 ``{action: approve|select_alternative|manual_override, value?}``；
      input 闸（setup/缺价录入）传字段值 dict（如 ``{value: 480}`` 或 ``{spec_version:"2024", ...}``）。
    """

    decision: dict = Field(..., description="confirm 动作或 input 字段值")


class SessionRewindRequest(BaseModel):
    """HITL 会话回退请求体 —— 回到某个已过的闸重答。

    字段：to_node —— 目标闸节点名（setup / feature_gate / list_gate / quota_gate / price_gate /
      quantity_gate / rates_gate / params_gate / rollup）；其后的已钉值随回退作废、重新确认。
    """

    to_node: str = Field(..., description="回退目标闸节点名")


def _map_session_exc(exc: Exception) -> HTTPException:
    """把会话推进时底层原语的异常映射成 HTTPException（沿用 compose 端点同款映射）。"""
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        return HTTPException(status_code=code, detail=f"依赖服务返回错误: {detail}")
    if isinstance(exc, requests.RequestException):
        return HTTPException(status_code=503, detail=f"依赖服务不可达（知识服务 :8100 / LLM :8099）: {exc}")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=502, detail=f"LLM 选码输出非合法 JSON: {exc}")
    raise exc


@router.post("/cost/session/start")
def cost_session_start_endpoint(req: SessionStartRequest) -> dict:
    """起一个可中断组价 HITL 会话，跑到首个闸或 done。

    参数：req —— ``SessionStartRequest``。返回：``{task_id, status, interrupt, events, items, overrides, audit_log}``；
      首个 interrupt 通常是编码确认闸（低置信/多候选时），或 spec 缺失时的 setup 录入闸；高置信可一路自动过。
      底层原语异常（知识服务 / LLM）按 compose 端点同款状态码映射。
    """
    from cost import session  # 懒加载：隔离 langgraph 依赖，不影响 /cost/compose 简单路径
    if not req.feature and not (req.features and any(f for f in req.features)):
        raise HTTPException(status_code=422, detail="需提供 feature（单构件）或 features（多构件，非空）")
    try:
        result = session.start(req.feature, spec=req.spec, region=req.region,
                               period=req.period, price_source=req.price_source, rates=req.rates,
                               quantity=req.quantity, features=req.features)
    except (requests.RequestException, ValueError) as exc:
        raise _map_session_exc(exc) from exc
    logger.info("/cost/session/start task=%s status=%s feature=%s", result.get("task_id"),
                result.get("status"), req.feature)
    return result


@router.post("/cost/session/{task_id}/resume")
def cost_session_resume_endpoint(task_id: str, req: SessionResumeRequest) -> dict:
    """以用户决策续跑会话到下个闸或 done。

    参数：task_id —— 会话标识；req.decision —— 闸的用户输入。
    返回：会话响应（下个 interrupt 或终态）；未知 task_id 由 langgraph 视为新线程、状态空（next 为 START）。
    """
    from cost import session
    try:
        result = session.resume(task_id, req.decision)
    except (requests.RequestException, ValueError) as exc:
        raise _map_session_exc(exc) from exc
    logger.info("/cost/session/%s/resume status=%s", task_id, result.get("status"))
    return result


@router.post("/cost/session/{task_id}/rewind")
def cost_session_rewind_endpoint(task_id: str, req: SessionRewindRequest) -> dict:
    """回退会话到某个已过的闸重答（丢弃其后已钉值），重新停在该闸。

    参数：task_id —— 会话标识；req.to_node —— 目标闸节点名。
    返回：会话响应（重新停在该闸的 interrupt）；to_node 非法/未到达 → status=error（业务错误，非 HTTP 错误）。
      底层 langgraph 时间旅行（``get_state_history`` + 从历史 checkpoint 重 invoke）；上游 compute 不重跑。
    """
    from cost import session
    try:
        result = session.rewind(task_id, req.to_node)
    except (requests.RequestException, ValueError) as exc:
        raise _map_session_exc(exc) from exc
    logger.info("/cost/session/%s/rewind to=%s status=%s", task_id, req.to_node, result.get("status"))
    return result


@router.get("/cost/session/{task_id}/state")
def cost_session_state_endpoint(task_id: str) -> dict:
    """读会话当前持久化状态（不推进图）。

    参数：task_id。返回：``{task_id, status, next, values}``——values 为完整 §5.4 状态文档；
      task_id 不存在 → status="unknown"、values 空（langgraph 无该线程 checkpoint）。
    """
    from cost import session
    return session.get_state(task_id)


@router.post("/cost/unit-price")
def cost_unit_price_endpoint(req: UnitPriceInput) -> dict:
    """综合单价计算原语（P2 ``compute_unit_price`` 的 HTTP 表面）——人材机费 + 费率 → 综合单价 →（可选）含税。

    参数：req —— ``UnitPriceInput``；FastAPI 据其 pydantic schema 自动校验，非法入参（负数 / NaN/Inf /
      缺取费基数 / 多余字段）直接 422——这就是「算钱那步的 pydantic 闸门」，无论谁/哪条路径调用都被拦在边界。
    返回：``compute_unit_price`` 结果（综合单价 + 六项构成 + 综合合价 + 可选含税 + 溯源 + 红线声明）。
      纯确定性、不入 LLM；费率由调用方给定，本端点不杜撰、不内置地区默认。
    """
    return compute_unit_price(req)


@router.post("/cost/rollup")
def cost_rollup_endpoint(req: RollupInput) -> dict:
    """总造价汇总原语（§13 ``rollup_cost`` 的 HTTP 表面）——分部分项 + 措施/其他/规费 →（可选税金）总造价。

    参数：req —— ``RollupInput``；FastAPI 据 pydantic schema 自动校验，负金额/NaN/Inf/多余字段 → 422 闸门。
    返回：``rollup_cost`` 结果（税前造价 + 可选税金/总造价 + 溯源 + 红线声明）。纯确定性、不入 LLM；
      税率与项目级费用由调用方给定，本端点不杜撰、不内置默认（不替用户定政策数）。
    """
    return rollup_cost(req)
