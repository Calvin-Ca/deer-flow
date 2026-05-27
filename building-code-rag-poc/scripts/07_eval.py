"""阶段 1：检索质量评测。

对 eval_set 中每条查询调用混合检索，计算：
  - 强条召回率（首要指标，must_be_mandatory=True 的用例）
  - 期望条款召回率（expected_clauses 命中比例）
  - 关联条款召回率（related_clauses 命中比例）

使用方式：
  .venv/bin/python scripts/07_eval.py \\
    --store-dir data/vector_store/GB_50016_20142018 \\
    --eval-set data/eval_set/gb50016_eval.json \\
    --skip-rerank
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent

console = Console()

COLLECTION_PREFIX = "building_code"


def _load_retrieve():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "retrieve_05",
        Path(__file__).parent / "05_retrieve.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.retrieve


def run_eval(
    eval_path: Path,
    store_dir: Path,
    milvus_host: str,
    milvus_port: int,
    collection_name: str,
    embed_url: str,
    embed_model_id: str,
    top_k: int,
    skip_rerank: bool,
    output_path: Path,
) -> None:
    retrieve_fn = _load_retrieve()

    with open(eval_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    rows = []
    for item in eval_set:
        query = item["query"]
        expected = set(item.get("expected_clauses", []))
        related = set(item.get("related_clauses", []))
        must_mandatory = item.get("must_be_mandatory", False)

        if not expected:
            # 超范围测试用例：不计入召回指标，单独记录
            rows.append({
                "id": item["id"],
                "query": query,
                "must_mandatory": must_mandatory,
                "expected": [],
                "related": list(related),
                "hit_expected": [],
                "hit_related": [],
                "missed_expected": [],
                "missed_related": list(related),
                "expected_recall": None,
                "related_recall": None,
                "mandatory_recall": None,
                "pass": None,
                "notes": item.get("notes", ""),
            })
            continue

        hits = retrieve_fn(
            query, store_dir, milvus_host, milvus_port, collection_name,
            embed_url, embed_model_id, top_k, top_k * 2, top_k * 2, skip_rerank,
        )
        hit_paths = {h["clause_path"] for h in hits}

        hit_expected = expected & hit_paths
        missed_expected = expected - hit_paths
        expected_recall = len(hit_expected) / len(expected)

        hit_related = related & hit_paths if related else set()
        related_recall = len(hit_related) / len(related) if related else None

        mandatory_recall = expected_recall if must_mandatory else None
        passed = (expected_recall == 1.0) if must_mandatory else (expected_recall >= 0.5)

        rows.append({
            "id": item["id"],
            "query": query,
            "must_mandatory": must_mandatory,
            "expected": list(expected),
            "related": list(related),
            "hit_expected": list(hit_expected),
            "hit_related": list(hit_related),
            "missed_expected": list(missed_expected),
            "missed_related": list(related - hit_paths),
            "expected_recall": expected_recall,
            "related_recall": related_recall,
            "mandatory_recall": mandatory_recall,
            "pass": passed,
            "notes": item.get("notes", ""),
        })

    _print_report(rows, top_k)

    summary = _build_summary(rows, top_k)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "details": rows}, f, ensure_ascii=False, indent=2)
    console.print(f"\n[green]✓ 详细结果写入 {output_path}[/green]")


def _build_summary(rows: list[dict], top_k: int) -> dict:
    scored = [r for r in rows if r["expected_recall"] is not None]
    mandatory_rows = [r for r in scored if r["must_mandatory"]]
    passed = [r for r in scored if r["pass"]]

    avg_expected = sum(r["expected_recall"] for r in scored) / len(scored) if scored else 0
    avg_mandatory = (
        sum(r["mandatory_recall"] for r in mandatory_rows) / len(mandatory_rows)
        if mandatory_rows else 0
    )
    related_scored = [r for r in scored if r["related_recall"] is not None]
    avg_related = (
        sum(r["related_recall"] for r in related_scored) / len(related_scored)
        if related_scored else 0
    )

    return {
        "top_k": top_k,
        "total_queries": len(rows),
        "scored_queries": len(scored),
        "pass_count": len(passed),
        "pass_rate": len(passed) / len(scored) if scored else 0,
        "avg_expected_recall": avg_expected,
        "avg_mandatory_recall": avg_mandatory,
        "avg_related_recall": avg_related,
        "missed_queries": [r["id"] for r in scored if not r["pass"]],
    }


def _print_report(rows: list[dict], top_k: int) -> None:
    scored = [r for r in rows if r["expected_recall"] is not None]
    mandatory_rows = [r for r in scored if r["must_mandatory"]]

    # 逐条结果表
    t = Table(title=f"逐条结果（top_k={top_k}）", show_header=True, header_style="bold cyan")
    t.add_column("ID", width=5)
    t.add_column("查询", max_width=30, no_wrap=False)
    t.add_column("期望召回", width=8, justify="right")
    t.add_column("关联召回", width=8, justify="right")
    t.add_column("强条", width=4)
    t.add_column("漏召", max_width=20, no_wrap=False)

    for r in rows:
        if r["expected_recall"] is None:
            t.add_row(r["id"], r["query"][:28], "[dim]N/A[/dim]", "[dim]N/A[/dim]", "", "[dim]超范围[/dim]")
            continue
        exp_pct = f"{r['expected_recall']:.0%}"
        rel_pct = f"{r['related_recall']:.0%}" if r["related_recall"] is not None else "-"
        mandatory_mark = "[red]✓[/red]" if r["must_mandatory"] else ""
        missed = ", ".join(r["missed_expected"]) or "[green]无[/green]"
        pass_color = "green" if r["pass"] else "red"
        t.add_row(
            r["id"],
            r["query"][:28],
            f"[{pass_color}]{exp_pct}[/{pass_color}]",
            rel_pct,
            mandatory_mark,
            missed,
        )

    console.print(t)

    # 汇总
    avg_expected = sum(r["expected_recall"] for r in scored) / len(scored) if scored else 0
    avg_mandatory = (
        sum(r["mandatory_recall"] for r in mandatory_rows) / len(mandatory_rows)
        if mandatory_rows else 0
    )
    related_scored = [r for r in scored if r["related_recall"] is not None]
    avg_related = (
        sum(r["related_recall"] for r in related_scored) / len(related_scored)
        if related_scored else 0
    )
    passed = [r for r in scored if r["pass"]]

    console.print(f"\n[bold]汇总（top_k={top_k}）[/bold]")
    console.print(f"  有效查询数：{len(scored)} / {len(rows)}")
    console.print(f"  通过数：{len(passed)} / {len(scored)}")
    console.print(f"  [bold red]强条平均召回率：{avg_mandatory:.1%}[/bold red]  ← 核心指标")
    console.print(f"  期望条款平均召回率：{avg_expected:.1%}")
    console.print(f"  关联条款平均召回率：{avg_related:.1%}")

    if failed := [r for r in scored if not r["pass"]]:
        console.print(f"\n[red]未通过的查询（{len(failed)} 条）：[/red]")
        for r in failed:
            console.print(f"  {r['id']} — {r['query'][:40]}  漏召：{r['missed_expected']}")


@click.command()
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="data/vector_store/<standard>/",
)
@click.option(
    "--eval-set", "eval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
@click.option("--top-k", default=20, show_default=True, help="检索 top-k。")
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--embed-url", default="http://localhost:8097", show_default=True)
@click.option("--embed-model-id", default="/model", show_default=True)
@click.option("--skip-rerank", is_flag=True, help="跳过 Rerank（调试用）。")
@click.option(
    "--output", "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="结果 JSON 输出路径（默认 data/eval_results/<standard>_eval.json）。",
)
def main(
    store_dir: Path,
    eval_path: Path | None,
    top_k: int,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    skip_rerank: bool,
    output_path: Path | None,
) -> None:
    """批量评测检索质量，输出强条召回率报告。"""

    if eval_path is None:
        eval_path = ROOT / "data" / "eval_set" / "gb50016_eval.json"

    collection_name = f"{COLLECTION_PREFIX}_{store_dir.name}".lower().replace("-", "_")

    if output_path is None:
        output_path = ROOT / "data" / "eval_results" / f"{store_dir.name}_eval.json"

    run_eval(
        eval_path, store_dir, milvus_host, milvus_port, collection_name,
        embed_url, embed_model_id, top_k, skip_rerank, output_path,
    )


if __name__ == "__main__":
    main()
