"""cost 侧校验闸（③ 层，AGENT_DEV §9.2 ③「cost 侧对齐同一 GuardReport 契约」/ §9.5 T-A3）。

norm 侧把 C-01/02/03 收成 ``norm/guards.py``；cost 侧此前只有 provenance 信封逐字段透传
（need_review / no_source / missing_base / 501），**没有一个统一的结构化裁决**。本模块把 cost 组价
结果（``orchestration.compose`` 产物）过一遍确定性校验闸（无 LLM），产出**与 norm 同形的
``common.guards.GuardReport``** → 进 ``meta.guard``，前端/审计据同一契约读「口径纯净/溯源完整/裁决」。

cost 侧三红线映射（对齐 AGENT_PRD C-01/02/03）：
  - **C-03 无命中不幻觉**：选不出码（``code=None`` / ``need_review``）→ ``verdict=reject``、``tier=none``
    （转人工，不杜撰编码/价格）；选到码但价格未就绪/无定额映射 → 价格缺口如实透传（``provenance_complete=False``，
    不 reject——选码本身有价值）。
  - **C-02 口径纯净**：取数不得跨版串库（2013/2024 同 9 位码不同义）——扫定额子目 ``spec_version`` 的年份，
    与请求 ``spec`` 不符即 C-02 error、``caliber_pure=False``。
  - **C-01 溯源完整**：命中信息价的材料必带来源（``price_doc_id``/期段）、定额子目必带库号/版本——缺则
    ``provenance_complete=False`` + C-01 warn（不杜撰来源）。

**红线**：本层只对既成结果做**确定性裁决**（不改价、不选码、不调 LLM），与 norm 侧同原则——校验不靠弱模型自觉。
"""
from __future__ import annotations

import re
from typing import Any

from common.guards import (
    GUARD_C01,
    GUARD_C02,
    GUARD_C03,
    VERDICT_PASS,
    VERDICT_REJECT,
    GuardReport,
)

# cost 拒答/缺口给出路（C-03）：转人工话术，避免包装成「有价格的答案」。
_REVIEW_OUTLET = "建议人工复核构件描述与清单编码；缺价项按当期信息价人工询价，不杜撰。"

# 组价支持的国标版本（口径轴）。spec_version 里含哪个年份即判为哪版。
_KNOWN_VERSIONS = ("2013", "2024")


def _version_of(text: Any) -> str | None:
    """从 spec_version 文本抽国标版本年份（2013/2024）。

    参数：text —— 如 ``"SJG 171-2024"`` / ``"GB_T50854-2024"`` / ``"2013"``（None/无年份→None）。
    返回：``"2013"`` / ``"2024"`` / None（未识别，不参与口径判定、不误杀）。
    """
    if not text:
        return None
    s = str(text)
    for v in _KNOWN_VERSIONS:
        if re.search(rf"\b{v}\b", s) or v in s:
            return v
    return None


def audit_cost_result(
    result: dict,
    *,
    spec: str,
    region: str,
) -> GuardReport:
    """对 ``orchestration.compose`` 结果跑 cost 侧 C-01/02/03 校验闸 → ``GuardReport``。

    参数：
        result —— compose 产物（含 ``code`` / ``selection`` / ``price`` / ``price_status``）。
        spec —— 请求的国标版本（2013/2024，口径轴）；region —— 请求地区（深圳）。
    返回：
        ``GuardReport``（进 ``meta.guard``）：
        - 选不出码 → verdict=reject、tier=none（C-03，转人工不杜撰）；
        - 选到码 → verdict=pass、tier=local，再逐项标 C-02 跨版串库 / C-01 溯源缺口；
        - 价格未就绪/无定额 → provenance_complete=False（缺口透传，不 reject）。
    行为：纯确定性，不改 result、不调 LLM。
    """
    code = result.get("code")
    selection = result.get("selection") or {}
    price = result.get("price")
    price_status = result.get("price_status")

    # ── C-03：选不出码（含低置信 need_review）→ 转人工，不当权威结论呈现 ──
    if not code:
        report = GuardReport(verdict=VERDICT_REJECT, tier="none",
                             provenance_complete=False, caliber_pure=True)
        reason = selection.get("reason") or "候选内选不出可信编码"
        report.add(GUARD_C03, "error",
                   f"选不出码（need_review）：{reason}。{_REVIEW_OUTLET}")
        return report

    report = GuardReport(verdict=VERDICT_PASS, tier="local")

    # ── C-02：取数口径纯净（定额子目 spec_version 年份须与请求 spec 一致，防 2013/2024 串库）──
    quotas = (price or {}).get("quotas", []) or []
    for q in quotas:
        qv = _version_of(q.get("spec_version"))
        if qv and qv != str(spec):
            report.caliber_pure = False
            report.add(GUARD_C02, "error",
                       f"定额子目跨版串库：子目 {q.get('quota_code') or q.get('code')} "
                       f"spec_version={q.get('spec_version')!r}（{qv}版）≠ 请求 {spec} 版")

    # ── C-01：溯源完整（定额带库号/版本；命中信息价的材料带来源）──
    if price is None:
        # 选到码但无组价取数（未就绪 / 无定额映射）：价格缺口如实透传，非 reject。
        report.provenance_complete = False
        report.add(GUARD_C03, "warn",
                   f"选到码 {code} 但无组价取数（price_status={price_status}）：价格缺口如实透传，未杜撰。")
    else:
        if not quotas:
            report.provenance_complete = False
            report.add(GUARD_C01, "warn", f"编码 {code} 无映射定额子目：无定额可溯源（缺口透传，不杜撰）")
        for q in quotas:
            if not (q.get("quota_doc_id") or q.get("spec_version")):
                report.provenance_complete = False
                report.add(GUARD_C01, "warn",
                           f"定额子目 {q.get('quota_code') or q.get('code')} 缺库号/版本溯源")
            for r in (q.get("resources", []) or []):
                status = str(r.get("price_status") or "").strip().lower()
                # 命中信息价（有单价）却无来源文件 → 溯源缺口（不杜撰来源）。
                hit = status in {"ok", "matched"}
                if hit and r.get("unit_price") is not None and not r.get("price_doc_id"):
                    report.provenance_complete = False
                    report.add(GUARD_C01, "warn",
                               f"材料「{r.get('name')}」命中信息价但缺来源文件(price_doc_id)")

    return report


