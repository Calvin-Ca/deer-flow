"""组价结果确定性复核器（generator–verifier 的"地基"）—— 纯函数、无 I/O、无 LLM。

`cost-critic` 子智能体的确定性预检：把「能用规则判的」从「要 LLM 判语义的」里剥出来。本模块只做
**规则能定死对错**的检查（算术独立重算 / 完整性 / 编码格式 / 地域版本隔离 / 价格覆盖），语义匹配
（构件↔码是否贴切、漏项错套）交给子智能体的 LLM 那半。

**算术独立重算**是核心：按 GB 50500 口径（人材机费 + 管理费 + 利润 + 风险）**从 components 重算**综合单价，
与组价结果里 agent 报出来的数逐分比对——抓弱模型在「工具算对了、写到答案里写错了」这类转写/幻觉误差
（口径与 ``calc_engine.calc_unit_rate`` 一致，但**刻意独立实现、禁止复用其代码**——复用会让公式 bug
同时骗过生成与复核，自己验自己恒真；两套独立算术才是「独立重算」的实质）。
"""
from __future__ import annotations

import re
from typing import Any

from langchain.tools import tool

from .state import agent_allowed_specs

_CODE_RE = re.compile(r"^(\d{9})(?:\d{3})?$")  # 9 位全国码，或 12 位含顺序号（取前 9）
_CATEGORIES = ("人工", "材料", "机械", "其他")
_AMOUNT_TOL = 0.02  # 综合单价重算容差（元）：逐步 round 到分，正常应逐分一致


def _num(v: Any) -> float | None:
    """取数值；非数值/None → None。"""
    return float(v) if isinstance(v, (int, float)) else None


def _rate(rates: dict[str, Any], *keys: str) -> float:
    """从费率块按多个候选键名取率（兼容 management_rate / management_fee_rate 等写法），缺省 0。"""
    for k in keys:
        v = _num(rates.get(k))
        if v is not None:
            return v
    return 0.0


def recompute_unit_price(components: list[dict[str, Any]], rates: dict[str, Any]) -> dict[str, Any]:
    """从工料机 components + 费率**独立重算**综合单价（口径同 unit_rate_node，逐步 round 2 位）。

    参数：components —— 工料机行，每行 category(人工/材料/机械/其他) + consumption|quantity + unit_price|price；
      rates —— 费率块（management/profit/risk，兼容 *_rate / *_fee_rate 写法）。
    返回：``{labor, material, machine, other, rmm, management_fee, profit, risk_fee, unit_price, skipped}``；
      skipped 为缺数值被跳过的行数（>0 说明重算不完整，调用方据此降级为 warn 而非 critical）。
    """
    totals = {c: 0.0 for c in _CATEGORIES}
    skipped = 0
    for comp in components or []:
        if not isinstance(comp, dict):
            skipped += 1
            continue
        qty = _num(comp.get("consumption", comp.get("quantity")))
        price = _num(comp.get("unit_price", comp.get("price")))
        if qty is None or price is None:
            skipped += 1
            continue
        cat = comp.get("category")
        totals[cat if cat in totals else "其他"] += round(qty * price, 2)

    labor, material = round(totals["人工"], 2), round(totals["材料"], 2)
    machine, other = round(totals["机械"], 2), round(totals["其他"], 2)
    rmm = round(labor + material + machine + other, 2)  # 人材机费（费率基数）
    mgmt = round(rmm * _rate(rates, "management_rate", "management_fee_rate"), 2)
    profit = round(rmm * _rate(rates, "profit_rate", "profit"), 2)
    risk = round(rmm * _rate(rates, "risk_rate", "risk"), 2)
    return {
        "labor": labor, "material": material, "machine": machine, "other": other,
        "rmm": rmm, "management_fee": mgmt, "profit": profit, "risk_fee": risk,
        "unit_price": round(rmm + mgmt + profit + risk, 2), "skipped": skipped,
    }


def _finding(ftype: str, severity: str, detail: str) -> dict[str, Any]:
    """一条复核发现。severity: critical(硬错，打回) / warn(存疑，转 HITL) / info。"""
    return {"type": ftype, "severity": severity, "detail": detail}


