"""组价对外原语工具层。

这一层只放可稳定复用的 tool wrapper：
- 检索/取数封装
- 确定性算价
- HITL 门控判断与人工补录规范化

MCP façade 和 HTTP router 共享这层，避免在暴露层重复拼同一套胶水逻辑。
"""
from __future__ import annotations

from typing import Any

from cost import gates, provenance, quota_selection
from cost.pricing import (
    HierarchyItem,
    HierarchyRollupInput,
    RollupInput,
    UnitPriceInput,
    compute_unit_price,
    rollup_cost,
    rollup_hierarchy,
)


def cost_match_bill_item_tool(description: str, spec: str, top_k: int = 10) -> dict[str, Any]:
    """清单识别 tool：构件描述 → 清单候选召回 + 候选内选码信封。"""
    return provenance.list_match(description, spec, top_k)


def cost_price_compose_envelope_tool(region: str, code: str, spec: str) -> dict[str, Any]:
    """组价取数 tool：已确认清单码 → 定额子目信封 + 信息价材料块。"""
    return provenance.from_price_compose(region, code, spec)


def cost_compute_unit_price_tool(req: UnitPriceInput | dict[str, Any]) -> dict[str, Any]:
    """确定性综合单价计算 tool。"""
    inp = req if isinstance(req, UnitPriceInput) else UnitPriceInput.model_validate(req)
    return compute_unit_price(inp)


def cost_rollup_tool(req: RollupInput | dict[str, Any]) -> dict[str, Any]:
    """确定性项目总造价汇总 tool。"""
    inp = req if isinstance(req, RollupInput) else RollupInput.model_validate(req)
    return rollup_cost(inp)


def cost_rollup_hierarchy_tool(req: HierarchyRollupInput | dict[str, Any]) -> dict[str, Any]:
    """确定性层级汇总 tool。"""
    inp = req if isinstance(req, HierarchyRollupInput) else HierarchyRollupInput.model_validate(req)
    return rollup_hierarchy(inp)


def cost_gate_decision_tool(gate: str, payload: dict[str, Any]) -> dict[str, Any]:
    """HITL 门控判断 tool：输入步骤数据 → 是否需要人工介入。"""
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
    raise ValueError(f"未知门控类型 gate={gate!r}")


def cost_build_manual_quota_basis_tool(
    *,
    labor_cost: float | None = None,
    material_cost: float | None = None,
    machine_cost: float | None = None,
    quota_code: str | None = None,
) -> dict[str, Any]:
    """人工补录定额基价规范化 tool。"""
    data = {
        "quota_code": quota_code,
        "labor_cost": labor_cost,
        "material_cost": material_cost,
        "machine_cost": machine_cost,
    }
    if gates.has_partial_costs(data):
        return {"basis": None, "needs_human_input": True,
                "reason": "人材机三项需一起填齐，或三项全空表示放弃补录"}
    basis = gates.build_manual_basis(data)
    if basis is None:
        return {"basis": None, "needs_human_input": False, "reason": "用户未补录定额基价，跳过该构件"}
    return {"basis": basis, "needs_human_input": False, "reason": "人工补录定额基价完整"}


def cost_select_quota_tool(
    quotas: list[dict[str, Any]],
    feature: str | None = None,
    code: str | None = None,
    tau: float = 0.75,
) -> dict[str, Any]:
    """套定额选择 tool：多定额候选内选择子目。"""
    return quota_selection.select_quota(feature, code, quotas, tau=tau)

