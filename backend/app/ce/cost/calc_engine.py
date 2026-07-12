"""智能计算引擎——能力 5 的单源底座（纯函数、零 I/O、LLM 永不算钱，2026-07-12）。

架构同能力 2/3/4（共享引擎、各套壳），四引擎至此集齐：
- **lead 面**：``tools.cost_calc`` 工具（薄壳）——单点算综合单价/合价/汇总/校验；
- **workflow 面**：``stages.run_settle`` 经 ``nodes`` 的 compute 节点装配（结算阶段）。

设计不变量：
- **全部确定性算术**：输入必须是显式数值（components/费率/工程量），不从自然语言猜数；
- **breakdown 可审计**：每步输出算式+金额，供前端展示与复核端独立重算对账；
- **诚实缺口**：费率未提供按 0 计但**显式标注 rates_missing**（综合单价=人材机费不是静默近似）；
  目标/操作无内置公式 → capability_gap 闸交人（描述规则后由模型试算并标注「需人工复核」）。

命名注记：``unit_price``（操作名，Σ数量×单价 的通用合计）与结果字段 ``unit_price``（综合单价）
是历史撞名——操作层面保留兼容，新用法建议用 ``unit_rate``（综合单价）/``rollup``（合计）表达。
"""
from __future__ import annotations

from typing import Any

SUPPORTED_OPERATIONS = ["unit_price", "unit_rate", "line_total", "rollup", "check"]
# 单条清单确定性计算链（有序，越后层级越高）；target-driven 求解算到目标层即停
CALC_CHAIN = ["unit_rate", "line_total"]


def _capability_gap(node: str, question: str, detail: dict[str, Any]) -> dict[str, Any]:
    """capability_gap 型 HITL 中断载荷（与 nodes._capability_gate 同契约，引擎内自构免循环依赖）。"""
    return {
        "gate_type": "capability_gap",
        "node": node,
        "question": question,
        "allow_manual": True,
        "allow_reason": True,
        "required_fields": ["manual_result"],
        "detail": detail,
    }


def calc_unit_price(payload: dict[str, Any]) -> dict[str, Any]:
    """通用合计：Σ(数量×单价)。注意与综合单价（unit_rate）不同——本操作不分类、不叠费率。"""
    components = payload.get("components")
    if not isinstance(components, list):
        return {
            "node": "unit_price",
            "status": "awaiting_input",
            "message": "unit_price 节点只对显式 components 做确定性计算；不会从自然语言或不明字段里猜价格。",
            "required_fields": ["components"],
        }

    total = 0.0
    rows: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        quantity = component.get("quantity", 1)
        unit_price = component.get("unit_price", component.get("price"))
        if not isinstance(quantity, (int, float)) or not isinstance(unit_price, (int, float)):
            rows.append({"component": component, "status": "skipped", "reason": "missing numeric quantity/unit_price"})
            continue
        amount = float(quantity) * float(unit_price)
        total += amount
        rows.append({"component": component, "status": "computed", "amount": amount})
    return {"node": "unit_price", "status": "done", "amount": total, "rows": rows}


