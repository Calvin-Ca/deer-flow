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


def _parse_no(clause_no: str) -> tuple[int, ...] | None:
    """把 '3.1.7' 拆成 (3, 1, 7)。

    Args:
        clause_no: 条款号，如 "3.1.7"；附录条款为字母开头（"A.0.1"）

    Returns:
        各段整数元组；字母编号（附录）返回 None——附录不参与正文连续性检查
    """
    try:
        return tuple(int(x) for x in clause_no.split("."))
    except ValueError:
        return None


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
        if parts and len(parts) >= 3:
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


_RE_EMBEDDED_CLAUSE = re.compile(
    r'^#{0,3}\s*((?:[A-Z]\.\d+(?:\.\d+)*|\d+\.\d+\.\d+(?:\.\d+)?))\s*\S',
    re.MULTILINE,
)


def check_leak(clauses: list[dict]) -> int:
    """统计正文里混入了**其他条款**的条款数（切分渗漏）。

    比「char_len 超阈值」精确得多：附录里合法存在数万字的大表（如附加应力系数表、
    全国城镇烈度表），单纯按长度报警会误伤；而正文中出现另一个条款号独占一行，
    只可能是没切开。历史案例：附录整体灌进每本的末条（GB50009_10.3.3 达 17.5 万字）、
    E.5 整节灌进 E.4.3。

    Args:
        clauses: 单本规范的条款记录列表

    Returns:
        正文含其他条款号的条款数（正常应为 0）
    """
    bad = 0
    for c in clauses:
        # 首行是自身编号，从第二行起找
        body = c["text"].split("\n", 1)[1] if "\n" in c["text"] else ""
        for m in _RE_EMBEDDED_CLAUSE.finditer(body):
            if m.group(1) != c["clause_no"]:
                bad += 1
                break
    return bad


def check_table_capture(clauses: list[dict], std_code: str) -> tuple[int, int]:
    """比对源 md 与条文库的表格数，量化切分过程中的内容丢失。

    这是最直接的「内容有没有丢」断言：条款数、字数都可能因切分策略变化而波动，
    但源文档里的 <table> 数量是客观不变的，捕获率必须 100%。
    历史案例：让章节标题关闭当前条款后，附录标题直下、没有编号的表格无处安放，
    静默丢失 23 张（GB50009 附录A「常用材料和构件的自重」整章只有表、无条款号）。

    Args:
        clauses:  单本规范的条款记录列表
        std_code: 标准号，用于定位源 md

    Returns:
        (源 md 正文中的表格数, 条文库中捕获的表格数)；源文件缺失时返回 (0, 0)
    """
    from src.parse.clause_splitter import STANDARDS, _find_md, _find_commentary_start

    meta = STANDARDS.get(std_code)
    parsed_root = CLAUSES_FILE.parent / "parsed"
    md = _find_md(parsed_root, meta["file_kw"]) if meta and parsed_root.exists() else None
    if not md:
        return 0, 0
    lines = md.read_text(encoding="utf-8").split("\n")
    body = "\n".join(lines[:_find_commentary_start(lines)])
    return len(re.findall(r"<table", body, re.I)), sum(len(c["tables"]) for c in clauses)


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
        head = c["clause_no"].split(".")[0]
        if not head.isdigit():
            # 附录条款：路径应为「附录X …」且字母与编号一致
            if not path[0].startswith(f"附录{head}"):
                bad += 1
            continue
        if path[0].split()[0].split(".")[0] != head:
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
        leak_n = check_leak(clauses)
        src_tables, got_tables = check_table_capture(clauses, std_code)

        # 硬门限：
        #   1. 强制性条文非零
        #   2. 表格 HTML 全部完整（无截断）
        #   3. 条文说明混入率 = 0    —— 历史 bug：分块器「保留最后出现的」使
        #      文末的条文说明附录整体覆盖正文，条文库退化为解释性文字、限值表全丢，
        #      而当时的门限（1+2）全部通过。此断言专为拦截该类回归。
        #   4. chapter_path 章号与条款号章号一致率 100%
        #   5. 切分渗漏 = 0    —— 历史 bug：附录用字母编号（A.0.1）、正文条款被 MinerU
        #      排成标题（## 3.5.3），两者都匹配不上原条款正则，其内容一路灌进上一条，
        #      使每本末条膨胀到十几万字（GB50009_10.3.3 达 17.5 万字，占全库 43% 文本）。
        #   6. 表格捕获率 = 100%  —— 最直接的「内容有没有丢」断言：条款数与字数会随
        #      切分策略波动，但源 md 的 <table> 数是客观不变量。
        ok = (
            mandatory_n > 0
            and table_stat["complete_rate"] >= 1.0
            and commentary_n == 0
            and path_bad_n == 0
            and leak_n == 0
            and (src_tables == 0 or got_tables >= src_tables)
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
            "leak_count":         leak_n,
            "src_tables":         src_tables,
            "got_tables":         got_tables,
            "pass":               ok,
        })
    return results


def _fmt_rate(r: float) -> str:
    return f"{r*100:.1f}%"


def print_summary(results: list[dict]) -> None:
    print(f"{'规范':<18} {'条款':>5} {'强制':>4} {'极短':>4} {'表格/完整':>9} {'caption率':>9} {'公式':>5} {'引用':>5} {'说明混入':>8} {'路径错':>6} {'渗漏':>5} {'表捕获':>8} {'结论':>5}")
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
            f"{r['commentary_count']:>8} {r['path_bad_count']:>6} {r['leak_count']:>5} "
            f"{r['got_tables']}/{r['src_tables']:<6} {status:>5}"
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
            f"| {r['commentary_count']} | {r['path_bad_count']} | {r['leak_count']} "
            f"| {r['got_tables']}/{r['src_tables']} | {pass_mark} |"
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
        "> ③ **说明混入 = 0**；④ **路径错 = 0**；⑤ **切分渗漏 = 0**；⑥ **表格捕获率 = 100%**。",
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