# ─────────────────────────── 内置自测（无需服务、无需 LLM）───────────────────────────
# 运行：cd ce-services && uv run python -m cost.guards
def _selftest() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    passed = failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        flag = "✓" if cond else "✗"
        print(f"{flag} {name}{('  ' + extra) if extra else ''}")
        if cond:
            passed += 1
        else:
            failed += 1

    # ① 选不出码 → reject / tier=none（C-03）
    rep = audit_cost_result(
        {"code": None, "selection": {"need_review": True, "reason": "候选都不匹配"},
         "price": None, "price_status": "skipped(need_review)"},
        spec="2024", region="深圳")
    check("C-03 选不出码：verdict=reject + tier=none",
          rep.verdict == VERDICT_REJECT and rep.tier == "none")
    check("C-03 选不出码：violations 带 C-03",
          any(v["code"] == GUARD_C03 for v in rep.violations))

    # ② 选到码 + 全合规取数 → pass / 纯净 / 完整
    good_price = {"quotas": [{
        "quota_code": "010006-15", "spec_version": "SJG 171-2024", "quota_doc_id": "SZ-SJG171",
        "resources": [{"name": "松杂枋板材", "price_status": "matched", "unit_price": 1904.0,
                       "price_doc_id": "SZ-2026-05", "category": "材料", "unit": "m3"}],
    }]}
    rep2 = audit_cost_result(
        {"code": "010502006", "selection": {"need_review": False}, "price": good_price,
         "price_status": "ok"}, spec="2024", region="深圳")
    check("合规：pass + 纯净 + 完整",
          rep2.verdict == VERDICT_PASS and rep2.caliber_pure and rep2.provenance_complete,
          f"pure={rep2.caliber_pure} prov={rep2.provenance_complete} viol={rep2.violations}")

    # ③ C-02 跨版串库：请求 2024，定额子目 spec_version 是 2013
    bad_ver = {"quotas": [{"quota_code": "X", "spec_version": "GB 50854-2013", "quota_doc_id": "D"}]}
    rep3 = audit_cost_result(
        {"code": "010502006", "selection": {}, "price": bad_ver, "price_status": "ok"},
        spec="2024", region="深圳")
    check("C-02 跨版串库：caliber_pure=False + C-02 error",
          rep3.caliber_pure is False and any(v["code"] == GUARD_C02 for v in rep3.violations))
    check("C-02 跨版串库：verdict 仍 pass（选码有价值，仅标口径）",
          rep3.verdict == VERDICT_PASS)

    # ④ C-01 溯源缺口：命中信息价但无 price_doc_id
    no_src = {"quotas": [{"quota_code": "Y", "spec_version": "SJG 171-2024", "quota_doc_id": "D",
              "resources": [{"name": "铁钉", "price_status": "matched", "unit_price": 5.0,
                             "price_doc_id": None, "category": "材料", "unit": "kg"}]}]}
    rep4 = audit_cost_result(
        {"code": "010502006", "selection": {}, "price": no_src, "price_status": "ok"},
        spec="2024", region="深圳")
    check("C-01 命中缺来源：provenance_complete=False + C-01 warn",
          rep4.provenance_complete is False and any(v["code"] == GUARD_C01 for v in rep4.violations))

    # ⑤ 选到码但价格未就绪（2013）→ pass 但 provenance_complete=False（缺口透传，不 reject）
    rep5 = audit_cost_result(
        {"code": "010502006", "selection": {}, "price": None,
         "price_status": "未就绪(该国标版本组价数据未就绪，仅返回选码)"}, spec="2013", region="深圳")
    check("价格未就绪：pass + provenance_complete=False（不 reject）",
          rep5.verdict == VERDICT_PASS and rep5.provenance_complete is False)

    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