def calc_unit_rate(payload: dict[str, Any]) -> dict[str, Any]:
    """综合单价确定性计算（GB 50500 口径：人材机费 + 企业管理费 + 利润 + 风险）。

    按类汇总工料机（含量×单价）得人材机费，再按传入费率叠加。LLM 不参与计算：components 与
    费率均来自工具取数/传入，本函数只做算术并输出逐步 breakdown（可审计）。
    费率全部未提供时按 0 计——综合单价=人材机费，结果**显式标注 rates_missing**（费率应来自
    fee_rate_lookup 或用户提供，静默 0 会误导）。
    """
    components = payload.get("components")
    if not isinstance(components, list):
        return {
            "node": "unit_rate",
            "status": "awaiting_input",
            "message": "unit_rate 只对显式 components 做确定性计算；不会从自然语言或不明字段里猜数。",
            "required_fields": ["components"],
        }

    totals = {"人工": 0.0, "材料": 0.0, "机械": 0.0, "其他": 0.0}
    component_rows: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        category = component.get("category")
        quantity = component.get("consumption", component.get("quantity"))
        unit_price = component.get("unit_price", component.get("price"))
        if not isinstance(quantity, (int, float)) or not isinstance(unit_price, (int, float)):
            component_rows.append({"name": component.get("name"), "category": category, "status": "skipped", "reason": "缺 consumption/unit_price 数值"})
            continue
        amount = round(float(quantity) * float(unit_price), 2)
        totals[category if category in totals else "其他"] += amount
        component_rows.append({"name": component.get("name"), "category": category, "formula": f"{quantity}×{unit_price}", "amount": amount})

    labor = round(totals["人工"], 2)
    material = round(totals["材料"], 2)
    machine = round(totals["机械"], 2)
    other = round(totals["其他"], 2)
    rmm = round(labor + material + machine + other, 2)  # 人材机费（费率基数）

    rates_given = any(isinstance(payload.get(k), (int, float)) for k in ("management_rate", "profit_rate", "risk_rate"))
    management_rate = float(payload.get("management_rate") or 0)
    profit_rate = float(payload.get("profit_rate") or 0)
    risk_rate = float(payload.get("risk_rate") or 0)
    management_fee = round(rmm * management_rate, 2)
    profit = round(rmm * profit_rate, 2)
    risk_fee = round(rmm * risk_rate, 2)
    unit_price = round(rmm + management_fee + profit + risk_fee, 2)

    # 逐步计算过程（供前端 ChainOfThoughtStep / present_files 展示，可审计）
    breakdown = [
        {"item": "人工费", "formula": "Σ人工(含量×单价)", "amount": labor},
        {"item": "材料费", "formula": "Σ材料(含量×单价)", "amount": material},
        {"item": "机械费", "formula": "Σ机械(含量×单价)", "amount": machine},
    ]
    if other:
        breakdown.append({"item": "其他费", "formula": "Σ其他(含量×单价)", "amount": other})
    breakdown.append({"item": "人材机费", "formula": "人工费+材料费+机械费" + ("+其他费" if other else ""), "amount": rmm})
    breakdown.append({"item": "企业管理费", "formula": f"人材机费×{management_rate:.2%}", "rate": management_rate, "amount": management_fee})
    breakdown.append({"item": "利润", "formula": f"人材机费×{profit_rate:.2%}", "rate": profit_rate, "amount": profit})
    if risk_rate:
        breakdown.append({"item": "风险费", "formula": f"人材机费×{risk_rate:.2%}", "rate": risk_rate, "amount": risk_fee})
    breakdown.append({"item": "综合单价", "formula": "人材机费+管理费+利润" + ("+风险费" if risk_rate else ""), "amount": unit_price})

    result = {
        "node": "unit_rate",
        "status": "done",
        "unit_price": unit_price,
        "labor_cost": labor,
        "material_cost": material,
        "machine_cost": machine,
        "rmm_cost": rmm,
        "management_fee": management_fee,
        "profit": profit,
        "risk_fee": risk_fee,
        "component_rows": component_rows,
        "breakdown": breakdown,
        "rate_provenance": {"basis": "rmm", "note": "管理费率/利润率/风险率应来自 fee_rate 库，非 LLM 编造"},
    }
    if not rates_given:
        result["rates_missing"] = True
        result["rate_provenance"]["warning"] = "费率未提供，全部按 0 计——本结果=人材机费，非完整综合单价；费率可用 fee_rate_lookup 查询或由用户提供"
    return result


def calc_line_total(payload: dict[str, Any]) -> dict[str, Any]:
    """清单合价确定性计算：综合单价 × 工程量（纯算术 + breakdown）。"""
    unit_price = payload.get("unit_price")
    quantity = payload.get("quantity")
    if not isinstance(unit_price, (int, float)) or not isinstance(quantity, (int, float)):
        return {
            "node": "line_total",
            "status": "awaiting_input",
            "message": "line_total 需要综合单价与工程量两个显式数值。",
            "required_fields": ["unit_price", "quantity"],
        }
    amount = round(float(unit_price) * float(quantity), 2)
    return {
        "node": "line_total",
        "status": "done",
        "amount": amount,
        "unit_price": float(unit_price),
        "quantity": float(quantity),
        "breakdown": [{"item": "清单合价", "formula": f"综合单价 {unit_price} × 工程量 {quantity}", "amount": amount}],
    }


def calc_rollup(payload: dict[str, Any]) -> dict[str, Any]:
    """多行合计：Σ items[].amount（缺数值的行如实 skipped，不猜）。"""
    items = payload.get("items")
    if not isinstance(items, list):
        return {"node": "rollup", "status": "awaiting_input", "required_fields": ["items"]}
    total = 0.0
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("amount", item.get("total"))
        if not isinstance(value, (int, float)):
            rows.append({"item": item, "status": "skipped", "reason": "missing numeric amount"})
            continue
        total += float(value)
        rows.append({"item": item, "status": "included", "amount": float(value)})
    return {"node": "rollup", "status": "done", "amount": total, "rows": rows}


