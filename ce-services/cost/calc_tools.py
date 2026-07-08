"""建设工程造价——确定性计算工具集（单文件、纯函数、pydantic 入参 schema）。

设计原则（按造价领域知识组织，不参照其它实现）：

1. **只收确定性算术**：凡「给定输入即唯一确定输出」的计算，全部做成独立 ``*_tool``；
   语义判断（选码 / 套定额 / 取费基数选哪个 / 费率取几）不在此文件——那是检索/人工决策，
   其结果作为**入参**喂进来。本文件不做任何猜测、不内置政策默认值（费率/税率一律由调用方给定）。
2. **纯函数 + pydantic 闸门**：每个 tool 收 ``*Input | dict`` 单对象入参，经 pydantic 强校验
   （``extra=forbid`` 拒多余字段、``ge/gt`` 非负、``allow_inf_nan=False`` 拒 NaN/Inf）后再算；
   无 I/O、无全局状态、无模型调用，同输入永远同输出，可单测、可复现、可审计。
3. **定点金额**：一切金额用 ``Decimal`` 计算、``ROUND_HALF_UP`` 量化到分（0.01 元）；
   每行先量化再求和，避免浮点累加误差。几何量按各自惯例保留精度。
4. **自带痕迹**：每个 tool 返回 ``{主结果, breakdown/明细, formula 公式, provenance 溯源}``。

暴露层用法：``INPUT_MODELS[name].model_json_schema()`` 出 JSON schema；``TOOLS[name](payload)`` 执行。

链路：工料机含量×单价 → 定额基价 →(换算/价差)→ 综合单价 →×工程量→ 综合合价
     → 分部分项 →+措施+其他+规费+税金→ 单位工程造价 → 单项 → 建设项目。
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Callable, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ══════════════════════════════════════════════════════════════════════════
# 0. 通用底座：定点、枚举、pydantic 基类
# ══════════════════════════════════════════════════════════════════════════

_CENT = Decimal("0.01")
_M = TypeVar("_M", bound=BaseModel)

# 复用的受约束标量类型（非负、有限）
_Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Rate = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Qty = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_PosLen = Annotated[float, Field(gt=0, allow_inf_nan=False)]


def _dec(x: float | int | str | Decimal) -> Decimal:
    """任意数值 → Decimal（经 str 转换避免浮点二进制误差）。"""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _money(x: Decimal) -> float:
    """量化到分（元，ROUND_HALF_UP），返回 float。"""
    return float(x.quantize(_CENT, rounding=ROUND_HALF_UP))


def _q(x: Decimal) -> Decimal:
    """量化到分（Decimal，供内部逐行累加）。"""
    return x.quantize(_CENT, rounding=ROUND_HALF_UP)


def _round(x: Decimal, ndigits: int) -> float:
    """量化到 ndigits 位小数（几何量用），返回 float。"""
    return float(x.quantize(Decimal(1).scaleb(-ndigits), rounding=ROUND_HALF_UP))


def _v(model: type[_M], req: _M | dict[str, Any]) -> _M:
    """入参归一：已是模型直接用，是 dict 则过 pydantic 校验。"""
    return req if isinstance(req, model) else model.model_validate(req)


class FeeBase(str, Enum):
    """取费基数——企业管理费/利润/风险按哪个口径乘费率（显式声明，无默认）。

    ``labor`` 人工费 / ``labor_machine`` 人工+机械 / ``lmm`` 人材机 / ``direct`` 直接费(同 lmm)。
    """

    labor = "labor"
    labor_machine = "labor_machine"
    lmm = "lmm"
    direct = "direct"


class TaxMethod(str, Enum):
    """增值税计税方式：``general`` 一般计税(常 9%) / ``simple`` 简易计税(常 3%)。公式一致，仅标注口径。"""

    general = "general"
    simple = "simple"


class _In(BaseModel):
    """所有入参模型基类：拒多余字段（防止悄悄塞进未声明口径）。"""

    model_config = ConfigDict(extra="forbid")


# 资源类别归一：五花八门的类别标签 → 人工/材料/机械
_CATEGORY_MAP = {
    "人工": "labor", "劳务": "labor", "labor": "labor",
    "材料": "material", "主材": "material", "辅材": "material", "material": "material",
    "机械": "machine", "机具": "machine", "施工机械": "machine", "machine": "machine",
}


def _norm_category(cat: str) -> str:
    key = (cat or "").strip()
    if key in _CATEGORY_MAP:
        return _CATEGORY_MAP[key]
    for k, v in _CATEGORY_MAP.items():
        if k in key:
            return v
    raise ValueError(f"无法识别资源类别 {cat!r}（应为 人工/材料/机械 之一）")


def _fee_base_dec(labor: float, material: float, machine: float, fb: FeeBase) -> Decimal:
    """按取费基数口径求基数金额（Decimal）。"""
    lab, mat, mac = _dec(labor), _dec(material), _dec(machine)
    if fb is FeeBase.labor:
        return lab
    if fb is FeeBase.labor_machine:
        return lab + mac
    return lab + mat + mac  # lmm / direct


def _rate_fee_calc(base_amount: float, rate: float, name: str, clause: str | None) -> dict[str, Any]:
    """费率型费用纯计算：金额 = 基数 × 费率%。"""
    amt = _q(_dec(base_amount) * _dec(rate) / Decimal("100"))
    return {"name": name, "amount": float(amt), "base_amount": base_amount, "rate": rate,
            "formula": f"{name} = 计算基数 × 费率%", "provenance": {"clause": clause}}


def _tax_calc(pre_tax: float, tax_rate: float, method: TaxMethod) -> dict[str, Any]:
    """增值税纯计算。"""
    pre = _q(_dec(pre_tax))
    tax = _q(pre * _dec(tax_rate) / Decimal("100"))
    return {"pre_tax_total": float(pre), "tax": float(tax), "total_with_tax": _money(pre + tax),
            "tax_rate": tax_rate, "method": method.value,
            "formula": "税金 = 税前造价 × 税率%；含税造价 = 税前 + 税金",
            "provenance": {"clause": "GB 50500 税金"}}


# ══════════════════════════════════════════════════════════════════════════
# 1. 资源 → 定额基价
# ══════════════════════════════════════════════════════════════════════════


class ResourceLine(_In):
    category: str = Field(..., description="资源类别：人工/材料/机械（自动归一）")
    name: str | None = Field(None, description="资源名称（可选，展示/审计）")
    quantity: _Qty = Field(..., description="每计量单位的工料机含量")
    unit_price: _Money = Field(..., description="资源单价（元）")


class ComposeBasePriceInput(_In):
    resources: list[ResourceLine] = Field(..., min_length=1, description="工料机资源明细")


def compose_base_price_tool(req: ComposeBasePriceInput | dict[str, Any]) -> dict[str, Any]:
    """定额基价合成：Σ(工料机含量 × 单价)，按 人工/材料/机械 归集。

    返回 ``{labor_cost, material_cost, machine_cost, base_price, lines[], formula, provenance}``，
    base_price = 人材机净基价（= 定额直接工程费，不含管理费/利润）。
    """
    inp = _v(ComposeBasePriceInput, req)
    buckets = {"labor": Decimal("0"), "material": Decimal("0"), "machine": Decimal("0")}
    lines: list[dict[str, Any]] = []
    for r in inp.resources:
        cat = _norm_category(r.category)
        amt = _q(_dec(r.quantity) * _dec(r.unit_price))
        buckets[cat] += amt
        lines.append({"name": r.name, "category": cat, "quantity": r.quantity,
                      "unit_price": r.unit_price, "amount": float(amt)})
    base = buckets["labor"] + buckets["material"] + buckets["machine"]
    return {
        "labor_cost": float(buckets["labor"]), "material_cost": float(buckets["material"]),
        "machine_cost": float(buckets["machine"]), "base_price": float(base), "lines": lines,
        "formula": "人/材/机费 = Σ(该类各资源 含量 × 单价)；基价 = 人 + 材 + 机",
        "provenance": {"basis": "定额工料机含量 + 资源单价", "note": "净基价，不含管理费/利润/风险"},
    }


class ApplyQuotaConversionInput(_In):
    labor_cost: _Money = Field(..., description="定额人工费基价（元）")
    material_cost: _Money = Field(..., description="定额材料费基价（元）")
    machine_cost: _Money = Field(..., description="定额机械费基价（元）")
    labor_factor: _Rate = Field(1.0, description="人工换算系数（默认 1）")
    machine_factor: _Rate = Field(1.0, description="机械换算系数（默认 1）")
    material_factor: _Rate = Field(1.0, description="材料换算系数（默认 1）")
    material_delta: float = Field(0.0, allow_inf_nan=False, description="材料费增减额（元，正增负减）")


def apply_quota_conversion_tool(req: ApplyQuotaConversionInput | dict[str, Any]) -> dict[str, Any]:
    """定额换算：人工×系数、机械×系数、材料×系数+增减额。返回换算后三项 + base_price + 前后对照。"""
    inp = _v(ApplyQuotaConversionInput, req)
    lab = _q(_dec(inp.labor_cost) * _dec(inp.labor_factor))
    mac = _q(_dec(inp.machine_cost) * _dec(inp.machine_factor))
    mat = _q(_dec(inp.material_cost) * _dec(inp.material_factor) + _dec(inp.material_delta))
    if mat < 0:
        raise ValueError("换算后材料费为负，请检查 material_delta")
    base = lab + mat + mac
    return {
        "labor_cost": float(lab), "material_cost": float(mat), "machine_cost": float(mac),
        "base_price": float(base),
        "before": {"labor": inp.labor_cost, "material": inp.material_cost, "machine": inp.machine_cost},
        "factors": {"labor": inp.labor_factor, "machine": inp.machine_factor,
                    "material": inp.material_factor, "material_delta": inp.material_delta},
        "formula": "人工×人工系数；机械×机械系数；材料×材料系数 + 材料增减额",
        "provenance": {"basis": "定额换算规则（调用方给定系数）"},
    }


class MaterialPriceItem(_In):
    name: str | None = Field(None, description="材料名称")
    quantity: _Qty = Field(..., description="材料含量")
    quota_price: _Money = Field(..., description="定额基价（元）")
    market_price: _Money = Field(..., description="市场价/信息价（元）")


class MaterialPriceDifferenceInput(_In):
    items: list[MaterialPriceItem] = Field(..., min_length=1, description="材料价差明细")


def material_price_difference_tool(req: MaterialPriceDifferenceInput | dict[str, Any]) -> dict[str, Any]:
    """材料价差：Σ (市场价 − 定额基价) × 含量（可正可负，用于把定额价调到市场活价口径）。"""
    inp = _v(MaterialPriceDifferenceInput, req)
    total = Decimal("0")
    lines: list[dict[str, Any]] = []
    for it in inp.items:
        diff = _q((_dec(it.market_price) - _dec(it.quota_price)) * _dec(it.quantity))
        total += diff
        lines.append({"name": it.name, "quantity": it.quantity, "quota_price": it.quota_price,
                      "market_price": it.market_price, "diff": float(diff)})
    return {"price_diff_total": float(total), "lines": lines,
            "formula": "价差合计 = Σ (市场价 − 定额基价) × 含量",
            "provenance": {"basis": "信息价/市场价 − 定额基价"}}


# ══════════════════════════════════════════════════════════════════════════
# 2. 综合单价
# ══════════════════════════════════════════════════════════════════════════


class FeeBaseInput(_In):
    labor_cost: _Money = Field(..., description="人工费（元）")
    material_cost: _Money = Field(..., description="材料费（元）")
    machine_cost: _Money = Field(..., description="机械费（元）")
    fee_base: FeeBase = Field(..., description="取费基数口径")


def fee_base_amount_tool(req: FeeBaseInput | dict[str, Any]) -> dict[str, Any]:
    """取费基数金额：按口径取 人工 / 人工+机械 / 人材机。"""
    inp = _v(FeeBaseInput, req)
    base = _fee_base_dec(inp.labor_cost, inp.material_cost, inp.machine_cost, inp.fee_base)
    label = {"labor": "人工费", "labor_machine": "人工费+机械费",
             "lmm": "人工+材料+机械", "direct": "人工+材料+机械"}[inp.fee_base.value]
    return {"fee_base": inp.fee_base.value, "amount": _money(base), "formula": label}


class UnitPriceInput(_In):
    labor_cost: _Money = Field(..., description="人工费（元/计量单位）")
    material_cost: _Money = Field(..., description="材料费（元/计量单位）")
    machine_cost: _Money = Field(..., description="施工机具使用费（元/计量单位）")
    management_fee_rate: _Rate = Field(..., description="企业管理费率 %（调用方给定，无默认）")
    profit_rate: _Rate = Field(..., description="利润率 %（调用方给定，无默认）")
    fee_base: FeeBase = Field(..., description="取费基数口径（无默认）")
    risk_rate: _Rate = Field(0.0, description="风险费率 %（默认 0）")


def comprehensive_unit_price_tool(req: UnitPriceInput | dict[str, Any]) -> dict[str, Any]:
    """综合单价（清单口径，不含增值税）= 人+材+机 + 管理费 + 利润 + 风险；后三项 = 取费基数 × 费率%。"""
    inp = _v(UnitPriceInput, req)
    base = _fee_base_dec(inp.labor_cost, inp.material_cost, inp.machine_cost, inp.fee_base)

    def _rf(rate: float) -> Decimal:
        return _q(base * _dec(rate) / Decimal("100"))

    lab, mat, mac = _q(_dec(inp.labor_cost)), _q(_dec(inp.material_cost)), _q(_dec(inp.machine_cost))
    mgmt, profit, risk = _rf(inp.management_fee_rate), _rf(inp.profit_rate), _rf(inp.risk_rate)
    unit = lab + mat + mac + mgmt + profit + risk
    return {
        "unit_price": float(unit),
        "breakdown": {"人工费": float(lab), "材料费": float(mat), "施工机具使用费": float(mac),
                      "企业管理费": float(mgmt), "利润": float(profit), "风险费用": float(risk),
                      "fee_base": inp.fee_base.value, "fee_base_amount": float(base)},
        "formula": "综合单价 = 人+材+机 + 管理费 + 利润 + 风险；管理费/利润/风险 = 取费基数 × 费率%",
        "provenance": {"clause": "GB 50500 综合单价构成", "note": "不含增值税；费率由调用方给定"},
    }


class LineAmountInput(_In):
    unit_price: _Money = Field(..., description="综合单价（元）")
    quantity: _Qty = Field(..., description="工程量（清单数量）")


def line_amount_tool(req: LineAmountInput | dict[str, Any]) -> dict[str, Any]:
    """综合合价 = 综合单价 × 工程量。"""
    inp = _v(LineAmountInput, req)
    amt = _q(_dec(inp.unit_price) * _dec(inp.quantity))
    return {"amount": float(amt), "unit_price": inp.unit_price, "quantity": inp.quantity,
            "formula": "综合合价 = 综合单价 × 工程量"}


# ══════════════════════════════════════════════════════════════════════════
# 3. 费率型费用 & 求和
# ══════════════════════════════════════════════════════════════════════════


class RateFeeInput(_In):
    base_amount: _Money = Field(..., description="计算基数（元）")
    rate: _Rate = Field(..., description="费率 %")
    name: str = Field("费率费用", description="费用名（语义标注）")
    clause: str | None = Field(None, description="溯源条文（可选）")


def rate_based_fee_tool(req: RateFeeInput | dict[str, Any]) -> dict[str, Any]:
    """通用费率费：金额 = 计算基数 × 费率%。总价措施/规费/总包服务/附加税等一切「基数×费率」通用。"""
    inp = _v(RateFeeInput, req)
    return _rate_fee_calc(inp.base_amount, inp.rate, inp.name, inp.clause)


class BaseRateInput(_In):
    base_amount: _Money = Field(..., description="计算基数（元）")
    rate: _Rate = Field(..., description="费率 %")


def safety_civilized_fee_tool(req: BaseRateInput | dict[str, Any]) -> dict[str, Any]:
    """安全文明施工费（总价措施，不可竞争）= 计算基数 × 费率。"""
    inp = _v(BaseRateInput, req)
    return _rate_fee_calc(inp.base_amount, inp.rate, "安全文明施工费", "总价措施项目费（不可竞争）")


def general_contractor_service_fee_tool(req: BaseRateInput | dict[str, Any]) -> dict[str, Any]:
    """总承包服务费 = 分包/甲供工程金额 × 费率。"""
    inp = _v(BaseRateInput, req)
    return _rate_fee_calc(inp.base_amount, inp.rate, "总承包服务费", "其他项目费")


class DayworkItem(_In):
    name: str | None = Field(None, description="计日工名称")
    quantity: _Qty = Field(..., description="数量")
    unit_price: _Money = Field(..., description="综合单价（元）")


class DayworksInput(_In):
    items: list[DayworkItem] = Field(..., min_length=1, description="计日工明细")


def dayworks_tool(req: DayworksInput | dict[str, Any]) -> dict[str, Any]:
    """计日工合计 = Σ (数量 × 综合单价)。"""
    inp = _v(DayworksInput, req)
    total = Decimal("0")
    lines: list[dict[str, Any]] = []
    for it in inp.items:
        amt = _q(_dec(it.quantity) * _dec(it.unit_price))
        total += amt
        lines.append({"name": it.name, "quantity": it.quantity, "unit_price": it.unit_price,
                      "amount": float(amt)})
    return {"dayworks_total": float(total), "lines": lines,
            "formula": "计日工 = Σ 数量 × 综合单价", "provenance": {"clause": "其他项目费"}}


class SumAmountsInput(_In):
    amounts: list[_Money] = Field(..., description="待求和的金额清单（元，各项非负）")
    name: str = Field("合计", description="合计项名称")


def sum_amounts_tool(req: SumAmountsInput | dict[str, Any]) -> dict[str, Any]:
    """通用求和（分部分项合价、暂列金额汇总、任意金额清单加总）——各项量化后相加。"""
    inp = _v(SumAmountsInput, req)
    total = Decimal("0")
    for a in inp.amounts:
        total += _q(_dec(a))
    return {"name": inp.name, "total": float(total), "item_count": len(inp.amounts),
            "formula": f"{inp.name} = Σ 各项金额"}


# ══════════════════════════════════════════════════════════════════════════
# 4. 税金
# ══════════════════════════════════════════════════════════════════════════


class TaxInput(_In):
    pre_tax_total: _Money = Field(..., description="税前造价（元）")
    tax_rate: _Rate = Field(..., description="增值税率/征收率 %")
    method: TaxMethod = Field(TaxMethod.general, description="计税方式：general/simple")


def compute_tax_tool(req: TaxInput | dict[str, Any]) -> dict[str, Any]:
    """增值税 = 税前造价 × 税率%；含税造价 = 税前 + 税金。税率由调用方给定，不内置默认。"""
    inp = _v(TaxInput, req)
    return _tax_calc(inp.pre_tax_total, inp.tax_rate, inp.method)


# ══════════════════════════════════════════════════════════════════════════
# 5. 单位工程造价 & 多级汇总
# ══════════════════════════════════════════════════════════════════════════


class UnitProjectCostInput(_In):
    division_fee: _Money = Field(..., description="分部分项合价（Σ各清单综合合价，元）")
    measure_fee: _Money = Field(0.0, description="措施项目费（元）")
    other_fee: _Money = Field(0.0, description="其他项目费（元）")
    fee_levy: _Money = Field(0.0, description="规费（元）")
    tax_rate: float | None = Field(None, ge=0, allow_inf_nan=False, description="税率 %，给定则算税金+含税")
    tax_method: TaxMethod = Field(TaxMethod.general, description="计税方式")


def unit_project_cost_tool(req: UnitProjectCostInput | dict[str, Any]) -> dict[str, Any]:
    """单位工程造价 = 分部分项 + 措施 + 其他 + 规费 (+税金)。tax_rate 缺省则只出税前造价。"""
    inp = _v(UnitProjectCostInput, req)
    parts = {"分部分项费": _q(_dec(inp.division_fee)), "措施项目费": _q(_dec(inp.measure_fee)),
             "其他项目费": _q(_dec(inp.other_fee)), "规费": _q(_dec(inp.fee_levy))}
    pre = sum(parts.values(), Decimal("0"))
    out: dict[str, Any] = {
        "breakdown": {k: float(v) for k, v in parts.items()},
        "pre_tax_total": float(pre), "tax": None, "total": None,
        "formula": "税前造价 = 分部分项 + 措施 + 其他 + 规费；总造价 = 税前 + 税金",
        "provenance": {"clause": "GB 50500 工程造价构成"},
    }
    if inp.tax_rate is not None:
        t = _tax_calc(float(pre), inp.tax_rate, inp.tax_method)
        out["tax"], out["total"], out["tax_rate"] = t["tax"], t["total_with_tax"], inp.tax_rate
    return out


class RollupTreeInput(_In):
    items: list[dict[str, Any]] = Field(
        ..., min_length=1,
        description="末级明细，每项含各层级键 + amount（元，None=未计价，计入 missing 不计金额）")
    levels: list[str] = Field(
        ..., min_length=1, description="分组层级键名（由粗到细，如 [single_work, unit_work]）")

    @model_validator(mode="after")
    def _check_keys(self) -> "RollupTreeInput":
        for it in self.items:
            for lvl in self.levels:
                if lvl not in it:
                    raise ValueError(f"item 缺少层级键 {lvl!r}")
            amt = it.get("amount")
            if amt is not None and (not isinstance(amt, (int, float)) or amt < 0
                                    or not math.isfinite(float(amt))):
                raise ValueError(f"amount 须为非负有限数或 None，收到 {amt!r}")
        return self


def rollup_tree_tool(req: RollupTreeInput | dict[str, Any]) -> dict[str, Any]:
    """多级层次汇总：按 levels 逐层聚合 amount（如 单项工程 > 单位工程 > 分部分项）。

    返回嵌套树 ``{name, subtotal, item_count, missing, children[]}`` + 顶层 subtotal/missing_items。
    未计价项（amount=None）计入 missing、不虚构金额；分组按出现顺序（可复现）。
    """
    inp = _v(RollupTreeInput, req)
    levels = inp.levels

    def _new(name: str) -> dict[str, Any]:
        return {"name": name, "_sub": Decimal("0"), "item_count": 0, "missing": 0, "_children": {}}

    root = _new("__root__")
    for it in inp.items:
        chain = [root]
        node = root
        for lvl in levels:
            key = str(it[lvl])
            node["_children"].setdefault(key, _new(key))
            node = node["_children"][key]
            chain.append(node)
        amt = it.get("amount")
        leaf = chain[-1]
        leaf["item_count"] += 1
        if amt is None:
            for nd in chain:
                nd["missing"] += 1
        else:
            q = _q(_dec(amt))
            for nd in chain:
                nd["_sub"] += q

    def _emit(nd: dict[str, Any]) -> dict[str, Any]:
        out = {"name": nd["name"], "subtotal": float(nd["_sub"]),
               "item_count": nd["item_count"], "missing": nd["missing"]}
        if nd["_children"]:
            out["children"] = [_emit(c) for c in nd["_children"].values()]
        return out

    return {"subtotal": float(root["_sub"]), "missing_items": root["missing"], "levels": levels,
            "tree": [_emit(c) for c in root["_children"].values()],
            "formula": "逐层 Σ 末级金额；未计价项计入 missing、不计金额"}


# ══════════════════════════════════════════════════════════════════════════
# 6. 常见工程量算量（确定性几何公式，节选高频构件）
# ══════════════════════════════════════════════════════════════════════════


class RebarWeightInput(_In):
    diameter_mm: _PosLen = Field(..., description="钢筋直径（mm）")
    length_m: _Qty = Field(..., description="单根长度（m）")
    count: int = Field(1, gt=0, description="根数")


def rebar_theoretical_weight_tool(req: RebarWeightInput | dict[str, Any]) -> dict[str, Any]:
    """钢筋理论重量 = 0.00617 × d²(mm) × 长度(m) × 根数（kg）；系数 = 7850×π/4×10⁻⁶。"""
    inp = _v(RebarWeightInput, req)
    per_m = _dec("0.00617") * _dec(inp.diameter_mm) * _dec(inp.diameter_mm)
    total = per_m * _dec(inp.length_m) * _dec(inp.count)
    return {"weight_kg": _round(total, 3), "weight_t": _round(total / Decimal("1000"), 5),
            "unit_weight_kg_per_m": _round(per_m, 4),
            "formula": "W = 0.00617 × d² × 长度 × 根数（kg）"}


class ConcreteRectInput(_In):
    width_m: _PosLen = Field(..., description="截面宽（m）")
    height_m: _PosLen = Field(..., description="截面高（m）")
    length_m: _Qty = Field(..., description="构件长/柱高（m，按净长）")


def concrete_rect_volume_tool(req: ConcreteRectInput | dict[str, Any]) -> dict[str, Any]:
    """矩形截面混凝土体积 = 宽 × 高 × 长（m³）。适用矩形柱/梁/墙。"""
    inp = _v(ConcreteRectInput, req)
    v = _dec(inp.width_m) * _dec(inp.height_m) * _dec(inp.length_m)
    return {"volume_m3": _round(v, 4), "formula": "V = 宽 × 高 × 长"}


class ConcreteCircularInput(_In):
    diameter_m: _PosLen = Field(..., description="直径（m）")
    height_m: _Qty = Field(..., description="高/长（m）")


def concrete_circular_volume_tool(req: ConcreteCircularInput | dict[str, Any]) -> dict[str, Any]:
    """圆形截面混凝土体积 = π/4 × D² × 高（m³）。适用圆柱/桩。"""
    inp = _v(ConcreteCircularInput, req)
    v = _dec(math.pi) / Decimal("4") * _dec(inp.diameter_m) * _dec(inp.diameter_m) * _dec(inp.height_m)
    return {"volume_m3": _round(v, 4), "formula": "V = π/4 × D² × 高"}


class FormworkRectColumnInput(_In):
    width_m: _PosLen = Field(..., description="柱宽（m）")
    depth_m: _PosLen = Field(..., description="柱进深（m）")
    height_m: _Qty = Field(..., description="柱高（m）")


def formwork_rect_column_area_tool(req: FormworkRectColumnInput | dict[str, Any]) -> dict[str, Any]:
    """矩形柱模板面积 = 周长 × 柱高 = 2×(宽+进深) × 高（m²，不扣节点）。"""
    inp = _v(FormworkRectColumnInput, req)
    perimeter = Decimal("2") * (_dec(inp.width_m) + _dec(inp.depth_m))
    area = perimeter * _dec(inp.height_m)
    return {"area_m2": _round(area, 4), "perimeter_m": _round(perimeter, 4),
            "formula": "模板面积 = 2×(宽+进深) × 柱高"}


class MasonryInput(_In):
    length_m: _Qty = Field(..., description="墙长（m）")
    height_m: _Qty = Field(..., description="墙高（m）")
    thickness_m: _PosLen = Field(..., description="墙厚（m）")
    deductions_m3: _Qty = Field(0.0, description="扣除量（门窗洞口/构造柱等，m³）")


def masonry_volume_tool(req: MasonryInput | dict[str, Any]) -> dict[str, Any]:
    """砌体体积 = 长 × 高 × 厚 − 扣除（m³）。"""
    inp = _v(MasonryInput, req)
    gross = _dec(inp.length_m) * _dec(inp.height_m) * _dec(inp.thickness_m)
    net = gross - _dec(inp.deductions_m3)
    if net < 0:
        raise ValueError("扣除量大于毛体积，请检查输入")
    return {"volume_m3": _round(net, 4), "gross_m3": _round(gross, 4),
            "deductions_m3": inp.deductions_m3, "formula": "V = 长 × 高 × 厚 − 扣除"}


class EarthworkTrenchInput(_In):
    bottom_width_m: _Qty = Field(..., description="沟槽底宽（m）")
    depth_m: _Qty = Field(..., description="挖深 H（m）")
    length_m: _Qty = Field(..., description="沟槽长（m）")
    slope_k: _Rate = Field(0.0, description="放坡系数 K（水平:垂直）")
    both_sides: bool = Field(True, description="是否两侧放坡")


def earthwork_sloped_trench_tool(req: EarthworkTrenchInput | dict[str, Any]) -> dict[str, Any]:
    """放坡基槽/管沟土方量（梯形断面 × 长，m³）：两侧 (底宽+K×H)×H；单侧 (底宽+K×H/2)×H。"""
    inp = _v(EarthworkTrenchInput, req)
    b, h, k = _dec(inp.bottom_width_m), _dec(inp.depth_m), _dec(inp.slope_k)
    area = (b + k * h) * h if inp.both_sides else (b + k * h / Decimal("2")) * h
    vol = area * _dec(inp.length_m)
    return {"volume_m3": _round(vol, 4), "section_area_m2": _round(area, 4),
            "both_sides": inp.both_sides,
            "formula": "两侧 V=(底宽+K×H)×H×长；单侧 V=(底宽+K×H/2)×H×长"}


# ══════════════════════════════════════════════════════════════════════════
# 7. 造价指标 & 价格调值
# ══════════════════════════════════════════════════════════════════════════


class UnitAreaCostInput(_In):
    total_cost: _Money = Field(..., description="工程造价（元）")
    floor_area_m2: _PosLen = Field(..., description="建筑面积（m²）")


def unit_area_cost_tool(req: UnitAreaCostInput | dict[str, Any]) -> dict[str, Any]:
    """单方造价指标 = 工程造价 ÷ 建筑面积（元/m²）。"""
    inp = _v(UnitAreaCostInput, req)
    v = _dec(inp.total_cost) / _dec(inp.floor_area_m2)
    return {"cost_per_m2": _money(v), "total_cost": inp.total_cost, "floor_area_m2": inp.floor_area_m2,
            "formula": "单方造价 = 造价 ÷ 建筑面积"}


class AdjustmentFactor(_In):
    name: str | None = Field(None, description="可调因子名（人工/钢材/水泥等）")
    weight: _Rate = Field(..., description="变值权重 ai")
    base_index: _PosLen = Field(..., description="基期价格指数")
    current_index: _Qty = Field(..., description="当期价格指数")


class PriceIndexAdjustmentInput(_In):
    base_price: _Money = Field(..., description="基准价 P0（元）")
    fixed_weight: _Rate = Field(..., description="定值权重 a0（不调部分）")
    factors: list[AdjustmentFactor] = Field(..., min_length=1, description="各可调因子")


def price_index_adjustment_tool(req: PriceIndexAdjustmentInput | dict[str, Any]) -> dict[str, Any]:
    """价格调值公式（调值公式法）：P = P0 × [a0 + Σ ai × (当期指数/基期指数)]。

    约束：a0 + Σai 应 ≈ 1，回报 weight_sum 供核对。
    """
    inp = _v(PriceIndexAdjustmentInput, req)
    terms = _dec(inp.fixed_weight)
    weight_sum = _dec(inp.fixed_weight)
    detail: list[dict[str, Any]] = []
    for f in inp.factors:
        ratio = _dec(f.current_index) / _dec(f.base_index)
        terms += _dec(f.weight) * ratio
        weight_sum += _dec(f.weight)
        detail.append({"name": f.name, "weight": f.weight, "base_index": f.base_index,
                       "current_index": f.current_index, "ratio": _round(ratio, 4)})
    adjusted = _dec(inp.base_price) * terms
    return {"adjusted_price": _money(adjusted), "base_price": inp.base_price,
            "adjustment_factor": _round(terms, 6), "weight_sum": _round(weight_sum, 4),
            "factors": detail, "formula": "P = P0 × [a0 + Σ ai × (当期指数/基期指数)]",
            "note": "weight_sum 应≈1，偏离请核对权重设置"}


# ══════════════════════════════════════════════════════════════════════════
# 8. 注册表：tool 名 → 函数 / 入参模型（供 MCP/function-calling 暴露层）
# ══════════════════════════════════════════════════════════════════════════

TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "compose_base_price": compose_base_price_tool,
    "apply_quota_conversion": apply_quota_conversion_tool,
    "material_price_difference": material_price_difference_tool,
    "fee_base_amount": fee_base_amount_tool,
    "comprehensive_unit_price": comprehensive_unit_price_tool,
    "line_amount": line_amount_tool,
    "rate_based_fee": rate_based_fee_tool,
    "safety_civilized_fee": safety_civilized_fee_tool,
    "general_contractor_service_fee": general_contractor_service_fee_tool,
    "dayworks": dayworks_tool,
    "sum_amounts": sum_amounts_tool,
    "compute_tax": compute_tax_tool,
    "unit_project_cost": unit_project_cost_tool,
    "rollup_tree": rollup_tree_tool,
    "rebar_theoretical_weight": rebar_theoretical_weight_tool,
    "concrete_rect_volume": concrete_rect_volume_tool,
    "concrete_circular_volume": concrete_circular_volume_tool,
    "formwork_rect_column_area": formwork_rect_column_area_tool,
    "masonry_volume": masonry_volume_tool,
    "earthwork_sloped_trench": earthwork_sloped_trench_tool,
    "unit_area_cost": unit_area_cost_tool,
    "price_index_adjustment": price_index_adjustment_tool,
}

INPUT_MODELS: dict[str, type[BaseModel]] = {
    "compose_base_price": ComposeBasePriceInput,
    "apply_quota_conversion": ApplyQuotaConversionInput,
    "material_price_difference": MaterialPriceDifferenceInput,
    "fee_base_amount": FeeBaseInput,
    "comprehensive_unit_price": UnitPriceInput,
    "line_amount": LineAmountInput,
    "rate_based_fee": RateFeeInput,
    "safety_civilized_fee": BaseRateInput,
    "general_contractor_service_fee": BaseRateInput,
    "dayworks": DayworksInput,
    "sum_amounts": SumAmountsInput,
    "compute_tax": TaxInput,
    "unit_project_cost": UnitProjectCostInput,
    "rollup_tree": RollupTreeInput,
    "rebar_theoretical_weight": RebarWeightInput,
    "concrete_rect_volume": ConcreteRectInput,
    "concrete_circular_volume": ConcreteCircularInput,
    "formwork_rect_column_area": FormworkRectColumnInput,
    "masonry_volume": MasonryInput,
    "earthwork_sloped_trench": EarthworkTrenchInput,
    "unit_area_cost": UnitAreaCostInput,
    "price_index_adjustment": PriceIndexAdjustmentInput,
}


if __name__ == "__main__":
    # 自检：一条 C30 现浇矩形柱从基价到含税合价的确定性走一遍（dict 入参走 pydantic 校验）
    base = compose_base_price_tool({"resources": [
        {"category": "人工", "name": "混凝土综合工", "quantity": 1.2, "unit_price": 150},
        {"category": "材料", "name": "C30商品混凝土", "quantity": 1.01, "unit_price": 480},
        {"category": "机械", "name": "混凝土振捣器", "quantity": 0.05, "unit_price": 30},
    ]})
    up = comprehensive_unit_price_tool({
        "labor_cost": base["labor_cost"], "material_cost": base["material_cost"],
        "machine_cost": base["machine_cost"], "management_fee_rate": 15, "profit_rate": 10,
        "fee_base": "labor_machine", "risk_rate": 1,
    })
    ln = line_amount_tool({"unit_price": up["unit_price"], "quantity": 8.5})
    unit = unit_project_cost_tool({"division_fee": ln["amount"], "measure_fee": 1200,
                                   "fee_levy": 800, "tax_rate": 9})
    print("基价:", base["base_price"])
    print("综合单价:", up["unit_price"])
    print("综合合价:", ln["amount"])
    print("单位工程含税造价:", unit["total"], unit["breakdown"])
    print("tools:", len(TOOLS), "| input_models:", len(INPUT_MODELS))
