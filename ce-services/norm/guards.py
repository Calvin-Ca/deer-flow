"""校验闸成层（T-A3）—— 把 C-01 溯源 / C-02 口径纯净 / C-03 拒答 统一为**显式 guard**。

> 对应 PRD C-01/C-02/C-03、§8.3 web 三道闸、
> §5.1 依据卡 provenance 字段。

**为什么成层**：这三条红线此前散在各处（零召回拒答内联在 router、溯源靠 LLM 自觉填 cited_clauses、
口径纯净没人管），且**靠弱模型自觉**=不可靠。本模块把它们收成进检索后/生成后**确定性执行**的一层
（无 LLM），结构化拦截并产出可审计的 ``GuardReport``。

**三闸（norm-qa 侧，结构化、不靠 LLM）**：
  - **C-03 拒答**：零召回 → 集中化的「无依据」结构化拒答（**给出路**：说明已查范围 + 建议渠道），
    不喂空上下文给 LLM 编答案。``reject_no_recall``。
  - **C-02 口径纯净**：逐条 cited_clause 抽 family/version，与本次裁定的 resolved 规范**冲突即剔除**
    （family 不符=串库污染、版本不符=跨版串引），保证输出无他部/他版条文混入。``audit_answer``。
  - **C-01 溯源完整**：保留的 cited_clause 的 ``standard`` 字段**确定性规范化为 resolved 全码**
    （标准号+版本恒定带齐，不靠 LLM 抄对）；缺条款号者标记溯源不完整。``audit_answer``。

裁决（``verdict``）：原本有引用但**全被 C-02 剔光**（=答案建立在污染条文上）→ ``reject`` 降级为无依据
拒答；否则 ``pass``（清洗后的答案 + 报告照常返回）。

定位：当前覆盖 norm-qa（C-01/02/03 在此最缺显式层）；cost 侧已有 provenance 信封做结构化拦截
（need_review/no_source，见 ``cost/provenance.py``），后续可对齐同一 GuardReport 契约。
"""
from __future__ import annotations

from typing import Any

from common.guards import (  # ③ 共享契约（norm/cost 同形 GuardReport，见 common/guards.py）
    GUARD_C01,
    GUARD_C02,
    GUARD_C03,
    VERDICT_PASS,
    VERDICT_REJECT,
    GuardReport,
)
from norm.standard_router import family_version_of

# 拒答给出路（C-03 / EH-02）：统一的「建议渠道」话术，避免做「死工具」。
_REJECT_OUTLET = "建议核对规范代号/版本，或换用更贴合的规范、补充更具体的问法；必要时查阅住建部及深圳住建局造价站原文。"
_DISCLAIMER = "本回答仅供参考，不替代专业造价审核。"


def reject_no_recall(standard: str, *, search_meta: dict | None = None,
                     extra_aspects: list[str] | None = None) -> tuple[dict, GuardReport]:
    """C-03 零召回拒答（集中化，给出路）。返回 (拒答 answer, GuardReport)。

    参数：standard —— 本次裁定的规范代号（写入 meta）；search_meta —— 透传知识层检索 meta；
      extra_aspects —— 追加进 uncertain_aspects 的说明。
    """
    report = GuardReport(verdict=VERDICT_REJECT, provenance_complete=False,
                         caliber_pure=True, cited_total=0, cited_dropped=0, tier="none")
    report.add(GUARD_C03, "error", f"零召回（standard={standard}）：库内无相关条文，拒答不编造")
    aspects = ["检索零召回，可能问题超出该规范范围或需换用其它规范"]
    if extra_aspects:
        aspects.extend(extra_aspects)
    answer = {
        "answer": f"未在所选造价规范（{standard}）中检索到与问题相关的条文，无法作答。{_REJECT_OUTLET}{_DISCLAIMER}",
        "cited_clauses": [],
        "uncertain_aspects": aspects,
        "out_of_scope_warnings": [],
    }
    return answer, report


def _normalize_cited(clause: dict, resolved_family: str, resolved_version: str,
                     resolved_standard: str, report: GuardReport) -> dict | None:
    """单条 cited_clause 过 C-02/C-01：冲突剔除（返回 None），否则规范化 standard 字段后返回。"""
    declared = str(clause.get("standard") or "")
    fam, ver = family_version_of(declared)

    # C-02 口径纯净：声明了 family 且与 resolved 不符 → 串库污染，剔除。
    if fam and fam != resolved_family:
        report.add(GUARD_C02, "error",
                   f"剔除他部条文：cited.standard={declared!r}（{fam}）≠ 裁定 {resolved_family}")
        return None
    # C-02：family 相符但声明了 version 且与 resolved 不符 → 跨版串引，剔除（同 9 位码不同义）。
    if ver and ver != resolved_version:
        report.add(GUARD_C02, "error",
                   f"剔除跨版条文：cited.standard={declared!r}（{ver}版）≠ 裁定 {resolved_version}版")
        return None

    # C-01 溯源完整：standard 字段确定性规范化为 resolved 全码（标准号+版本恒带齐，不靠 LLM 抄）。
    out = dict(clause)
    out["standard"] = resolved_standard
    if declared and declared != resolved_standard:
        out["standard_declared"] = declared  # 留痕 LLM 原写法（审计用）
    # C-01：条款号缺失 → 该条溯源不完整（不剔除，仅标记，避免误杀「整段背景」型引用）。
    if not str(clause.get("clause") or "").strip():
        report.add(GUARD_C01, "warn", f"引用缺条款号：{declared or resolved_standard}")
        report.provenance_complete = False
    return out