def verify_cost_result(result: dict[str, Any]) -> dict[str, Any]:
    """确定性复核一条组价结果，返回 verdict + findings（不判语义，那半交子智能体 LLM）。

    参数：result —— 组价结果 dict，可含：``code``(清单码)、``region``/``spec``(口径)、
      ``components``(工料机行)、``rates``(费率)、``claimed_unit_price``(agent 报出的综合单价，被校验)、
      ``materials``(材料行，验价格覆盖)。字段尽量宽松匹配，缺则跳过对应检查。
    返回：``{verdict: pass|doubt|fail, findings:[...], recomputed:{...}|None}``。
      verdict：任一 critical → fail；无 critical 但有 warn → doubt；全清 → pass。
    """
    findings: list[dict[str, Any]] = []

    # 1) 编码格式：9 位（12 位取前 9）。缺 code 不报（可能是"只取数未选码"场景）。
    code = result.get("code")
    code9 = None
    if code is not None:
        m = _CODE_RE.match(str(code).strip())
        if m:
            code9 = m.group(1)
        else:
            findings.append(_finding("code_format", "critical", f"清单编码 {code!r} 非合法 9/12 位码"))

    # 2) 地域/版本隔离：声明必须是深圳 + 2013/2024（他省/他版本 = 串库红线）。
    region = result.get("region")
    if region is not None and str(region).strip() not in ("深圳", "shenzhen", "Shenzhen"):
        findings.append(_finding("region_leak", "critical", f"地域 {region!r} 非深圳（组价/价格锁深圳口径，串库红线）"))
    spec = result.get("spec")
    if spec is not None and str(spec).strip() not in agent_allowed_specs():
        # 允许清单复用 agent 面口径闸（默认仅 2013，评测 env 放开时自动跟随）——声明 2024 的
        # 组价结果本身即产品范围外产物（2026-07-12 裁定），复核不放行。
        findings.append(_finding("spec_invalid", "critical", f"版本 {spec!r} 不在允许口径 {sorted(agent_allowed_specs())}"))

    # 3) 算术独立重算：有 components + claimed_unit_price 才算；重算值 vs 报出值须逐分一致。
    recomputed = None
    components = result.get("components")
    claimed = _num(result.get("claimed_unit_price", result.get("unit_price")))
    if isinstance(components, list) and components:
        recomputed = recompute_unit_price(components, result.get("rates") or {})
        # 工料机不完整（有行缺数值）→ 重算不可信，降级 warn。
        if recomputed["skipped"]:
            findings.append(_finding("components_incomplete", "warn",
                                     f"{recomputed['skipped']} 行工料机缺 consumption/unit_price，重算不完整"))
        if claimed is not None and recomputed["skipped"] == 0:
            diff = round(abs(recomputed["unit_price"] - claimed), 2)
            if diff > _AMOUNT_TOL:
                findings.append(_finding(
                    "arith_mismatch", "critical",
                    f"综合单价重算={recomputed['unit_price']} 与报出={claimed} 差 {diff}（>容差 {_AMOUNT_TOL}）"
                    f"；人材机费={recomputed['rmm']} 管理费={recomputed['management_fee']} 利润={recomputed['profit']}"))
    elif claimed is not None:
        # 报了综合单价却没给 components → 无法核算，存疑（弱模型常凭空报数）。
        findings.append(_finding("uncheckable_price", "warn", "报出了综合单价但未提供 components，无法独立重算核对"))

    # 4) 人材机完整性：至少人工+材料两类非零（机械可为 0）。
    if recomputed is not None and recomputed["skipped"] == 0:
        if recomputed["labor"] <= 0 and recomputed["material"] <= 0:
            findings.append(_finding("basis_empty", "warn", "重算人材机费为 0（人工/材料均空），基价可能未取到"))

    # 5) 价格覆盖：materials 里有缺价（value 为空）→ 存疑（缺价不该杜撰）。
    materials = result.get("materials")
    if isinstance(materials, list) and materials:
        missing = [m.get("std") or m.get("raw") for m in materials
                   if isinstance(m, dict) and _num((m.get("price") or {}).get("value")) is None]
        if missing:
            findings.append(_finding("price_missing", "warn", f"{len(missing)} 项材料缺信息价：{missing[:5]}"))

    severities = {f["severity"] for f in findings}
    verdict = "fail" if "critical" in severities else ("doubt" if "warn" in severities else "pass")
    return {"verdict": verdict, "findings": findings, "recomputed": recomputed, "code": code9}


def verify_cost(result: dict[str, Any]) -> dict[str, Any]:
    """确定性复核一条组价结果（算术独立重算 + 完整性/编码/地域隔离/价格覆盖），返回结构化 verdict。

    用于 cost-critic 复核子智能体的"确定性预检"：规则能定死的对错在此判掉，语义匹配（构件↔码是否贴切、
    漏项错套）由子智能体的 LLM 那半判。**本工具不调 LLM、不猜数**——只对显式 result 字段做算术与规则核对。

    Args:
        result: 组价结果对象，可含 code / region / spec / components(工料机行) / rates(费率) /
            claimed_unit_price(被校验的综合单价) / materials(验价格覆盖)。字段宽松匹配，缺则跳过对应检查。
    """
    return verify_cost_result(result)


verify_cost_tool = tool("verify_cost", parse_docstring=True)(verify_cost)

__all__ = ["recompute_unit_price", "verify_cost_result", "verify_cost", "verify_cost_tool"]
