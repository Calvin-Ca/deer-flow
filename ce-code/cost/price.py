"""Phase C · 价格库：从深圳信息价（月刊）抽 `resource_price`（动态独立管道）。

信息价是**动态数据**（月更），与定额/规范的静态口径解耦：`resource_price` 带
`effective_period` 时效，按 region + 期取价，不参与 `effective_priority` 排序。

源是 MinerU 解析的信息价 PDF（`chunks.json`），价目表形如：

    序号 | 材料名称 | 型号、规格 | 单位 | 价格(元)
    一、黑色及有色金属  ← 分类行（序号列是中文数字标题、其余空），记为 sub_category
    23  | 热轧带肋钢筋HRB500E | Φ20 | t | 3913.00  ← 物料行

表头有几种变体（材料/设备/项目名称），用**列名子串定位**（不假定固定列序）：
名称列（含「名称」）→ 设备名称=机械 / 项目名称=人工 / 否则=材料；型号·规格列→spec；
「单位」列→unit；含「价格」+「元」且非「公式」的列→price。据此天然排除：
价格指数（月份列、无价格列）/ 造价对比（BIM）/ 系数表 / 混凝土公式价（价格计算公式(元)）。

输出：``data/structured/SZ-JGXX-PRICE/resource_price.jsonl``（per-doc），每行带物料
自然键（category/name/spec/unit，供 ``load_pg`` upsert 进 `resource` 取 id）+ price +
effective_period + region + price_type + doc_id + provenance。

> 物料↔定额 resource 的对接（按自然键合并）是**富化**步：信息价名称（如「建筑废渣
> 混凝土实心砖 240x115x53」）与定额名（「普通混凝土实心砖 240×115×53」）格式有差，
> 精确命中有限，语义匹配待后续——P0 先把价目结构化入库，按红线「只建议不定稿」。

跑法（单行）：
  uv run python -m cost.price --input "data/structured/2026_5深圳信息价_www_zgjct_com下载/default/chunks.json"
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from cost import resolve_doc_dir

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = ROOT / "data" / "structured"

DOC_ID = "SZ-JGXX-PRICE"
REGION = "深圳"
PRICE_TYPE = "信息价"

# 单位归一（信息价排版字形 / LaTeX → 定额约定，提高与 resource 自然键的命中率）
_UNIT_NORM = {"㎡": "m²", "㎥": "m³", "m2": "m²", "m3": "m³",
              "$m^2$": "m²", "$m^3$": "m³", "M³": "m³", "M²": "m²"}

# 分类行的序号列形如「一、黑色及有色金属」（中文数字 + 、）
_SUBCAT_RE = re.compile(r"^[一二三四五六七八九十]+[、.]")


def _num(v) -> float | None:
    """价格文本 → float（去千分位逗号/空格）；非数字返回 None。"""
    if v is None:
        return None
    s = re.sub(r"[,\s]", "", str(v)).strip()
    if not s or not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return float(s)


def _norm_unit(u: str) -> str:
    """单位归一（去 LaTeX/字形差异）。"""
    u = (u or "").strip()
    return _UNIT_NORM.get(u, u)


def normalize_price_doc(standard_id: str) -> tuple[str, str, str]:
    """信息价 standard_id → (doc_id, spec_version, period_start_iso)。

    从「2026-5深圳信息价…」抽年月，spec_version=「深圳信息价 2026-05」、
    时效起=当月 1 日；抽不到年月时 spec_version 退化为原串、period 留 None。
    参数：standard_id —— chunks 的 standard_id。返回：三元组。
    """
    m = re.search(r"(\d{4})[-年]\s*(\d{1,2})", standard_id or "")
    if not m:
        return DOC_ID, (standard_id or "深圳信息价"), ""
    y, mo = int(m.group(1)), int(m.group(2))
    return DOC_ID, f"深圳信息价 {y}-{mo:02d}", f"{y}-{mo:02d}-01"


def _period(start_iso: str) -> tuple[str, str]:
    """当月起始日 → 时效区间 [当月1日, 次月1日)（半开，对齐 DATERANGE）。

    参数：start_iso —— 'YYYY-MM-01'。返回：(start, end) ISO 日期串。
    """
    if not start_iso:
        return "", ""
    y, mo, _ = (int(x) for x in start_iso.split("-"))
    end = date(y + 1, 1, 1) if mo == 12 else date(y, mo + 1, 1)
    return start_iso, end.isoformat()


# 名称列表头 → resource 大类
_NAME_CAT = {"设备名称": "机械", "项目名称": "人工"}


def _resolve_cols(header: list[str]) -> dict | None:
    """按列名子串定位价目表列下标；非价目表（无价格列/无名称列）返回 None。

    参数：header —— 表首行（已 strip）。返回：{name,spec,unit,price,category} 或 None。
    """
    name_i = spec_i = unit_i = price_i = None
    category = "材料"
    for i, h in enumerate(header):
        h = h.strip()
        if price_i is None and "价格" in h and "元" in h and "公式" not in h:
            price_i = i
        elif name_i is None and "名称" in h:
            name_i = i
            category = _NAME_CAT.get(h, "材料")
        elif spec_i is None and ("型号" in h or "规格" in h):
            spec_i = i
        elif unit_i is None and h == "单位":
            unit_i = i
    if name_i is None or price_i is None or unit_i is None:
        return None  # 不是物料价目表（指数表/系数表/公式价表/对比表）
    return {"name": name_i, "spec": spec_i, "unit": unit_i,
            "price": price_i, "category": category}


def extract(chunks: list[dict]) -> tuple[list[dict], dict]:
    """遍历 chunks 抽价目行 + 收集 report 统计。

    参数：chunks —— 信息价 chunks.json 节点列表。
    返回：(records, stats)；records 含物料自然键 + price + 时效 + 溯源。
    """
    records: list[dict] = []
    stats = {"price_tables": 0, "skipped_tables": 0, "subcats": set(),
             "bad_price": 0, "by_cat": {}}
    doc_id = spec_version = ""
    period_start = ""

    for chunk in chunks:
        if not doc_id:
            doc_id, spec_version, period_start = normalize_price_doc(
                chunk.get("standard_id") or "")
        for table in chunk.get("tables") or []:
            body = table.get("body") or []
            if not body:
                continue
            header = [str(c).strip() for c in body[0]]
            col = _resolve_cols(header)
            if col is None:
                stats["skipped_tables"] += 1
                continue
            stats["price_tables"] += 1
            sub_category = None
            page = table.get("page")
            caption = table.get("caption") or chunk.get("title")
            for row in body[1:]:
                def cell(i):
                    return str(row[i]).strip() if (i is not None and i < len(row)) else ""
                name = cell(col["name"])
                price = _num(cell(col["price"]))
                first = cell(0)
                # 分类行：价格空 + 名称空 + 序号列是中文数字标题
                if price is None and not name:
                    if first and _SUBCAT_RE.match(first):
                        sub_category = first
                        stats["subcats"].add(first)
                    continue
                if price is None:
                    stats["bad_price"] += 1
                    continue
                if not name:
                    continue
                cat = col["category"]
                records.append({
                    "category": cat,
                    "name": name,
                    "spec": cell(col["spec"]) or None,
                    "unit": _norm_unit(cell(col["unit"])),
                    "price": price,
                    "sub_category": sub_category,
                    "region": REGION,
                    "price_type": PRICE_TYPE,
                    "effective_start": _period(period_start)[0],
                    "effective_end": _period(period_start)[1],
                    "doc_id": doc_id,
                    "spec_version": spec_version,
                    "provenance": {"page": page, "caption": caption},
                })
                stats["by_cat"][cat] = stats["by_cat"].get(cat, 0) + 1

    return records, stats


def _dedup(records: list[dict]) -> tuple[list[dict], int]:
    """同一物料同期多价去重（按 (category,name,spec,unit) 保首条）。

    resource_price 的 EXCLUDE 约束禁止同资源同期重叠，故落盘前先去重。
    参数：records —— extract 产出。返回：(去重后, 丢弃数)。
    """
    seen, out = set(), []
    for r in records:
        key = (r["category"], r["name"], r["spec"], r["unit"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out, len(records) - len(out)


def report(records: list[dict], stats: dict, dropped: int) -> None:
    """打印质量 report（表分类计数 / 大类分布 / 价格区间 / 去重）。"""
    t = Table(title="信息价抽取总览", show_header=False)
    t.add_row("价目表 / 跳过表", f"{stats['price_tables']} / {stats['skipped_tables']}")
    t.add_row("价目行 records", str(len(records)))
    t.add_row("分类（sub_category）", str(len(stats["subcats"])))
    t.add_row("同期多价去重丢弃", str(dropped))
    t.add_row("价格非数字跳过", str(stats["bad_price"]))
    console.print(t)
    console.print(f"大类分布：{stats['by_cat']}")
    prices = [r["price"] for r in records]
    if prices:
        console.print(f"价格区间：{min(prices)} ~ {max(prices)} 元")
    if records:
        console.print(f"时效：{records[0]['effective_start']} ~ {records[0]['effective_end']}"
                      f"（doc_id={records[0]['doc_id']}）")


def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（一行一条，UTF-8 不转义）。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@click.command()
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path),
              required=True, help="信息价 chunks.json 路径")
@click.option("--outdir", type=click.Path(path_type=Path), default=DEFAULT_OUTDIR,
              show_default=True, help="resource_price.jsonl 输出根（按 doc_id 分目录）")
@click.option("--period", "period_override", default="",
              help="覆盖时效起始月 YYYY-MM（默认从 standard_id 推断）")
@click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
def main(input_path: Path, outdir: Path, period_override: str, dry_run: bool) -> None:
    """信息价 chunks.json → resource_price.jsonl（物料月度价 + 时效）+ 质量 report。"""
    chunks = json.load(open(input_path, encoding="utf-8"))
    records, stats = extract(chunks)
    if period_override:  # 显式覆盖时效（standard_id 缺年月时）
        s, e = _period(f"{period_override}-01")
        for r in records:
            r["effective_start"], r["effective_end"] = s, e
    if records and not records[0]["effective_start"]:
        raise click.ClickException(
            "无法确定时效期：standard_id 未含年月，请用 --period YYYY-MM 指定")
    records, dropped = _dedup(records)
    report(records, stats, dropped)
    if dry_run:
        console.print("[dim]--dry-run：未落盘[/]")
        return
    doc_dir = resolve_doc_dir(outdir, records)  # data/structured/SZ-JGXX-PRICE/
    doc_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(records, doc_dir / "resource_price.jsonl")
    console.print(f"[green]已写[/] {doc_dir}/resource_price.jsonl（{len(records)} 行）")


if __name__ == "__main__":
    main()
