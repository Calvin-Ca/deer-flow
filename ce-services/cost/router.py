"""CostAgent 确定性原语路由 —— BOQ 核对 + 算钱（综合单价/总造价）+ HITL 门控判断原语。

组价的**有状态 HITL 会话**（``/cost/session/*``）、``/cost/compose`` 一次性选码取数、``/orchestrate``
复合编排及其点燃的 13 节点图，已随前端组价 widget 一并退役。本层现只保留**无状态确定性端点**：
核对（check，打 ce-db）、算钱（pricing，纯确定性 pydantic 闸门）、门控判断与人工补录规范化（gates）。
"""
from __future__ import annotations

import logging

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from common.config import COST_DEFAULT_REGION
from cost import gates
from cost.pricing import (
    HierarchyRollupInput,
    RollupInput,
    UnitPriceInput,
    compute_unit_price,
    rollup_cost,
    rollup_hierarchy,
)

logger = logging.getLogger("ce-services.cost")

router = APIRouter(tags=["cost-agent"])


class BoqRow(BaseModel):
    """一条 BOQ 清单行（FR-C 核对输入）。除 code 外均可选——给什么核什么。"""

    code: str = Field(..., description="清单编码（9 位全国码或 12 位含顺序码）")
    name: str | None = Field(None, description="清单项名称")
    unit: str | None = Field(None, description="计量单位")
    quantity: float | None = Field(None, description="工程量")
    unit_price: float | None = Field(None, description="综合单价（元）")
    amount: float | None = Field(None, description="合价（元）")


class CostCheckRequest(BaseModel):
    """FR-C 清单核对请求体。

    字段：rows —— BOQ 行列表（1–500）；spec —— 国标版本（缺省默认深圳·2013，§4.0 不反问）；
      region —— 地区（声明用，默认深圳）。BOQ 行随请求进出、服务端不落盘（多租户边界在 deer-flow 层）。
    """

    rows: list[BoqRow] = Field(..., min_length=1, max_length=500, description="BOQ 清单行")
    spec: str | None = Field(None, description="国标版本 2013/2024，缺省默认深圳·2013")
    region: str = Field(COST_DEFAULT_REGION, description="地区，默认深圳")