def calc_check(payload: dict[str, Any]) -> dict[str, Any]:
    """清单行结构完整性校验（编码格式/名称/单位），verdict=pass|reject。"""
    items = payload.get("items") or payload.get("boq") or []
    if not isinstance(items, list):
        return {"node": "check", "status": "awaiting_input", "required_fields": ["items"]}

    issues: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append({"index": index, "severity": "error", "message": "item must be an object"})
            continue
        code = item.get("code")
        if not isinstance(code, str) or len(code.strip()) not in {9, 12}:
            issues.append({"index": index, "severity": "error", "message": "missing or invalid bill code"})
        if not item.get("name"):
            issues.append({"index": index, "severity": "warn", "message": "missing item name"})
        if not item.get("unit"):
            issues.append({"index": index, "severity": "warn", "message": "missing unit"})
    return {
        "node": "check",
        "status": "done",
        "verdict": "reject" if any(issue["severity"] == "error" for issue in issues) else "pass",
        "issues": issues,
    }


def compute_cost(payload: dict[str, Any]) -> dict[str, Any]:
    """target-driven 造价计算：按依赖链算到 target，产沿途合并 breakdown。

    从底层沿确定性计算链（unit_rate → line_total）算到 target 即停；前置数据已给（如直接给了
    unit_price）则跳过对应步。target 无内置公式 → capability_gap 闸交人描述规则。
    """
    target = payload.get("target") or "line_total"
    if target not in CALC_CHAIN:
        return {
            "node": "compute",
            "status": "awaiting_input",
            "target": target,
            "interrupt": _capability_gap(
                "compute",
                f"目标「{target}」无内置确定性计算公式（当前确定性链：{' → '.join(CALC_CHAIN)}）；"
                "请描述该计算规则（基数、系数、来源），将交模型按规则试算并标注「需人工复核、非定稿」。",
                {"requested_target": target, "supported_targets": CALC_CHAIN},
            ),
        }

    state = dict(payload)
    breakdown: list[dict[str, Any]] = []
    steps: list[str] = []
    target_idx = CALC_CHAIN.index(target)

    # 层 0：综合单价（未直接给 unit_price 才算）
    if target_idx >= CALC_CHAIN.index("unit_rate") and not isinstance(state.get("unit_price"), (int, float)):
        unit_rate = calc_unit_rate(state)
        if unit_rate.get("status") != "done":
            return unit_rate
        state["unit_price"] = unit_rate["unit_price"]
        breakdown += unit_rate.get("breakdown", [])
        steps.append("unit_rate")
        rates_missing = unit_rate.get("rates_missing", False)
    else:
        rates_missing = False
    if target == "unit_rate":
        result = {"node": "compute", "target": target, "status": "done", "unit_price": state.get("unit_price"), "breakdown": breakdown, "steps": steps}
        if rates_missing:
            result["rates_missing"] = True
        return result

    # 层 1：清单合价
    line_total = calc_line_total(state)
    if line_total.get("status") != "done":
        return line_total
    breakdown += line_total.get("breakdown", [])
    steps.append("line_total")
    result = {
        "node": "compute",
        "target": "line_total",
        "status": "done",
        "unit_price": line_total["unit_price"],
        "quantity": line_total["quantity"],
        "total_price": line_total["amount"],
        "breakdown": breakdown,
        "steps": steps,
    }
    if rates_missing:
        result["rates_missing"] = True
    return result


def calc_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    """calc 统一入口：带 target 走链式 compute；带 operation 走单步；无公式 → capability_gap 闸。"""
    if payload.get("target"):
        return compute_cost(payload)

    operation = payload.get("operation") or payload.get("subnode") or payload.get("node")
    if not isinstance(operation, str) or not operation.strip():
        return {
            "node": "calc",
            "status": "awaiting_input",
            "required_fields": ["operation"],
            "supported_operations": SUPPORTED_OPERATIONS,
        }

    normalized = operation.strip()
    handler = _OPERATIONS.get(normalized)
    if handler is None:
        # 落在已实现确定性公式之外（如"整体项目成本"）→ 停下交人工确认口径/规则，不静默不支持。
        return {
            "node": "calc",
            "status": "awaiting_input",
            "operation": normalized,
            "interrupt": _capability_gap(
                "calc",
                f"当前 calc 仅内置 {' / '.join(SUPPORTED_OPERATIONS)} 五类确定性公式，无「{normalized}」对应公式；"
                "这类超出计算范围的场景需人工确认口径或提供计算规则。",
                {"requested_operation": normalized, "supported_operations": SUPPORTED_OPERATIONS},
            ),
            "supported_operations": SUPPORTED_OPERATIONS,
        }
    return handler(payload)


_OPERATIONS: dict[str, Any] = {
    "unit_price": calc_unit_price,
    "unit_rate": calc_unit_rate,
    "line_total": calc_line_total,
    "rollup": calc_rollup,
    "check": calc_check,
}

__all__ = [
    "CALC_CHAIN", "SUPPORTED_OPERATIONS", "calc_check", "calc_dispatch", "calc_line_total",
    "calc_rollup", "calc_unit_price", "calc_unit_rate", "compute_cost",
]
