"""FR-C 项目上下文核对 v1 —— BOQ 清单行确定性核对（``POST /cost/check`` 的内核）。

> 对应 AGENT_PRD §5.3（FR-C01~04 需项目上下文类）、C-01 溯源 / C-03 不幻觉 / C-04 检索层不算数。

**v1 范围（确定性核对，零 LLM）**：
  - **编码有效性**（FR-C04 错套的第一层）：格式（9 位全国码 / 12 位含顺序码）+ 库内存在性
    （``bill_get`` 按 spec 版本隔离查 bill_spec）；
  - **单位一致性**（FR-C04）：行单位 vs 规范 ``unit``/``unit_options``；
  - **名称偏离**（FR-C04 提示级）：行名称与规范名互不包含 → 提示可能错套；
  - **合价算术**（FR-C03 的算术部分，C-04 在代码算、不入 LLM）：``quantity × unit_price ≈ amount``。

**v1 诚实不做**（宁缺毋造，列入 ``unsupported`` 如实返回）：漏项检查（FR-C01，需领域配套项规则库）、
高估冒算/合理性判断（FR-C02，需可信单价基准）、单价偏差预警（FR-C03 价格部分，需组价基价——
2013 组价数据就绪后接入）。

**多租户边界**（PRD §8.2）：本模块无状态——BOQ 行随请求进出、不落盘不缓存；持久化挂载
（上传文件→会话）归 deer-flow 线程隔离层，不在本层做。

``bill_get_fn`` 可注入（单测离线，不依赖知识服务）。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from common import cost_client
from common.config import COST_DEFAULT_REGION, COST_DEFAULT_SPEC
from common.guards import VERDICT_PASS, GuardReport

# 9 位全国统一编码（清单项规范），或 12 位（含 3 位顺序码，实际清单常用）。
_CODE_RE = re.compile(r"^\d{9}(\d{3})?$")

# v1 诚实不做的核对面（如实返回，不假装覆盖）。
UNSUPPORTED_V1: tuple[str, ...] = (
    "漏项检查（FR-C01）：需领域配套项规则库，v1 未支持",
    "高估冒算/组价合理性判断（FR-C02）：需可信单价基准，v1 未支持",
    "单价偏差预警（FR-C03 价格部分）：需组价基价，该口径组价数据就绪后接入",
)

# 合价算术容差：绝对 0.01 元（分位四舍五入）与相对 0.5% 取大者。
_ABS_TOL = 0.01
_REL_TOL = 0.005


def _norm_unit(u: str | None) -> str:
    """单位字符串归一（比较用）：去空白、全角转半角常见写法、统一小写。"""
    if not u:
        return ""
    s = str(u).strip().lower()
    for a, b in (("㎡", "m2"), ("m²", "m2"), ("㎥", "m3"), ("m³", "m3"), ("平方米", "m2"),
                 ("立方米", "m3"), ("延长米", "m"), ("１", "1"), ("０", "0")):
        s = s.replace(a, b)
    return s


def check_row(row: dict[str, Any], spec: str,
              bill_get_fn: Callable[[str, str], dict | None]) -> dict[str, Any]:
    """核对单条 BOQ 行（确定性，零 LLM）。

    参数：row —— ``{code, name?, unit?, quantity?, unit_price?, amount?}``；spec —— 国标版本；
      bill_get_fn —— 编码查询函数 ``(code, spec) -> bill|None``。
    返回：``{code, checks:[{type,severity,detail}], bill:{name,unit,...}|None}``——checks 空=该行无发现；
      severity: error（错码/算术不符，应改）/ warn（单位/名称偏离，应核）。
    """
    checks: list[dict[str, str]] = []
    bill: dict | None = None
    code = str(row.get("code") or "").strip()

    # ① 编码有效性（格式 + 库内存在性；FR-C04 错套第一层）
    if not code:
        checks.append({"type": "missing_code", "severity": "error", "detail": "缺清单编码"})
    elif not _CODE_RE.fullmatch(code):
        checks.append({"type": "invalid_code", "severity": "error",
                       "detail": f"编码「{code}」非 9 位全国码（或 12 位含顺序码）格式"})
    else:
        bill = bill_get_fn(code[:9], spec)
        if bill is None:
            checks.append({"type": "code_not_found", "severity": "error",
                           "detail": f"编码 {code[:9]} 在国标 {spec} 版清单项规范中不存在（疑错套/串版本）"})

    if bill is not None:
        # ② 单位一致性（规范 unit + unit_options 任一命中即过）
        row_unit = _norm_unit(row.get("unit"))
        if row_unit:
            spec_units = {_norm_unit(bill.get("unit"))}
            spec_units.update(_norm_unit(u) for u in (bill.get("unit_options") or []))
            spec_units.discard("")
            if spec_units and row_unit not in spec_units:
                checks.append({"type": "unit_mismatch", "severity": "warn",
                               "detail": f"行单位「{row.get('unit')}」≠ 规范计量单位 {sorted(spec_units)}"
                                         f"（{bill.get('name')}）"})
        # ③ 名称偏离（提示级：互不包含 → 可能错套）
        row_name = str(row.get("name") or "").strip()
        bill_name = str(bill.get("name") or "").strip()
        if row_name and bill_name and row_name not in bill_name and bill_name not in row_name:
            checks.append({"type": "name_deviation", "severity": "warn",
                           "detail": f"行名称「{row_name}」与规范项名「{bill_name}」互不包含，请核对是否错套"})

    # ④ 合价算术（C-04：在代码算，不入 LLM）：三值齐才可核
    qty, price, amount = row.get("quantity"), row.get("unit_price"), row.get("amount")
    if qty is not None and price is not None and amount is not None:
        try:
            expect = float(qty) * float(price)
            diff = abs(expect - float(amount))
            if diff > max(_ABS_TOL, _REL_TOL * abs(float(amount))):
                checks.append({"type": "amount_mismatch", "severity": "error",
                               "detail": f"合价不符：量 {qty} × 单价 {price} = {expect:.2f} ≠ 填报合价 {amount}"})
        except (TypeError, ValueError):
            checks.append({"type": "amount_invalid", "severity": "error",
                           "detail": "量/单价/合价存在非数值，无法算术核对"})

    return {"code": code or None, "checks": checks,
            "bill": ({"name": bill.get("name"), "unit": bill.get("unit"),
                      "spec_version": bill.get("spec_version"), "doc_id": bill.get("doc_id")}
                     if bill else None)}


def check_boq(rows: list[dict[str, Any]], spec: str | None = None,
              region: str = COST_DEFAULT_REGION,
              bill_get_fn: Callable[[str, str], dict | None] | None = None) -> dict[str, Any]:
    """核对整份 BOQ 清单行（FR-C v1 主入口，确定性）。

    参数：rows —— BOQ 行列表（每行 ``{code, name?, unit?, quantity?, unit_price?, amount?}``）；
      spec —— 国标版本（缺省默认深圳·2013，§4.0 不反问）；region —— 地区（声明用）；
      bill_get_fn —— 可注入（默认打 ce-db ``/bill/{code}``）。
    返回：``{spec, region, total, rows_with_issues, issues:[{row_index, code, checks, bill}],
      unsupported, meta:{caliber, guard}}``——issues 只含有发现的行；unsupported 如实列 v1 不做的面；
      guard: C-01 溯源（发现均引 bill_spec 库内规范行）、报告型 verdict=pass（核对报告本身不拒答）。
    """
    resolved_spec = spec or COST_DEFAULT_SPEC
    _get = bill_get_fn or (lambda c, s: cost_client.bill_get(c, s))
    issues: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        r = check_row(row or {}, resolved_spec, _get)
        if r["checks"]:
            r["row_index"] = i
            issues.append(r)

    # 报告型 guard：核对报告本身不拒答（verdict=pass）；依据说明放 meta.basis，violations 只记真问题。
    report = GuardReport(verdict=VERDICT_PASS, tier="local")
    return {
        "spec": resolved_spec,
        "region": region,
        "total": len(rows),
        "rows_with_issues": len(issues),
        "issues": issues,
        "unsupported": list(UNSUPPORTED_V1),
        "meta": {
            "caliber": {"declared": f"{region}·{resolved_spec}", "region": region,
                        "spec": resolved_spec,
                        "spec_source": "user" if spec else "default"},
            "guard": report.as_meta(),
            "basis": f"逐行核对依据：国标 {resolved_spec} 版清单项规范（bill_spec）；"
                     f"合价算术为确定性计算（C-04 不入 LLM）",
        },
    }


# ─────────────────────────── 内置自测（注入 stub，无需服务）───────────────────────────
# 运行：cd ce-services && uv run python -m cost.check
def _selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    passed = failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        print(f"{'✓' if cond else '✗'} {name}{('  ' + extra) if extra else ''}")
        if cond:
            passed += 1
        else:
            failed += 1

    _DB = {"010401002": {"name": "实心砖墙", "unit": "m3", "unit_options": [],
                         "spec_version": "GB/T 50854-2013", "doc_id": "GB-50854"}}

    def stub_get(code: str, spec: str) -> dict | None:
        return _DB.get(code)

    # ① 全对行：无发现
    out = check_boq([{"code": "010401002", "name": "实心砖墙", "unit": "m³",
                      "quantity": 100, "unit_price": 500, "amount": 50000}],
                    spec="2013", bill_get_fn=stub_get)
    check("全对行：0 发现", out["rows_with_issues"] == 0, str(out["issues"]))
    check("口径声明：spec_source=user", out["meta"]["caliber"]["spec_source"] == "user")

    # ② 错码（库内不存在）→ error
    out2 = check_boq([{"code": "999999999"}], spec="2013", bill_get_fn=stub_get)
    check("错码：code_not_found error",
          out2["issues"][0]["checks"][0]["type"] == "code_not_found")

    # ③ 编码格式非法
    out3 = check_boq([{"code": "01040"}], spec="2013", bill_get_fn=stub_get)
    check("格式非法：invalid_code", out3["issues"][0]["checks"][0]["type"] == "invalid_code")

    # ④ 单位不符（warn）+ 12 位码取前 9 位查库
    out4 = check_boq([{"code": "010401002001", "unit": "m2"}], spec="2013", bill_get_fn=stub_get)
    types4 = [c["type"] for c in out4["issues"][0]["checks"]]
    check("12位码→前9位查库 + 单位不符 warn", types4 == ["unit_mismatch"], str(types4))

    # ⑤ 合价算术不符（C-04 确定性）→ error
    out5 = check_boq([{"code": "010401002", "quantity": 100, "unit_price": 500, "amount": 49000}],
                     spec="2013", bill_get_fn=stub_get)
    types5 = [c["type"] for c in out5["issues"][0]["checks"]]
    check("合价不符：amount_mismatch", "amount_mismatch" in types5, str(types5))

    # ⑥ 名称偏离（warn）
    out6 = check_boq([{"code": "010401002", "name": "空心砌块墙"}], spec="2013", bill_get_fn=stub_get)
    types6 = [c["type"] for c in out6["issues"][0]["checks"]]
    check("名称偏离：name_deviation warn", "name_deviation" in types6, str(types6))

    # ⑦ 缺省 spec → 默认口径 + unsupported 诚实列出
    out7 = check_boq([{"code": "010401002"}], bill_get_fn=stub_get)
    check("缺省 spec：默认口径 + spec_source=default",
          out7["spec"] == COST_DEFAULT_SPEC and out7["meta"]["caliber"]["spec_source"] == "default")
    check("v1 诚实 unsupported 列出 3 面", len(out7["unsupported"]) == 3)

    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
