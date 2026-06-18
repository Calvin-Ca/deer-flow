"""Phase C · 知识图谱 P0：生成「清单 → 定额」APPLIES 映射（跑通组价取数路径）。

清单（GB 50854，9 位码）与定额（SJG，6 位+变体）编码体系不同、不可互推，映射是
组价的关键一跳。P0 先用**名称匹配**自动种子：清单名 == 定额名首段（base）→ 高置信，
清单名 ⊂ base → 低置信。一个清单常对应多个定额子目（做法/材质/规格变体）= 1:N APPLIES。

> 这是**起步映射**（覆盖有限、带 confidence/source 标注），非定稿；未覆盖项与低置信项
> 待富化（语义召回 / 专家标注），按红线「只建议不定稿」交任务层 HITL。

输入：扫 ``data/structured/<doc_id>/`` 下全部 bill_spec.jsonl（清单）+ quota_item.jsonl
      （定额子目）——跨规范汇总（SJG171 建筑 + SJG170 土方一起匹配，扩大覆盖）。
输出：bill_quota_map.jsonl（bill_code → quota_code，带 relation/confidence/source；跨规范
      关系产物，扁平落 ``data/structured/`` 下）+ 覆盖 report

取数路径（入库后，见 README Step C 验收 SQL）：
  bill_spec → bill_quota_map → quota_item → quota_resource → resource（→ resource_price）
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

# click / rich 在 CLI（_cli）与 report 内 lazy import，保持纯函数（match/_compose_capable_versions 等）
# 在无这些依赖的环境也可 import + 单测（与 cost.bill_index 同款）。

ROOT = Path(__file__).resolve().parent.parent
STRUCT = ROOT / "data" / "structured"


def _read_jsonl(path: Path) -> list[dict]:
    """读 JSONL。

    参数：path —— 文件路径。
    返回：dict 列表。
    """
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def base_name(name: str) -> str:
    """取定额名首段（base 子目名，去规格/变体）。

    "实心砖墙 1/2砖 干混砌筑砂浆" → "实心砖墙"。

    参数：name —— 定额 quota_item.name。
    返回：首个空格前的 base 名。
    """
    return (name or "").split(" ")[0]


def match(bills: list[dict], quotas: list[dict]) -> list[dict]:
    """名称匹配生成 APPLIES 映射边。

    清单名 == 定额 base 名 → confidence 0.9；清单名 ⊂ base 名 → 0.6。
    一个清单匹配多个定额子目（1:N）。每行带 ``bill_spec_version``（清单所属国标版本）做**版本隔离**：
    同 9 位码跨版本不同义，映射须按版本区分（compose_price join 按 bill_spec_version 过滤）。

    参数：bills —— 清单列表（每条须含 spec_version）；quotas —— 定额子目列表。
    返回：bill_quota_map 行列表。
    """
    by_base: dict[str, list[dict]] = defaultdict(list)
    for q in quotas:
        by_base[base_name(q["name"])].append(q)

    rows: list[dict] = []
    for b in bills:
        bn = (b.get("name") or "").strip()
        if not bn:
            continue
        for bk, qs in by_base.items():
            if bn == bk:
                conf, src = 0.9, "auto_name_exact"
            elif bn in bk:
                conf, src = 0.6, "auto_name_substr"
            else:
                continue
            for q in qs:
                rows.append({
                    "bill_code": b["code"], "bill_spec_version": b.get("spec_version"),
                    "quota_code": q["quota_code"],
                    "quota_doc_id": q.get("doc_id") or "SZ-SJG171",
                    "relation": "APPLIES", "confidence": conf, "source": src,
                    "note": f"{bn} → {q['name']}",
                })
    return rows


def _compose_capable_versions() -> set[str]:
    """从 SPEC_REGISTRY 取「有兼容定额、可组价」的清单 spec_version 集合。

    定额（SJG）口径与现行清单配套，跨版本套用会口径错位（见 notebooks 口径讨论 + BACKLOG）。
    故只为 supports_compose 的版本生成清单→定额映射；2013（定额未就绪）暂不生成，避免造
    「2013 清单 → 2024 SJG 定额」的错耦合行。Phase 2 翻 2013 supports_compose=True 后自动纳入。
    返回：可组价的 spec_version 字符串集合。
    """
    import config

    versions: set[str] = set()
    for cfg in config.SPEC_REGISTRY.values():
        if cfg.get("supports_compose"):
            versions.update(cfg.get("bill_spec_versions") or [])
    return versions


def report(rows: list[dict], n_bills: int) -> None:
    """打印映射覆盖 report。

    参数：rows —— 映射行；n_bills —— 清单总数。
    返回：无（终端打印）。
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    covered = {r["bill_code"] for r in rows}
    t = Table(title="清单→定额 映射（P0 名称匹配）", show_header=False)
    t.add_row("映射边（APPLIES）", str(len(rows)))
    t.add_row("覆盖清单", f"{len(covered)}/{n_bills}（{100*len(covered)//max(n_bills,1)}%）")
    by_src: dict[str, int] = defaultdict(int)
    for r in rows:
        by_src[r["source"]] += 1
    t.add_row("按来源", " / ".join(f"{k}:{v}" for k, v in by_src.items()))
    console.print(t)