@router.post("/cost/check")
def cost_check_endpoint(req: CostCheckRequest) -> dict:
    """FR-C 项目上下文核对 v1：BOQ 清单行确定性核对（编码有效性/单位一致性/名称偏离/合价算术）。

    参数：req —— ``CostCheckRequest``。返回：``{spec, region, total, rows_with_issues,
      issues:[{row_index, code, checks:[{type,severity,detail}], bill}], unsupported, meta}``；
      漏项/高估冒算/单价偏差 v1 诚实列入 unsupported（宁缺毋造）。知识服务不可达→503、未知 spec→400。
    """
    from cost import check as boq_check  # 轻模块，打 ce-db 取真值
    try:
        result = boq_check.check_boq([r.model_dump() for r in req.rows],
                                     spec=req.spec, region=req.region)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 503
        detail = exc.response.text if exc.response is not None else str(exc)
        raise HTTPException(status_code=code, detail=f"知识服务返回错误: {detail}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"ce-db 不可达（:8102 /bill/{{code}}）: {exc}") from exc
    logger.info("/cost/check spec=%s rows=%d issues=%d",
                result.get("spec"), result.get("total"), result.get("rows_with_issues"))
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


@router.post("/cost/rollup")
def cost_rollup_endpoint(req: RollupInput) -> dict:
    """总造价汇总原语（§13 ``rollup_cost`` 的 HTTP 表面）——分部分项 + 措施/其他/规费 →（可选税金）总造价。

    参数：req —— ``RollupInput``；FastAPI 据 pydantic schema 自动校验，负金额/NaN/Inf/多余字段 → 422 闸门。
    返回：``rollup_cost`` 结果（税前造价 + 可选税金/总造价 + 溯源 + 红线声明）。纯确定性、不入 LLM；
      税率与项目级费用由调用方给定，本端点不杜撰、不内置默认（不替用户定政策数）。
    """
    return rollup_cost(req)


@router.post("/cost/rollup-hierarchy")
def cost_rollup_hierarchy_endpoint(req: HierarchyRollupInput) -> dict:
    """层级总造价汇总原语 —— 构件行 → 单位工程 → 单项工程 → 项目总造价。

    参数：req —— ``HierarchyRollupInput``；items[].total_price=None 表示该构件未计价，逐层计入
      ``missing_unit_price_items``，不计金额、不虚构。
    返回：``rollup_hierarchy`` 结果（两级明细 + 项目级费用/税金/总价）。纯确定性、不入 LLM。
    """
    return rollup_hierarchy(req)


class GateDecisionRequest(BaseModel):
    """门控判断请求体 —— 暴露 ``gates.should_pause_*`` 的 HTTP 表面。

    gate 取值：
      coding / quota / price / quantity / rates / params / basis_complete / has_priceable_item。
    payload 按 gate 传对应字段：
      coding={env,tau?}; quota={env}; price={price}; quantity={quantity}; rates={rates}; params={params};
      basis_complete={basis}; has_priceable_item={items}。
    """

    gate: str = Field(..., description="门控类型")
    payload: dict = Field(default_factory=dict, description="门控输入数据")


@router.post("/cost/gate-decision")
def cost_gate_decision_endpoint(req: GateDecisionRequest) -> dict:
    """组价 HITL 门控判断原语 —— 当前步骤数据 → 是否需要人工介入。

    返回：``{gate, needs_human_input, reason, payload?}``；coding/quota 需要人工时会附 confirm payload。
    本端点只判断，不推进 session、不落值、不调用 LLM。
    """
    gate, payload = req.gate, req.payload
    try:
        if gate == "coding":
            pause = gates.should_pause_coding(payload["env"], float(payload.get("tau", 0.75)))
            out = {"needs_human_input": pause, "reason": "清单编码低置信/多备选/需复核" if pause else "清单编码可自动通过"}
            if pause:
                out["payload"] = gates.confirm_payload("list_coding", payload["env"], "请确认清单编码")
            return {"gate": gate, **out}
        if gate == "quota":
            pause = gates.should_pause_quota(payload["env"])
            out = {"needs_human_input": pause, "reason": "无定额或多定额子目需确认" if pause else "唯一子目可自动通过"}
            if pause:
                out["payload"] = gates.confirm_payload("quota", payload["env"], "请确认套用定额子目")
            return {"gate": gate, **out}
        if gate == "price":
            pause = gates.should_pause_price(payload["price"])
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "信息价缺失或命中无值" if pause else "信息价可自动通过"}
        if gate == "quantity":
            pause = gates.should_pause_quantity(payload.get("quantity"))
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "工程量 Q 缺失，需人工录入" if pause else "工程量已给定"}
        if gate == "rates":
            pause = gates.should_pause_rates(payload.get("rates"))
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "管理费率/利润率/取费基数缺失" if pause else "综合单价费率已齐"}
        if gate == "params":
            pause = gates.should_pause_params(payload.get("params"))
            return {"gate": gate, "needs_human_input": pause,
                    "reason": "税金率缺失，需人工录入" if pause else "项目级费用参数已齐"}
        if gate == "basis_complete":
            ok = gates.basis_complete(payload.get("basis"))
            return {"gate": gate, "needs_human_input": not ok,
                    "reason": "人材机基价三项未齐" if not ok else "人材机基价完整"}
        if gate == "has_priceable_item":
            ok = gates.has_priceable_item(payload.get("items") or [])
            return {"gate": gate, "needs_human_input": not ok,
                    "reason": "全单无可算价构件" if not ok else "至少一项构件可算价"}
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"门控 {gate!r} 缺少必填 payload 字段: {exc}") from exc
    raise HTTPException(status_code=422, detail=f"未知门控类型 gate={gate!r}")


class ManualQuotaBasisRequest(BaseModel):
    """人工补录定额基价规范化请求体。"""

    quota_code: str | None = Field(None, description="定额子目号（可选）")
    labor_cost: float | None = Field(None, ge=0, description="定额人工费基价")
    material_cost: float | None = Field(None, ge=0, description="定额材料费基价")
    machine_cost: float | None = Field(None, ge=0, description="定额机械费基价")


@router.post("/cost/manual-quota-basis")
def cost_manual_quota_basis_endpoint(req: ManualQuotaBasisRequest) -> dict:
    """人工补录定额基价规范化 —— 三项全齐才生成 quota_basis，半填要求继续补齐。

    返回：``{basis, needs_human_input, reason}``。三项全空表示放弃补录；半填不静默丢弃。
    """
    data = req.model_dump()
    if gates.has_partial_costs(data):
        return {"basis": None, "needs_human_input": True,
                "reason": "人材机三项需一起填齐，或三项全空表示放弃补录"}
    basis = gates.build_manual_basis(data)
    if basis is None:
        return {"basis": None, "needs_human_input": False, "reason": "用户未补录定额基价，跳过该构件"}
    return {"basis": basis, "needs_human_input": False, "reason": "人工补录定额基价完整"}
