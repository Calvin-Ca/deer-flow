"""Phase C：从 GB 50500 正文抽「计价口径 / 费用构成」规则表 price_composition。

背景：2024 版规范重构后，GB 50500 已无清单项目录（清单项+编码+计算规则搬到
50854 等计算标准），只剩**计价规则正文 + 附录标准表格**。故不存在可与 bill_spec
的 9 位 code 对接的数据——「按 code 合并」模型作废（见 TODO「GB 50500 计价口径」条）。

本模块改抽组价引擎真正要程序化读的口径：**费用构成**。每条规则锚定 50500 原文
单句，正则抽出构成项列表，带 provenance 回指条文：

  - 综合单价构成（2.0.9）：人工费 / 材料费 / 施工机具使用费 / 管理费 / 利润 / 风险费用（不含增值税）
  - 工程造价构成（3.1.2）：分部分项 / 措施项目 / 其他项目 / 增值税（2024 版四部分，无独立规费）

规则声明式（RULES），加一条新构成只需补一个 RuleSpec（node_path + 正则 + 名称）。

输入：50500 的 chunks.json（splitter 产物）
输出：data/structured/price_composition.jsonl（一行一个「构成项」）+ 终端质量 report
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from cost.bill_spec import normalize_spec

console = Console()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = (ROOT / "data" / "structured"
                 / "GB_T50500_2024_建设工程工程量清单计价标准" / "default" / "chunks.json")
DEFAULT_OUTDIR = ROOT / "data" / "structured"

# 构成项分隔：顿号 + 末项「利润和…风险费用」的「和」
SPLIT_RE = re.compile(r"[、和]")


@dataclass(frozen=True)
class RuleSpec:
    """一条费用构成规则的抽取声明。

    参数（字段）：
      composite —— 被构成的费用名（如「综合单价」）。
      kind      —— 机器分类：unit_rate（综合单价层）/ project_cost（工程造价层）。
      node_path —— 构成定义所在节点的 node_path（provenance 锚点）。
      clause    —— 条文号（如 2.0.9），仅作溯源标注。
      pattern   —— 在该节点 content 上锚定「构成项列表」那段文字的正则（取 group 1）。
      note      —— 该构成的口径补充（如「不含增值税」），可空。
    返回：无（纯数据声明）。
    """

    composite: str
    kind: str
    node_path: str
    clause: str
    pattern: re.Pattern
    note: str = ""


# 声明式规则集：加新构成（如其他项目费细分）只需在此追加一条
RULES: list[RuleSpec] = [
    RuleSpec(
        composite="综合单价",
        kind="unit_rate",
        node_path="2",
        clause="2.0.9",
        pattern=re.compile(r"综合单价包括(.+?)，不包括增值税"),
        note="不含增值税；末项为一定范围内的风险费用",
    ),
    RuleSpec(
        composite="工程造价",
        kind="project_cost",
        node_path="3.1",
        clause="3.1.2",
        pattern=re.compile(r"工程量清单应按(.+?)分别编制"),
        note="2024 版四部分，无独立规费；分部分项/措施宜分别按单价/总价计价（3.2.1）",
    ),
]


def load_chunks(path: Path) -> list[dict]:
    """读 50500 的 chunks.json。

    参数：path —— chunks.json 路径。
    返回：节点 dict 列表。
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def split_components(segment: str) -> list[str]:
    """把锚定出的「构成项列表」文字切成项列表。

    "人工费、材料费、…、利润和一定范围内的风险费用" → ["人工费", …, "一定范围内的风险费用"]。

    参数：segment —— 正则 group 1 命中的列表文字。
    返回：去空白后的构成项列表（保持原文用词，不归一化以免杜撰）。
    """
    return [p.strip() for p in SPLIT_RE.split(segment) if p.strip()]


def extract(chunks: list[dict]) -> list[dict]:
    """按 RULES 在节点树上锚定各费用构成，展开成「构成项」行。

    参数：chunks —— 50500 chunks.json 节点列表。
    返回：构成项 dict 列表（每行一个 component，带 seq/provenance/治理字段）；
          规则未命中的（节点缺失 / 正则不中）抛 ValueError，宁缺毋造。
    """
    by_path = {c.get("node_path"): c for c in chunks}
    rows: list[dict] = []

    for rule in RULES:
        node = by_path.get(rule.node_path)
        if node is None:
            raise ValueError(f"规则 {rule.composite}：节点 {rule.node_path} 不在 chunks 中")
        m = rule.pattern.search(node.get("content") or "")
        if not m:
            raise ValueError(f"规则 {rule.composite}：正则未在节点 {rule.node_path} 命中"
                             f"（条文 {rule.clause} 措辞可能变化，需核对）")

        doc_id, spec_version = normalize_spec(node.get("standard_id") or "")
        components = split_components(m.group(1))
        for seq, comp in enumerate(components, 1):
            rows.append({
                "composite": rule.composite,
                "kind": rule.kind,
                "seq": seq,
                "component": comp,
                "note": rule.note,
                "doc_id": doc_id,
                "spec_version": spec_version,
                "provenance": {"node_path": rule.node_path, "clause": rule.clause},
            })

    return rows


def report(rows: list[dict]) -> None:
    """打印抽取结果（按构成分组），供人工对照原文核验。

    参数：rows —— extract 产出的构成项行。
    返回：无（终端打印）。
    """
    t = Table(title="费用构成规则（price_composition）")
    t.add_column("composite"); t.add_column("kind"); t.add_column("构成项（按序）")
    t.add_column("条文"); t.add_column("note")
    seen: dict[str, list[str]] = {}
    meta: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        seen.setdefault(r["composite"], []).append(f'{r["seq"]}.{r["component"]}')
        meta[r["composite"]] = (r["kind"], r["provenance"]["clause"], r["note"])
    for comp, items in seen.items():
        kind, clause, note = meta[comp]
        t.add_row(comp, kind, " ".join(items), clause, note)
    console.print(t)
    console.print(f"[green]共 {len(seen)} 个构成、{len(rows)} 个构成项[/]")


def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（一行一条记录，UTF-8 不转义）。

    参数：records —— 记录列表；path —— 输出路径。
    返回：无。
    """
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


@click.command()
@click.option("--input", "input_path", type=click.Path(exists=True, path_type=Path),
              default=DEFAULT_INPUT, show_default=True, help="50500 chunks.json 路径")
@click.option("--outdir", type=click.Path(path_type=Path), default=DEFAULT_OUTDIR,
              show_default=True, help="price_composition.jsonl 输出目录")
@click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
def main(input_path: Path, outdir: Path, dry_run: bool) -> None:
    """50500 chunks.json → price_composition.jsonl（费用构成规则）+ 质量 report。"""
    chunks = load_chunks(input_path)
    rows = extract(chunks)
    report(rows)

    if dry_run:
        console.print("[dim]--dry-run：未落盘[/]")
        return

    outdir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(rows, outdir / "price_composition.jsonl")
    console.print(f"[green]已写[/] {outdir/'price_composition.jsonl'}（{len(rows)} 行）")


if __name__ == "__main__":
    main()