def audit_answer(answer: dict, *, resolved_standard: str,
                 search_meta: dict | None = None) -> tuple[dict, GuardReport]:
    """对已生成回答跑 C-01 + C-02 校验闸（非零召回路径）。

    参数：answer —— generation.answer 产物；resolved_standard —— 本次裁定的规范全码（如 gb50854-2024）。
    返回：(清洗后的 answer, GuardReport)。

    行为：
      - C-02：剔除 family/版本与 resolved 冲突的 cited_clause（输出保证口径纯净）。
      - C-01：保留引用 standard 规范化为 resolved 全码；缺条款号标记 provenance_complete=False。
      - verdict：原本有引用但全被剔光 → reject（答案建立在污染条文上，降级为无依据拒答）；否则 pass。
    """
    resolved_family, resolved_version = family_version_of(resolved_standard)
    cited = answer.get("cited_clauses") or []
    report = GuardReport(verdict=VERDICT_PASS, cited_total=len(cited), tier="local")

    kept: list[dict] = []
    for c in cited:
        norm = _normalize_cited(c, resolved_family, resolved_version, resolved_standard, report)
        if norm is None:
            report.cited_dropped += 1
        else:
            kept.append(norm)

    report.caliber_pure = report.cited_dropped == 0

    # 原本有引用但全被 C-02 剔光：答案建立在污染条文上 → 降级拒答（不返回无根据的「答案」）。
    if cited and not kept:
        rej, rej_report = reject_no_recall(
            resolved_standard, search_meta=search_meta,
            extra_aspects=["原引用条文均非本规范口径，已全部剔除（疑似 LLM 串引/幻觉）"],
        )
        rej_report.violations = report.violations + rej_report.violations
        rej_report.cited_total = report.cited_total
        rej_report.cited_dropped = report.cited_dropped
        rej_report.caliber_pure = False
        return rej, rej_report

    out = dict(answer)
    out["cited_clauses"] = kept
    if not kept:
        # LLM 本就没给引用（如「所提供条文未涉及」型超范围回答）：标溯源不完整，但不强拒（答案自身已声明）。
        report.provenance_complete = False
    return out, report


# ─────────────────────────── 内置自测（无需服务、无需 LLM）───────────────────────────
# 运行：cd ce-services && uv run python -m norm.guards
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

    # ① C-03 零召回拒答
    rej, rep = reject_no_recall("gb50854-2024")
    check("C-03 零召回：verdict=reject", rep.verdict == VERDICT_REJECT)
    check("C-03 零召回：tier=none、给出路", rep.tier == "none" and "建议" in rej["answer"])

    # ② C-02 口径纯净：他部条文剔除（resolved 50854，引用混入 50500）
    ans = {"answer": "见条文。", "cited_clauses": [
        {"clause": "5.1.1", "standard": "GB_T50854-2024_房屋建筑与装饰工程工程量计算标准", "text": "...", "relevance": "direct"},
        {"clause": "3.2.1", "standard": "GB_50500-2013_建设工程工程量清单计价规范", "text": "...", "relevance": "direct"},
    ], "uncertain_aspects": [], "out_of_scope_warnings": []}
    out, rep = audit_answer(ans, resolved_standard="gb50854-2024")
    check("C-02 他部剔除：保留 1 / 剔 1", len(out["cited_clauses"]) == 1 and rep.cited_dropped == 1,
          f"kept={len(out['cited_clauses'])} dropped={rep.cited_dropped}")
    check("C-02 他部剔除：caliber_pure=False", rep.caliber_pure is False)
    check("C-01 规范化：保留条 standard=resolved 全码",
          out["cited_clauses"][0]["standard"] == "gb50854-2024")

    # ③ C-02 跨版串引剔除（resolved 50854-2024，引用 50854-2013）
    ans2 = {"answer": "x", "cited_clauses": [
        {"clause": "5.1.1", "standard": "gb50854-2013", "text": "...", "relevance": "direct"},
    ], "uncertain_aspects": [], "out_of_scope_warnings": []}
    out2, rep2 = audit_answer(ans2, resolved_standard="gb50854-2024")
    check("C-02 跨版剔除：全剔→verdict=reject", rep2.verdict == VERDICT_REJECT and rep2.cited_dropped == 1)

    # ④ C-01 缺条款号标记（family 相符、无版本声明、无 clause）
    ans3 = {"answer": "x", "cited_clauses": [
        {"clause": "", "standard": "GB 50854", "text": "...", "relevance": "contextual"},
    ], "uncertain_aspects": [], "out_of_scope_warnings": []}
    out3, rep3 = audit_answer(ans3, resolved_standard="gb50854-2024")
    check("C-01 缺条款号：provenance_complete=False、不剔除",
          rep3.provenance_complete is False and len(out3["cited_clauses"]) == 1)
    check("C-01 缺条款号：standard 仍规范化为全码",
          out3["cited_clauses"][0]["standard"] == "gb50854-2024")

    # ⑤ 全合规：family/版本相符、有条款号 → pass、纯净、完整
    ans4 = {"answer": "x", "cited_clauses": [
        {"clause": "5.1.1", "standard": "gb50854-2024", "text": "...", "relevance": "direct"},
    ], "uncertain_aspects": [], "out_of_scope_warnings": []}
    out4, rep4 = audit_answer(ans4, resolved_standard="gb50854-2024")
    check("合规：pass + 纯净 + 完整",
          rep4.verdict == VERDICT_PASS and rep4.caliber_pure and rep4.provenance_complete)

    print(f"\n自测：{passed} 过 / {failed} 败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
