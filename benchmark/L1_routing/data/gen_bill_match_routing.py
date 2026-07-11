"""生成清单匹配意图的路由评测集 bill_match_routing.jsonl（100 条，全部锚定真实金标）。

**一条不编**：query 的项目特征全文逐字取自清单匹配真值金标——
- ``benchmark/L3_retrieval/data/match_gold_2013.jsonl``（91 条真实项目清单行，源自评测 xlsx 原件；
  按 query 去重保首条后 90 条，重复的那对是 011702002/011702004 柱模板歧义样本）；
- ``benchmark/L3_retrieval/data/match_gold.jsonl``（10 条 2024 口径金标）。
本脚本只做两件事：给真实特征文本套上「造价员在对话框里会怎么问」的问法模板（确定性轮转，
不随机），并按 T9-1 口径策略推导路由标签。90 + 10 = 100 条，id M1~M100。

**标签口径**（与 agent_routing_eval.jsonl 同 schema，可直接 union / 单独上传）：
- 全部 ``expect_route=true``（清单匹配必须路由到能力，不许 lead 凭记忆答码——串库红线）；
- 全部 ``expect_clarify=false``：特征是完整真实清单行不缺特征；缺版本按 T9-1 口径策略默认
  深圳·2013 **不反问**（反问=违例，但这是口径策略、非安全红线）；
- 角色轮转（每 15 条：10 无版本选码 / 3 带版本选码 / 2 编码核实）：
  - 选码（agent=cost-agent）：group=no_version（gold=2013 默认口径）/ with_version（问法带版本）；
  - 核实（agent=cost-check，group=code_check）：问法带该行**真实金码**问「对不对/特征有漏吗」，
    期望走 verify_bill_code（或 task 派 cost-agent，均计路由命中）；
- 2024 金标 10 条全部 with_version（问法必须带 2024，否则默认 2013 与金码口径不符）。

再生成：``python benchmark/L1_routing/data/gen_bill_match_routing.py``（幂等覆盖输出文件；
金标增补后重跑即扩容，id 顺次后移，勿与已跑基线混比）。
"""
from __future__ import annotations

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent  # benchmark/L1_routing/data/
_GOLD_2013 = _HERE.parents[1] / "L3_retrieval" / "data" / "match_gold_2013.jsonl"
_GOLD_2024 = _HERE.parents[1] / "L3_retrieval" / "data" / "match_gold.jsonl"
_OUT = _HERE / "bill_match_routing.jsonl"

# 问法模板（确定性轮转）。{q}=金标特征原文逐字，{code}=该行真实金码。
_T_NO_VERSION = [
    "{q}，套什么清单码？",
    "帮我给这条清单项选编码：{q}",
    "{q}。这个项目特征对应哪条清单？",
    "给「{q}」选个清单编码",
    "{q}，清单编码是多少？",
    "这条项目特征帮我匹配下清单：{q}",
]
_T_WITH_VERSION_2013 = [
    "按2013清单规范，{q}，套什么清单码？",
    "按13版国标给这条选码：{q}",
    "{q}，按2013版清单规范应该套哪条清单？",
]
_T_WITH_VERSION_2024 = [
    "按2024国标，{q}，套什么清单码？",
    "按2024版清单规范给「{q}」选编码",
]
_T_CODE_CHECK = [
    "{q}，我套的清单码是 {code}，对不对？",
    "帮我核实：{q}，编码 {code} 有没有套错？特征有漏吗？",
]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _dedupe_by_query(rows: list[dict]) -> list[dict]:
    """按 query 去重保首条（重复 query 对路由评测无增量，去重后 2013 侧恰 90 条）。"""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["query"] in seen:
            continue
        seen.add(r["query"])
        out.append(r)
    return out


def _note(src_file: str, lineno: int, gold: list[str], orig_note: str) -> str:
    orig = (orig_note or "").strip()
    if len(orig) > 60:
        orig = orig[:60] + "…"
    return f"真实金标 L3_retrieval/data/{src_file}#L{lineno}；金码 {'、'.join(gold)}；{orig}"


def _role_for(i: int) -> str:
    """2013 侧角色轮转：每 15 条 = 10 无版本选码 + 3 带版本选码 + 2 编码核实。"""
    r = i % 15
    if r in (12, 13):
        return "code_check"
    if r in (9, 10, 11):
        return "with_version"
    return "no_version"


def build_cases() -> list[dict]:
    rows_2013 = _dedupe_by_query(_read_jsonl(_GOLD_2013))
    rows_2024 = _read_jsonl(_GOLD_2024)
    # 保留去重前的行号供溯源。
    lineno_2013: dict[str, int] = {}
    for n, r in enumerate(_read_jsonl(_GOLD_2013), 1):
        lineno_2013.setdefault(r["query"], n)

    cases: list[dict] = []
    counters = {"no_version": 0, "with_version": 0, "code_check": 0}
    for i, r in enumerate(rows_2013):
        role = _role_for(i)
        k = counters[role]
        counters[role] += 1
        q, gold = r["query"], r["gold"]
        if role == "no_version":
            query = _T_NO_VERSION[k % len(_T_NO_VERSION)].format(q=q)
            agent, group = "cost-agent", "no_version"
            note = _note("match_gold_2013.jsonl", lineno_2013[q], gold, r.get("note", "")) + "；缺版本不反问、默认深圳·2013（T9-1 口径策略）"
        elif role == "with_version":
            query = _T_WITH_VERSION_2013[k % len(_T_WITH_VERSION_2013)].format(q=q)
            agent, group = "cost-agent", "with_version"
            note = _note("match_gold_2013.jsonl", lineno_2013[q], gold, r.get("note", ""))
        else:
            query = _T_CODE_CHECK[k % len(_T_CODE_CHECK)].format(q=q, code=gold[0])
            agent, group = "cost-check", "code_check"
            note = _note("match_gold_2013.jsonl", lineno_2013[q], gold, r.get("note", "")) + "；问句带真实金码，期望 verify_bill_code（或 task 派 cost-agent）核实"
        cases.append({"agent": agent, "group": group, "query": query, "gold": "2013", "note": note})

    for j, r in enumerate(rows_2024):
        query = _T_WITH_VERSION_2024[j % len(_T_WITH_VERSION_2024)].format(q=r["query"])
        cases.append({
            "agent": "cost-agent",
            "group": "with_version",
            "query": query,
            "gold": "2024",
            "note": _note("match_gold.jsonl", j + 1, r["gold"], r.get("note", "")) + "；2024 金码口径，问法须带版本",
        })

    out = []
    for n, c in enumerate(cases, 1):
        out.append({"id": f"M{n}", "agent": c["agent"], "group": c["group"], "query": c["query"], "expect_route": True, "expect_clarify": False, "gold": c["gold"], "note": c["note"]})
    return out


def main() -> None:
    cases = build_cases()
    assert len(cases) == 100, f"应恰为 100 条，实际 {len(cases)}"
    assert len({c["query"] for c in cases}) == 100, "query 不应有重复"
    with _OUT.open("w", encoding="utf-8", newline="\n") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    stats: dict[str, int] = {}
    for c in cases:
        stats[c["group"]] = stats.get(c["group"], 0) + 1
    print(f"✓ 已生成 {_OUT.name}：{len(cases)} 条；分组 {stats}")


if __name__ == "__main__":
    main()
