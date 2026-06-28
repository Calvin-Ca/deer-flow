"""P2 综合单价组装 —— 确定性「算钱」tool ``compute_unit_price``。

按 GB 50500-2024 §2.0.9 综合单价构成（``price_composition`` 抽得）：

    综合单价 = 人工费 + 材料费 + 施工机具使用费 + 管理费 + 利润 + 风险费用（不含增值税）

其中人材机费来自前序组价取数（定额基价 / 信息价价差调整后，由调用方给定）；管理费 / 利润 /
风险费用 = **取费基数 × 费率**。本模块是**纯确定性计算器**，红线（来自 ``DEV.md §「组价能力对外暴露」``
四原则之「红线下沉到原语边界」+「算钱锁在代码、绝不容 LLM 介入」）：

1. **算钱不入 LLM**：本模块无任何模型调用，输入→输出唯一确定。
2. **pydantic 闸门**：``UnitPriceInput`` 强校验——金额 / 费率非负且有限（拒 NaN/Inf），取费基数显式声明
   （不内置地区默认，避免「取费基数」这一口径被悄悄杜撰）。无论谁、走哪条路径调用，都被这道 schema 拦在边界。
3. **不杜撰费率**：管理费 / 利润 / 风险率**一律由调用方传入**——深圳计价费率库（``fee_rate``）当前**只收录**
   安文措施费 / 夜间 / 赶工 / 总包服务费 / 增值税 / 附加税 / 工程保险费，**不含管理费、利润率**；定额
   ``base_price`` 亦为净人材机基价（不含管理费利润）。故管理费 / 利润率须由人工（HITL）按工程类别给定，
   本工具绝不替其填默认值（填了即杜撰动钱数字）。增值税率库内有，但仍作显式入参，由调用方决定取值。

含税造价（可选）：综合合价（综合单价 × 工程量）→ + 增值税 = 含税合价。措施费 / 其他项目费 / 规费等
项目级汇总属 ``rollup_cost``（远期），不在本工具范围。
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 综合单价构成溯源（GB 50500-2024 §2.0.9，与 ce-code price_composition.jsonl 一致）
_CLAUSE = "GB 50500-2024 §2.0.9"
_COMPONENTS = ["人工费", "材料费", "施工机具使用费", "管理费", "利润", "风险费用"]


class FeeBase(str, Enum):
    """取费基数 —— 管理费 / 利润 / 风险费用按哪个口径乘费率。

    深圳房建定额管理费、利润惯以「人工费 + 机械费」为基数，但口径随专业 / 文件而变，故**显式声明、不设默认**。
    取值：``labor``=人工费 / ``labor_machine``=人工费+机械费 / ``lmm``=人材机（人工+材料+机械）。
    """

    labor = "labor"
    labor_machine = "labor_machine"
    lmm = "lmm"


def _money(value: Decimal) -> float:
    """量化为 2 位小数（元，ROUND_HALF_UP 工程惯例），返回 float。

    参数：value —— Decimal 金额。
    返回：四舍五入到分的 float。
    """
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class UnitPriceInput(BaseModel):
    """``compute_unit_price`` 的入参 schema（动钱闸门）。

    字段（金额单位元、费率单位 %）：
      labor_cost / material_cost / machine_cost —— 人工费 / 材料费 / 施工机具使用费（每清单计量单位），均 ≥0。
      management_fee_rate / profit_rate —— 管理费率 / 利润率（%），≥0；**须由调用方按工程类别给定**（库内无）。
      risk_rate —— 风险费率（%），≥0，默认 0（GB 50500 §2.0.9「一定范围内的风险费用」，常按约定计）。
      fee_base —— 取费基数（``FeeBase``），**必填无默认**：管理费/利润/风险按此口径乘费率。
      tax_rate —— 增值税率（%），可选；给定则额外算含税合价。
      quantity —— 工程量（清单数量），>0，默认 1：综合合价 = 综合单价 × quantity。
    校验：所有数值拒 NaN/Inf（``allow_inf_nan=False`` + 显式 finite 校验），费率 / 金额非负，quantity 为正。
    """

    model_config = ConfigDict(extra="forbid")

    labor_cost: float = Field(..., ge=0, allow_inf_nan=False, description="人工费（元/计量单位）")
    material_cost: float = Field(..., ge=0, allow_inf_nan=False, description="材料费（元/计量单位）")
    machine_cost: float = Field(..., ge=0, allow_inf_nan=False, description="施工机具使用费（元/计量单位）")
    management_fee_rate: float = Field(..., ge=0, allow_inf_nan=False, description="管理费率 %（调用方给定）")
    profit_rate: float = Field(..., ge=0, allow_inf_nan=False, description="利润率 %（调用方给定）")
    risk_rate: float = Field(0.0, ge=0, allow_inf_nan=False, description="风险费率 %，默认 0")
    fee_base: FeeBase = Field(..., description="取费基数：labor / labor_machine / lmm")
    tax_rate: float | None = Field(None, ge=0, allow_inf_nan=False, description="增值税率 %，给定则算含税")
    quantity: float = Field(1.0, gt=0, allow_inf_nan=False, description="工程量（清单数量），默认 1")

    @field_validator("tax_rate")
    @classmethod
    def _tax_finite(cls, v: float | None) -> float | None:
        """增值税率为 None 时跳过；非 None 时 allow_inf_nan 已拦 NaN/Inf，此处仅留作显式护栏。"""
        if v is not None and not math.isfinite(v):
            raise ValueError("tax_rate 必须为有限数")
        return v


def _fee_base_amount(inp: UnitPriceInput) -> Decimal:
    """按 ``fee_base`` 算取费基数金额（Decimal）。

    参数：inp —— 已校验入参。
    返回：管理费 / 利润 / 风险所乘的基数（元，Decimal）。
    """
    labor = Decimal(str(inp.labor_cost))
    material = Decimal(str(inp.material_cost))
    machine = Decimal(str(inp.machine_cost))
    if inp.fee_base is FeeBase.labor:
        return labor
    if inp.fee_base is FeeBase.labor_machine:
        return labor + machine
    return labor + material + machine  # lmm


def compute_unit_price(inp: UnitPriceInput) -> dict[str, Any]:
    """人材机费 + 费率 → 综合单价 →（可选）含税合价，确定性公式（GB 50500 §2.0.9）。

    参数：inp —— ``UnitPriceInput``（已过 pydantic 闸门：金额/费率非负有限、取费基数显式、工程量为正）。
    返回：``{unit_price, breakdown, total_price, tax, total_with_tax, formula, provenance, notes}``：
      - breakdown —— 六项构成（人材机 + 管理费 + 利润 + 风险，各四舍五入到分）+ 取费基数与基数金额；
      - unit_price —— 综合单价（六项之和，不含增值税）；total_price —— 综合合价 = 综合单价 × 工程量；
      - tax / total_with_tax —— 仅当 tax_rate 给定时非空（增值税 = 综合合价 × tax_rate%、含税合价 = 二者和）；
      - provenance —— 构成溯源条文 + 构成项；notes —— 红线声明（不含税口径、费率来源、不杜撰）。
    每费用项独立量化到分（造价惯例），综合单价 = 已量化各项之和，故无累加误差、结果唯一。
    """
    labor = Decimal(str(inp.labor_cost))
    material = Decimal(str(inp.material_cost))
    machine = Decimal(str(inp.machine_cost))
    base = _fee_base_amount(inp)

    management_fee = (base * Decimal(str(inp.management_fee_rate)) / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    profit = (base * Decimal(str(inp.profit_rate)) / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    risk = (base * Decimal(str(inp.risk_rate)) / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    labor_q = labor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    material_q = material.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    machine_q = machine.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # 综合单价 = 已量化六项之和（不含增值税，§2.0.9）
    unit_price_dec = labor_q + material_q + machine_q + management_fee + profit + risk
    total_dec = (unit_price_dec * Decimal(str(inp.quantity))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    tax: float | None = None
    total_with_tax: float | None = None
    if inp.tax_rate is not None:
        tax_dec = (total_dec * Decimal(str(inp.tax_rate)) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        tax = float(tax_dec)
        total_with_tax = _money(total_dec + tax_dec)

    return {
        "unit_price": float(unit_price_dec),
        "breakdown": {
            "人工费": float(labor_q),
            "材料费": float(material_q),
            "施工机具使用费": float(machine_q),
            "管理费": float(management_fee),
            "利润": float(profit),
            "风险费用": float(risk),
            "fee_base": inp.fee_base.value,
            "fee_base_amount": _money(base),
        },
        "quantity": inp.quantity,
        "total_price": float(total_dec),
        "tax": tax,
        "total_with_tax": total_with_tax,
        "formula": "综合单价 = 人工费 + 材料费 + 施工机具使用费 + 管理费 + 利润 + 风险费用（不含增值税）",
        "provenance": {"clause": _CLAUSE, "components": _COMPONENTS},
        "notes": [
            "综合单价不含增值税（GB 50500 §2.0.9）；含税合价仅在给定 tax_rate 时计算。",
            "管理费/利润/风险率由调用方给定，本工具不内置默认、不杜撰动钱数字（费率库未收录管理费/利润）。",
            "取费基数（fee_base）由调用方显式声明，不按地区猜测。",
        ],
    }


# 工程造价构成（GB 50500，§13 末尾汇总）—— 不杜撰精确 §号，用通用条文名做溯源
_ROLLUP_CLAUSE = "GB 50500 工程造价构成"
_ROLLUP_COMPONENTS = ["分部分项费", "措施项目费", "其他项目费", "规费", "税金"]


class RollupInput(BaseModel):
    """``rollup_cost`` 的入参 schema（§13 总造价汇总闸门）。

    字段（金额单位元、税率单位 %）：
      subtotal —— 分部分项合价（Σ各清单综合合价），≥0。
      measure_fee / other_fee / fee_levy —— 措施项目费 / 其他项目费 / 规费（项目级，由调用方录入），默认 0、均 ≥0。
      tax_rate —— 税金率（%），可选；给定则在税前造价上计税金 + 总造价（GB 50500：税金 = 税前造价 × 税率）。
    校验：金额非负且有限（拒 NaN/Inf）；``extra=forbid`` 拒多余字段——动钱那步无论谁调用都被这道 schema 拦在边界。
    """

    model_config = ConfigDict(extra="forbid")

    subtotal: float = Field(..., ge=0, allow_inf_nan=False, description="分部分项合价（元）")
    measure_fee: float = Field(0.0, ge=0, allow_inf_nan=False, description="措施项目费（元）")
    other_fee: float = Field(0.0, ge=0, allow_inf_nan=False, description="其他项目费（元）")
    fee_levy: float = Field(0.0, ge=0, allow_inf_nan=False, description="规费（元）")
    tax_rate: float | None = Field(None, ge=0, allow_inf_nan=False, description="税金率 %，给定则算税金+总造价")


def rollup_cost(inp: RollupInput) -> dict[str, Any]:
    """分部分项 + 措施 + 其他 + 规费 →（可选税金）总造价，确定性汇总（GB 50500，§13）。

    参数：inp —— ``RollupInput``（已过 pydantic 闸门：金额非负有限、不收多余字段）。
    返回：``{subtotal, measure_fee, other_fee, fee_levy, pre_tax_total, tax, total, formula, provenance, notes}``：
      - pre_tax_total —— 税前造价 = 分部分项 + 措施 + 其他 + 规费（各项 Decimal 量化到分后求和）；
      - tax / total —— 仅当 tax_rate 给定时非空（税金 = 税前造价 × 税率%、总造价 = 税前 + 税金）；
      - provenance —— 造价构成溯源；notes —— 红线声明（不杜撰税率、项目级费用由调用方录入）。
    红线：本工具只做带溯源的确定性汇总，**不替用户定政策数**（税率/措施/其他/规费均由调用方给定，不内置默认）。
    """
    sub = Decimal(str(inp.subtotal)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    measure = Decimal(str(inp.measure_fee)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    other = Decimal(str(inp.other_fee)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    levy = Decimal(str(inp.fee_levy)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pre_tax = sub + measure + other + levy  # 已量化各项之和，无累加误差

    tax: float | None = None
    total: float | None = None
    if inp.tax_rate is not None:
        tax_dec = (pre_tax * Decimal(str(inp.tax_rate)) / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        tax = float(tax_dec)
        total = _money(pre_tax + tax_dec)

    return {
        "subtotal": float(sub),
        "measure_fee": float(measure),
        "other_fee": float(other),
        "fee_levy": float(levy),
        "pre_tax_total": float(pre_tax),
        "tax": tax,
        "total": total,
        "formula": "税前造价 = 分部分项 + 措施项目费 + 其他项目费 + 规费；总造价 = 税前造价 + 税金（税前 × 税率）",
        "provenance": {"clause": _ROLLUP_CLAUSE, "components": _ROLLUP_COMPONENTS},
        "notes": [
            "税金 = 税前造价 × 税率（GB 50500）；税率由调用方给定，不内置默认、不杜撰。",
            "措施/其他/规费为项目级费用，由调用方（HITL）录入，本工具只做带溯源的确定性汇总，不替用户定政策数。",
        ],
    }
