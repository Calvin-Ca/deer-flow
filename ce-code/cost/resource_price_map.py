"""Phase C · 资源对齐：生成「定额资源 → 信息价物料」价格映射（拉高 /price/compose 价覆盖）。

定额 resource 与信息价物料同物异名（规格嵌名/分离、×↔x、单位 千块↔块 等系统性差异），
精确匹配命中率极低。本 matcher 用 ``resource_norm`` 的**确定性归一键** + 单位换算对齐两侧，
产 ``resource_price_map``：定额 resource → 信息价 resource（带 unit_factor，amount=含量×单价×factor）。

> **不上 embedding**：实测信息价（~152 种常用大宗料）未登的定额材料约占 90%（干混砂浆/电焊条/
> 料石毛石铁钉等专项小料 → 走市场价/询价），缺口是「缺失」非「同义」，语义匹配几乎零收益。归一键
> 命中的是**确定匹配**（非猜测）→ confidence 1.0 可定稿；未命中归 ``no_source``，红线交 HITL 询价。

输入：扫 ``data/structured/<doc_id>/resource.jsonl``（定额，仅 材料/机械；人工费单位元不走信息价）
      + ``SZ-JGXX-PRICE/resource_price.jsonl``（信息价物料）。
输出：``data/structured/resource_price_map.jsonl``（跨规范关系产物，扁平）+ 覆盖 report。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from cost.resource_norm import canonical_key, unit_factor

console = Console()

ROOT = Path(__file__).resolve().parent.parent
STRUCT = ROOT / "data" / "structured"

# 走信息价取价的资源类别（人工费单位为元、是成本不是量，不走信息价单价）
_PRICED_CATEGORIES = {"材料", "机械"}


def _read_jsonl(path: Path) -> list[dict]:
    """读 JSONL。

    参数：path —— 文件路径。
    返回：dict 列表（空行跳过）。
    """
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（UTF-8 不转义）。

    参数：records —— 记录；path —— 路径。
    返回：无。
    """
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _nat(r: dict) -> dict:
    """取资源自然键（category/name/spec/unit）——loader 据此解析 resource_id。

    参数：r —— resource 记录。
    返回：仅含自然键四字段的 dict。
    """
    return {"category": r.get("category"), "name": r.get("name"),
            "spec": r.get("spec"), "unit": r.get("unit")}


def match(quota_res: list[dict], price_res: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """归一键 + 单位换算对齐定额资源 ↔ 信息价物料。

    参数：
      quota_res —— 定额资源列表（含 材料/机械/人工）。
      price_res —— 信息价物料列表。
    返回：(映射行列表, 三态计数 dict)。三态：matched（命中+单位可换算）/ unit_bad（名规命中但单位
          不可换算，不猜价）/ no_source（信息价无此料）。映射行带 quota/price 自然键 + unit_factor +
          confidence + method。
    """
    pidx: dict[str, list[dict]] = defaultdict(list)
    for r in price_res:
        pidx[canonical_key(r.get("name"), r.get("spec"))].append(r)

    seen: set[tuple] = set()
    rows: list[dict] = []
    stats = {"matched": 0, "unit_bad": 0, "no_source": 0, "skipped_labor": 0}
    for q in quota_res:
        if q.get("category") not in _PRICED_CATEGORIES:
            stats["skipped_labor"] += 1
            continue
        key = (q.get("name"), q.get("spec"), q.get("unit"))
        if key in seen:
            continue
        seen.add(key)
        cands = pidx.get(canonical_key(q.get("name"), q.get("spec")))
        if not cands:
            stats["no_source"] += 1
            continue
        pr = cands[0]                                   # 同归一键通常唯一（规格已并入键）
        factor = unit_factor(q.get("unit"), pr.get("unit"))
        if factor is None:                              # 名规命中但单位不可换算 → 不猜价（红线）
            stats["unit_bad"] += 1
            continue
        stats["matched"] += 1
        rows.append({
            "quota_resource": _nat(q),
            "price_resource": _nat(pr),
            "unit_factor": factor,
            "confidence": 1.0,                          # 归一键精确命中 = 确定匹配，非猜测
            "method": "rule_canon_exact",
            "note": f"{q.get('name')}〔{q.get('unit')}〕→ {pr.get('name')} {pr.get('spec') or ''}〔{pr.get('unit')}〕×{factor:g}",
        })
    return rows, stats


def report(rows: list[dict], stats: dict[str, int]) -> None:
    """打印三态覆盖 report。

    参数：rows —— 映射行；stats —— 三态计数。
    返回：无（终端打印）。
    """
    t = Table(title="定额资源 → 信息价 映射（规则归一）", show_header=False)
    t.add_row("映射边（confidence 1.0）", str(len(rows)))
    t.add_row("matched（命中+单位可换算）", str(stats["matched"]))
    t.add_row("unit_bad（名规命中·单位不可换算·不猜价）", str(stats["unit_bad"]))
    t.add_row("no_source（信息价无此料→HITL 询价）", str(stats["no_source"]))
    t.add_row("skipped（人工费等非信息价类）", str(stats["skipped_labor"]))
    factored = sum(1 for r in rows if r["unit_factor"] != 1.0)
    t.add_row("含单位换算的边", str(factored))
    console.print(t)


@click.command()
@click.option("--struct-dir", "struct_dir", type=click.Path(exists=True, path_type=Path),
              default=STRUCT, show_default=True,
              help="结构化产物根；扫 <doc_id>/resource.jsonl + SZ-JGXX-PRICE/resource_price.jsonl")
@click.option("--outdir", type=click.Path(path_type=Path), default=STRUCT, show_default=True)
@click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
def main(struct_dir: Path, outdir: Path, dry_run: bool) -> None:
    """定额 resource + 信息价 → resource_price_map.jsonl（规则归一对齐）。"""
    quota_res: list[dict] = []
    for path in sorted(struct_dir.glob("*/resource.jsonl")):
        quota_res.extend(_read_jsonl(path))
    price_paths = sorted(struct_dir.glob("*/resource_price.jsonl"))
    price_res: list[dict] = []
    for path in price_paths:
        price_res.extend(_read_jsonl(path))
    if not quota_res or not price_res:
        raise click.ClickException(
            f"未扫到 resource/resource_price（quota={len(quota_res)} price={len(price_res)}）："
            f"确认 {struct_dir}/<doc_id>/ 下已生成 per-doc 产物")
    rows, stats = match(quota_res, price_res)
    report(rows, stats)
    if dry_run:
        console.print("[dim]--dry-run：未落盘[/]")
        return
    outdir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(rows, outdir / "resource_price_map.jsonl")     # 跨规范关系产物，保持扁平
    console.print(f"[green]已写[/] {outdir/'resource_price_map.jsonl'}（{len(rows)} 边）")


if __name__ == "__main__":
    main()
