"""真实工程数据 xlsx → 清单匹配评测 gold（`match_gold.jsonl`）。

输入是真实工程的「BIM 构件↔清单」关联表（列 CODE/NAME/FEATURE/COMP_NAME/.../WORK_VALUE）。
**版本隔离**：xlsx 是 2013 版清单编码，gold 完全对 2013 版国标（`bill_spec` of GB-50854-2013，房建计量规范）验证，
不做 2013→2024 桥接（见 ce-code TODO 决策）。

做法：
  - 每行清单码取前 9 位（去 3 位项目自编号）；按 (code9, FEATURE) 去重（同清单多构件实例的量行折叠为一条）。
  - 查询 = COMP_NAME + FEATURE（**不含清单 NAME**，避免「查询=答案名」的循环；FEATURE 是真实项目特征）。
  - gold = [code9]；**只保留 code9 在 2013 bill_spec 内的**（不在索引里的码无法用检索验证，单列 uncovered 报告）。
  - 脱敏：只出 query + gold + note(NAME)；丢 WORK_VALUE/GROUP_NUM/项目标识等敏感+无关字段。

用法（服务器，从 ce-code 根；需先抽出 2013 bill_spec.jsonl）：
  uv run python -m tools.build_match_gold \
    --xlsx data/raw/清单量关联实物量数据.xlsx \
    --bill-spec data/structured/cost/GB-50854-2013/bill_spec.jsonl \
    --out data/eval_set/match_gold_2013.jsonl
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_WS = re.compile(r"\s+")


def code9(raw: str) -> str:
    """清单编码取前 9 位规范码（去掉 3 位项目自编顺序号）。纯函数。

    参数：raw —— xlsx CODE（常为 12 位，如 "010202007001"）。
    返回：前 9 位（"010202007"）；不足 9 位原样返回（去空白）。
    """
    s = re.sub(r"\D", "", str(raw or ""))
    return s[:9] if len(s) >= 9 else s


def clean_feature(text: str) -> str:
    """项目特征文本归一：折叠空白/换行为单空格、去首尾引号空白。纯函数。

    参数：text —— FEATURE 原文（多行带 (1)(2)… 编号）。
    返回：单行紧凑文本（嵌入端再按 480 字截断）。
    """
    return _WS.sub(" ", str(text or "").replace("　", " ")).strip().strip('"').strip()


def build_query(comp_name: str, feature: str) -> str:
    """拼 gold 查询文本 = 构件名 + 项目特征（不含清单 NAME，避免循环）。纯函数。

    参数：comp_name —— COMP_NAME（构件名）；feature —— FEATURE 原文。
    返回：``"{comp_name}；{cleaned_feature}"``，缺项自然省略。
    """
    parts = [p for p in (str(comp_name or "").strip(), clean_feature(feature)) if p]
    return "；".join(parts)


def load_valid_codes(bill_spec_path: Path) -> set[str]:
    """读 2013 bill_spec.jsonl，返回有效 9 位清单码集合（gold 覆盖过滤用）。

    参数：bill_spec_path —— bill_spec.jsonl 路径。
    返回：set[str]，库内全部 9 位 code。
    """
    codes: set[str] = set()
    for line in bill_spec_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            codes.add(code9(json.loads(line).get("code", "")))
    return codes


def rows_to_gold(rows: list[dict], valid_codes: set[str]) -> tuple[list[dict], list[dict]]:
    """xlsx 行 → (gold 列表, uncovered 列表)。去重 + 覆盖过滤（纯函数，便于单测）。

    参数：
        rows —— xlsx 行 dict，需含键 CODE/NAME/FEATURE/COMP_NAME。
        valid_codes —— 2013 bill_spec 的有效 9 位码集合。
    返回：
        (gold, uncovered)。gold 每项 ``{query, gold:[code9], note:NAME}``，按 (code9,FEATURE) 去重；
        code9 不在 valid_codes 的进 uncovered（``{code9, name}`` 去重），不进 gold。
    """
    seen: set[tuple[str, str]] = set()
    seen_uncov: set[str] = set()
    gold: list[dict] = []
    uncovered: list[dict] = []
    for r in rows:
        c9 = code9(r.get("CODE", ""))
        feat = clean_feature(r.get("FEATURE", ""))
        if not c9:
            continue
        key = (c9, feat)
        if key in seen:
            continue
        seen.add(key)
        if c9 not in valid_codes:
            if c9 not in seen_uncov:
                seen_uncov.add(c9)
                uncovered.append({"code9": c9, "name": str(r.get("NAME", "")).strip()})
            continue
        gold.append({
            "query": build_query(r.get("COMP_NAME", ""), r.get("FEATURE", "")),
            "gold": [c9],
            "note": str(r.get("NAME", "")).strip(),
        })
    return gold, uncovered


def _read_xlsx(path: Path) -> list[dict]:
    """读 xlsx 首个 sheet → list[dict]（首行表头为键）。openpyxl 在此 lazy import（服务器装）。"""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    return [dict(zip(header, row)) for row in it]


def _cli():
    """构造 click 命令（click/rich lazy import，纯函数模块顶层无依赖、本地可测）。"""
    import click
    from rich.console import Console

    console = Console()

    @click.command()
    @click.option("--xlsx", "xlsx_path", required=True, type=click.Path(exists=True, path_type=Path),
                  help="真实工程 xlsx（构件↔清单关联）")
    @click.option("--bill-spec", "bill_spec_path", required=True, type=click.Path(exists=True, path_type=Path),
                  help="2013 bill_spec.jsonl（覆盖过滤用）")
    @click.option("--out", "out_path", default="data/eval_set/match_gold_2013.jsonl", type=Path,
                  help="输出 gold jsonl（相对 ce-code 根）")
    def main(xlsx_path: Path, bill_spec_path: Path, out_path: Path) -> None:
        """xlsx → match_gold_2013.jsonl（脱敏 + 覆盖过滤 + 覆盖率报告）。"""
        valid = load_valid_codes(bill_spec_path)
        rows = _read_xlsx(xlsx_path)
        gold, uncovered = rows_to_gold(rows, valid)
        out = ROOT / out_path if not out_path.is_absolute() else out_path
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for g in gold:
                f.write(json.dumps(g, ensure_ascii=False) + "\n")
        uncov_path = out.with_name(out.stem + "_uncovered.jsonl")
        with uncov_path.open("w", encoding="utf-8") as f:
            for u in uncovered:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        total_codes = len({g["gold"][0] for g in gold}) + len(uncovered)
        console.print(f"xlsx 行 {len(rows)} → gold {len(gold)} 条（唯一码 "
                      f"{len({g['gold'][0] for g in gold})}）· uncovered 码 {len(uncovered)}")
        console.print(f"覆盖率（唯一码在 2013 库内）：{len({g['gold'][0] for g in gold})}/{total_codes}")
        console.print(f"[green]✓ {out}[/green]  uncovered → {uncov_path}")
        console.print("[dim]gold 已脱敏（仅 query+gold+note）；xlsx 原件勿入 git。[/dim]")

    return main


if __name__ == "__main__":
    _cli()()
