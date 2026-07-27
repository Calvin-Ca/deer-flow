"""
条文库质检（阶段 1.6）

读取 data/interim/clauses.jsonl，输出:
  - 控制台摘要（每本规范一行）
  - data/interim/parse_report.md（详细报告）

质检维度:
  1. 条款连续性  —— 条款号不应有大跳跃（连续缺口 > 5 视为警告）
  2. 表格完整率  —— caption 非空的表格比例 ≥ 90%
  3. 公式召回    —— 有公式的条款比例（参考值，无硬门限）
  4. 强制性完整  —— 每本规范必须有 > 0 条强制性条文
  5. 字长分布    —— char_len < 30 的条款数（极短条款可能是解析失败）
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CLAUSES_FILE = Path(__file__).parent.parent.parent / "data" / "interim" / "clauses.jsonl"
REPORT_FILE  = Path(__file__).parent.parent.parent / "data" / "interim" / "parse_report.md"

# 门限
# NOTE: caption 率不作为硬门限——续表（续表 X.X.X）没有独立 caption，属正常设计。
# 硬门限只检查 HTML 完整性（无截断表格）和强制性条文非零。


def _load(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _parse_no(clause_no: str) -> tuple[int, ...]:
    """把 '3.1.7' 拆成 (3, 1, 7)."""
    return tuple(int(x) for x in clause_no.split("."))


def check_continuity(clauses: list[dict]) -> list[str]:
    """
    检查条款号连续性：对每一节（前两段相同），相邻条款末段差值 > 3 视为警告。
    返回警告列表。
    """
    warnings = []
    # 按 chapter+section 分组
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for c in clauses:
        parts = _parse_no(c["clause_no"])
        if len(parts) >= 3:
            key = parts[:2]          # (章, 节)
            groups[key].append((parts, c["clause_no"]))

    for key, items in groups.items():
        items.sort(key=lambda x: x[0])
        prev_last = None
        for parts, no in items:
            last = parts[-1]
            if prev_last is not None and last - prev_last > 3:
                warnings.append(f"  跳跃: {key[0]}.{key[1]}.{prev_last} → {no}（差 {last - prev_last}）")
            prev_last = last
    return warnings


def check_tables(clauses: list[dict]) -> dict:
    """返回表格统计：总数/完整数（有</table>）/有 caption 数."""
    total = sum(len(c["tables"]) for c in clauses)
    complete = sum(
        1 for c in clauses for t in c["tables"]
        if "</table>" in t["html"].lower()
    )
    with_caption = sum(
        1 for c in clauses for t in c["tables"] if t["caption"].strip()
    )
    return {
        "total": total,
        "complete": complete,
        "complete_rate": complete / total if total else 1.0,
        "with_caption": with_caption,
        "caption_rate": with_caption / total if total else 1.0,
    }


# 条文说明的固有措辞。必须与正文的规范性引用区分开：
#   正文  「应按本节规定调整地震作用效应」——祈使、指向他处
#   说明  「本条规定了…的计算方法」「本条为…的基本表达式」——陈述、自我描述
# 故 `本条/本节规定` 必须带「了」，不能裸匹配。
_RE_COMMENTARY_TELL = re.compile(
    r'本[条节](?:规定了|明确了|参照了|沿用了|为|系)|试验研究表明|'
    r'(?:此|本)次修订|原规范|参考(?:了)?国外'
)


def check_commentary(clauses: list[dict]) -> int:
    """统计疑似「条文说明」而非正文的条款数。

    条文说明是解释条文来由的附录，与正文条款号完全重号，一旦混入即污染全库。
    判据取其固有措辞（正文是规定性语句，不会自称"本条规定了…"）。

    Args:
        clauses: 单本规范的条款记录列表

    Returns:
        命中条文说明特征词的条款数（正文库应为 0）
    """
    return sum(1 for c in clauses if _RE_COMMENTARY_TELL.search(c["text"][:120]))


def check_chapter_path(clauses: list[dict]) -> int:
    """统计 chapter_path 首层章号与条款号章号不一致的条款数。

    Args:
        clauses: 单本规范的条款记录列表

    Returns:
        不一致的条款数（正常应为 0）
    """
    bad = 0
    for c in clauses:
        path = c.get("chapter_path") or []
        if not path:
            bad += 1
            continue
        if path[0].split()[0].split(".")[0] != c["clause_no"].split(".")[0]:
            bad += 1
    return bad


def check_per_standard(all_clauses: list[dict]) -> list[dict]:
    by_std: dict[str, list[dict]] = defaultdict(list)
    for c in all_clauses:
        by_std[c["standard_code"]].append(c)

    results = []
    for std_code, clauses in sorted(by_std.items()):
        n = len(clauses)
        mandatory_n = sum(1 for c in clauses if c["is_mandatory"])
        tiny_n = sum(1 for c in clauses if c["char_len"] < 30)
        tiny_ratio = tiny_n / n if n else 0
        table_stat = check_tables(clauses)
        formula_clauses = sum(1 for c in clauses if c["formulas"])
        ref_clauses = sum(1 for c in clauses if c["refs"])
        continuity_warns = check_continuity(clauses)

        commentary_n = check_commentary(clauses)
        path_bad_n = check_chapter_path(clauses)

        # 硬门限：
        #   1. 强制性条文非零
        #   2. 表格 HTML 全部完整（无截断）
        #   3. 条文说明混入率 = 0    —— 历史 bug：分块器「保留最后出现的」使
        #      文末的条文说明附录整体覆盖正文，条文库退化为解释性文字、限值表全丢，
        #      而当时的门限（1+2）全部通过。此断言专为拦截该类回归。
        #   4. chapter_path 章号与条款号章号一致率 100%
        ok = (
            mandatory_n > 0
            and table_stat["complete_rate"] >= 1.0
            and commentary_n == 0
            and path_bad_n == 0
        )

        results.append({
            "std_code":           std_code,
            "std_name":           clauses[0]["standard_name"],
            "clause_count":       n,
            "mandatory_count":    mandatory_n,
            "tiny_count":         tiny_n,
            "table_total":        table_stat["total"],
            "table_complete":     table_stat["complete"],
            "table_caption_rate": table_stat["caption_rate"],
            "formula_clauses":    formula_clauses,
            "ref_clauses":        ref_clauses,
            "continuity_warns":   continuity_warns,
            "commentary_count":   commentary_n,
            "path_bad_count":     path_bad_n,
            "pass":               ok,
        })
    return results


def _fmt_rate(r: float) -> str:
    return f"{r*100:.1f}%"


def print_summary(results: list[dict]) -> None:
    print(f"{'规范':<18} {'条款':>5} {'强制':>4} {'极短':>4} {'表格/完整':>9} {'caption率':>9} {'公式':>5} {'引用':>5} {'说明混入':>8} {'路径错':>6} {'结论':>5}")
    print("-" * 96)
    all_pass = True
    for r in results:
        status = "OK" if r["pass"] else "FAIL"
        if not r["pass"]:
            all_pass = False
        tbl = f"{r['table_complete']}/{r['table_total']}"
        print(
            f"{r['std_code']:<18} {r['clause_count']:>5} {r['mandatory_count']:>4} "
            f"{r['tiny_count']:>4} {tbl:>9} {_fmt_rate(r['table_caption_rate']):>9} "
            f"{r['formula_clauses']:>5} {r['ref_clauses']:>5} "
            f"{r['commentary_count']:>8} {r['path_bad_count']:>6} {status:>5}"
        )
    print("-" * 96)
    total = sum(r["clause_count"] for r in results)
    print(f"合计: {total} 条, {'全部通过' if all_pass else '有未通过项目'}")


def write_report(results: list[dict], out_path: Path) -> None:
    lines = [
        "# 条文库解析质检报告",
        "",
        "| 规范 | 条款数 | 强制 | 极短(<30) | 表格总数 | caption率 | 公式条款 | 引用条款 | 说明混入 | 路径错 | 通过 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        pass_mark = "OK" if r["pass"] else "FAIL"
        lines.append(
            f"| {r['std_code']} {r['std_name']} "
            f"| {r['clause_count']} | {r['mandatory_count']} | {r['tiny_count']} "
            f"| {r['table_complete']}/{r['table_total']} | {_fmt_rate(r['table_caption_rate'])} "
            f"| {r['formula_clauses']} | {r['ref_clauses']} "
            f"| {r['commentary_count']} | {r['path_bad_count']} | {pass_mark} |"
        )

    lines += ["", "## 连续性警告"]
    has_warn = False
    for r in results:
        if r["continuity_warns"]:
            has_warn = True
            lines.append(f"\n### {r['std_code']}")
            lines.extend(r["continuity_warns"])
    if not has_warn:
        lines.append("\n无大跳跃。")

    lines += [
        "",
        "> **门限说明**：① 强制性条文数 > 0；② 表格 HTML 全部完整（complete = total）；",
        "> ③ **说明混入 = 0**；④ **路径错 = 0**。",
        "> caption 率仅供参考——续表（续表 X.X.X）无独立 caption 属正常设计，不纳入门限。",
        ">",
        "> ③④ 为 2026-07-27 新增，拦截历史 bug 的回归：分块器原按「同条款号保留最后出现的」去重，",
        "> 而规范 PDF 是「正文在前、条文说明在后」且两段完全重号，导致条文说明整体覆盖正文，",
        "> 全库退化为解释性文字、限值表格大量丢失；当时仅有 ①② 两条门限，全部规范「通过」。",
        "",
        "## 结论",
    ]
    all_pass = all(r["pass"] for r in results)
    if all_pass:
        lines.append("全部规范通过质检门限，可进入阶段 2 数据构造。")
    else:
        failed = [r["std_code"] for r in results if not r["pass"]]
        lines.append(f"以下规范未通过质检，需排查后再进阶段 2：{', '.join(failed)}")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入: {out_path}")


def main() -> None:
    if not CLAUSES_FILE.exists():
        print(f"[ERROR] 找不到 {CLAUSES_FILE}，请先运行 src/parse/clause_splitter.py", file=sys.stderr)
        sys.exit(1)

    clauses = _load(CLAUSES_FILE)
    print(f"加载 {len(clauses)} 条记录\n")

    results = check_per_standard(clauses)
    print_summary(results)
    write_report(results, REPORT_FILE)


if __name__ == "__main__":
    main()