def _write_jsonl(records: list[dict], path: Path) -> None:
    """写 JSONL（UTF-8 不转义）。

    参数：records —— 记录；path —— 路径。
    返回：无。
    """
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _scan(struct: Path, name: str) -> list[dict]:
    """跨 per-doc 子目录汇总同名 jsonl（``struct/<doc_id>/<name>``）。

    清单（bill_spec）与定额（quota_item）按 doc_id 分目录存放，APPLIES 映射需跨
    全部规范名称匹配，故汇总所有规范的同名产物（如 SJG171 + SJG170 定额一起）。
    参数：struct —— 结构化产物根；name —— 文件名。返回：拼接后的记录列表。
    """
    rows: list[dict] = []
    for path in sorted(struct.glob(f"*/{name}")):
        rows.extend(_read_jsonl(path))
    return rows


def _cli():
    """构造 click 命令（click/rich lazy import，纯函数模块顶层无依赖、本地可测）。"""
    import click
    from rich.console import Console

    console = Console()

    @click.command()
    @click.option("--struct-dir", "struct_dir", type=click.Path(exists=True, path_type=Path),
                  default=STRUCT, show_default=True,
                  help="结构化产物根；扫 <doc_id>/bill_spec.jsonl 与 <doc_id>/quota_item.jsonl")
    @click.option("--outdir", type=click.Path(path_type=Path), default=STRUCT, show_default=True)
    @click.option("--dry-run", is_flag=True, help="只看 report，不落盘")
    def main(struct_dir: Path, outdir: Path, dry_run: bool) -> None:
        """bill_spec + quota_item → bill_quota_map.jsonl（清单→定额 APPLIES 名称匹配）。"""
        bills = _scan(struct_dir, "bill_spec.jsonl")
        quotas = _scan(struct_dir, "quota_item.jsonl")
        if not bills or not quotas:
            raise click.ClickException(
                f"未扫到 bill_spec/quota_item（bills={len(bills)} quotas={len(quotas)}）："
                f"确认 {struct_dir}/<doc_id>/ 下已生成 per-doc 产物")
        # 版本隔离：只为「有兼容定额」的清单版本建映射（2013 定额未就绪 → 跳过，免造错耦合）
        capable = _compose_capable_versions()
        bills_capable = [b for b in bills if b.get("spec_version") in capable]
        skipped = len(bills) - len(bills_capable)
        if skipped:
            console.print(f"[yellow]跳过 {skipped} 条无兼容定额版本的清单[/]（仅为 {sorted(capable)} 建映射）")
        rows = match(bills_capable, quotas)
        report(rows, len(bills_capable))
        if dry_run:
            console.print("[dim]--dry-run：未落盘[/]")
            return
        outdir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(rows, outdir / "bill_quota_map.jsonl")  # 跨规范关系产物，保持扁平
        console.print(f"[green]已写[/] {outdir/'bill_quota_map.jsonl'}（{len(rows)} 边）")

    return main


if __name__ == "__main__":
    _cli()()
