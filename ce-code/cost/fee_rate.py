"""Phase C · 费率库：从深圳市建设工程计价费率标准（2023）抽 `fee_rate`。

费率是「综合单价之上算工程造价」的乘数：安全文明施工措施费 / 夜间施工增加费 /
赶工措施费 / 总承包服务费 / 增值税 / 附加税费 / 工程保险费的**参考范围 + 推荐值**。

源是 MinerU 解析的费率标准 `chunks.json`，7 张费率表表头**各不相同**（专业工程/工程
类别/费用名称\\系数/项目名称…，单位 %/‰/系数混用）。表少而杂，故用**声明式规则**
（`RULES`：按 caption 锚定每表的列布局 + 费用元数据），而非通用列检测——caption 不中
任何规则即跳过并计数（**宁缺毋造**，不猜列）。安文费清单部分列项表 / 附录 B 包含内容
等非费率表不在此处理。

输出：``data/structured/cost/SZ-FLBZ-2023/fee_rate.jsonl``（per-doc），每行：
fee_category / fee_name / applicable（可空）/ ref_low / ref_high / recommended / unit
+ doc_id / spec_version / region / effective_priority / provenance。

跑法（单行）：
  uv run python -m cost.fee_rate --input "data/structured/chunks/深圳市建设工程计价费率标准2023/default/chunks.json"
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from cost import resolve_doc_dir

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = ROOT / "data" / "structured" / "cost"

DOC_ID = "SZ-FLBZ-2023"
SPEC_VERSION = "深圳市建设工程计价费率标准 2023"
REGION = "深圳"

# 参考范围 "a~b"（兼容 ~ ～ － - 分隔）
_RANGE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[~～\-－]\s*(-?\d+(?:\.\d+)?)\s*$")

# 声明式规则：每表按 caption 子串锚定列布局。col 为 None 表示该表无此列。
# name_col=None → fee_name 取 fee_category；rec_col=None → 无推荐值（仅参考范围）；
# range_col=None → 无参考范围（仅单一费率，如附加税费的税率）。
RULES = [
    {"match": "安全文明施工措施费费率部分费率表", "fee_category": "安全文明施工措施费",
     "name_col": None, "applicable_col": 0, "range_col": 1, "rec_col": 2, "unit": "%"},
    {"match": "夜间施工增加费系数", "fee_category": "夜间施工增加费",
     "name_col": 0, "applicable_col": None, "range_col": 1, "rec_col": 2, "unit": "系数"},
    {"match": "赶工措施费系数", "fee_category": "赶工措施费",
     "name_col": 0, "applicable_col": 1, "range_col": 2, "rec_col": 3, "unit": "系数"},
    {"match": "总承包服务费费率", "fee_category": "总承包服务费",
     "name_col": 0, "applicable_col": 1, "range_col": 2, "rec_col": 3, "unit": "%"},
    {"match": "增值税综合应纳税费率", "fee_category": "增值税",
     "name_col": None, "applicable_col": None, "range_col": 0, "rec_col": 1, "unit": "%"},
    {"match": "城市维护建设税", "fee_category": "附加税费",
     "name_col": 0, "applicable_col": None, "range_col": None, "rec_col": 1, "unit": "%"},
    {"match": "工程保险费费率", "fee_category": "工程保险费",
     "name_col": None, "applicable_col": 0, "range_col": 1, "rec_col": None, "unit": "‰"},
]


def _match_rule(caption: str) -> dict | None:
    """按 caption 子串选规则；不中返回 None（不猜列）。"""
    cap = caption or ""
    for r in RULES:
        if r["match"] in cap:
            return r
    return None


def _num(v) -> float | None:
    """单值费率文本 → float；非数字返回 None。"""
    s = re.sub(r"[,\s%‰]", "", str(v or "")).strip()
    if not s or not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return float(s)


def _range(v) -> tuple[float | None, float | None]:
    """参考范围 "a~b" → (low, high)；不匹配返回 (None, None)。"""
    m = _RANGE_RE.match(str(v or ""))
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def extract(chunks: list[dict]) -> tuple[list[dict], dict]:
    """遍历 chunks，按 RULES 抽费率行 + 收集 report 统计。

    参数：chunks —— 费率标准 chunks.json 节点列表。
    返回：(records, stats)。
    """
    records: list[dict] = []
    stats = {"rate_tables": 0, "skipped_tables": 0, "anomalies": []}

    for chunk in chunks:
        for table in chunk.get("tables") or []:
            body = table.get("body") or []
            if not body:
                continue
            caption = table.get("caption") or chunk.get("title") or ""
            rule = _match_rule(caption)
            if rule is None:
                stats["skipped_tables"] += 1
                continue
            stats["rate_tables"] += 1
            page = table.get("page")

            for row in body[1:]:  # 跳表头
                def cell(i):
                    return str(row[i]).strip() if (i is not None and i < len(row)) else ""

                name = cell(rule["name_col"]) if rule["name_col"] is not None \
                    else rule["fee_category"]
                if not name:  # 名称列空 → 残行，跳过（宁缺毋造）
                    continue
                applicable = cell(rule["applicable_col"]) or None \
                    if rule["applicable_col"] is not None else None
                ref_low, ref_high = _range(cell(rule["range_col"])) \
                    if rule["range_col"] is not None else (None, None)
                recommended = _num(cell(rule["rec_col"])) \
                    if rule["rec_col"] is not None else None

                # 该有值却全空 → 记异常，不杜撰
                if ref_low is None and recommended is None:
                    stats["anomalies"].append({"caption": caption, "row": row})
                    continue

                records.append({
                    "fee_category": rule["fee_category"],
                    "fee_name": name,
                    "applicable": applicable,
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                    "recommended": recommended,
                    "unit": rule["unit"],
                    "doc_id": DOC_ID,
                    "spec_version": SPEC_VERSION,
                    "region": REGION,
                    "effective_priority": 1,
                    "provenance": {"page": page, "caption": caption},
                })

    return records, stats


def report(records: list[dict], stats: dict) -> None:
    """打印质量 report（表计数 / 大类分布 / 异常）。"""
    t = Table(title="费率抽取总览", show_header=False)
    t.add_row("费率表 / 跳过表", f"{stats['rate_tables']} / {stats['skipped_tables']}")
    t.add_row("费率行 records", str(len(records)))
    t.add_row("异常行（弃）", str(len(stats["anomalies"])))
    console.print(t)
    by_cat: dict[str, int] = {}
    for r in records:
        by_cat[r["fee_category"]] = by_cat.get(r["fee_category"], 0) + 1
    console.print(f"大类分布：{by_cat}")
    for a in stats["anomalies"][:5]:
        console.print(f"[yellow]异常[/]：{a['caption']} | {a['row']}")


def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（一行一条，UTF-8 不转义）。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@click.command()
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path),
              required=True, help="费率标准 chunks.json 路径")
@click.option("--outdir", type=click.Path(path_type=Path), default=DEFAULT_OUTDIR,
              show_default=True, help="fee_rate.jsonl 输出根（按 doc_id 分目录）")
@click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
def main(input_path: Path, outdir: Path, dry_run: bool) -> None:
    """费率标准 chunks.json → fee_rate.jsonl（费率参考范围 + 推荐值）+ 质量 report。"""
    chunks = json.load(open(input_path, encoding="utf-8"))
    records, stats = extract(chunks)
    report(records, stats)
    if dry_run:
        console.print("[dim]--dry-run：未落盘[/]")
        return
    if not records:
        raise click.ClickException("未抽到任何费率行：检查 caption 是否匹配 RULES")
    doc_dir = resolve_doc_dir(outdir, records)  # data/structured/cost/SZ-FLBZ-2023/
    doc_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(records, doc_dir / "fee_rate.jsonl")
    console.print(f"[green]已写[/] {doc_dir}/fee_rate.jsonl（{len(records)} 行）")


if __name__ == "__main__":
    main()
