"""检索质量评测（⚠️ T10 待换 Recall@k / 引用召回 / MRR；强条口径已废，见 schema.py）。

对 eval_set 中每条查询调用混合检索，计算：
  - 期望条款召回率（expected_clauses 命中比例，首要指标）
  - 关联条款召回率（related_clauses 命中比例）
  - 通过判定：期望召回率 ≥ 0.5（强条机制 2026-06-12 废，不再区分强条/非强条阈值）

**判命中口径 = 包含关系**（DEV §5.1）：返回块**包含或等于**目标条即算命中（``_contains``），
非严格 node_path 相等。clause 粒度下返回单元与目标条 1:1、包含=相等；但只 emit 到粗粒度
（section）的 profile 下，返回 "5.3" 应命中期望 "5.3.4"——否则粗粒度 profile 被系统性低估、
ablation 结论失真。

使用方式（从 ce-code 根，单行命令）：
  python -m tools.eval --store-dir data/vector_store/<std>/<profile> --eval-set ../benchmark/L3_retrieval/data/gb50016_eval.json --skip-rerank
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from config import collection_name as build_collection_name
from ir.query import RetrievalQuery
from retrieval.hybrid_retriever import HybridRetriever

ROOT = Path(__file__).resolve().parent.parent

console = Console()

# 附录根 node_path（"附录E"）→ 其下条号以字母打头（E.1 / E.1.1），故包含判定需特判。
_APPENDIX_ROOT_RE = re.compile(r"^附录\s*([A-Z])$")


def _contains(returned_path: str, target_path: str) -> bool:
    """返回块 ``returned_path`` 是否「包含或等于」目标条 ``target_path``（按 node_path 层级）。

    包含 = 返回块是目标条本身或其祖先（粗粒度命中细粒度目标）：
      · 相等：``"5.3.4" == "5.3.4"``。
      · 数字/附录字母号段后代：``"5.3"`` 包含 ``"5.3.4" / "5.3.4.1"``；``"E.1"`` 包含 ``"E.1.1"``
        （靠 ``target.startswith(returned + ".")``，号段边界以 "." 划清，避免 "5.3" 误含 "5.30"）。
      · 附录根：``"附录E"`` 包含 ``"E.1" / "E.1.1"``（附录条号以字母 E 打头，非 "附录E." 前缀）。
    其余（无编号标题路径、跨分支）一律不含。
    """
    if returned_path == target_path:
        return True
    app = _APPENDIX_ROOT_RE.match(returned_path)
    if app:
        letter = app.group(1)
        return target_path.startswith(letter + ".")
    return target_path.startswith(returned_path + ".")


def _hit(targets: set[str], hit_paths: list[str]) -> set[str]:
    """目标条集合里被「包含关系」命中的子集（任一返回块包含该目标条即命中）。"""
    return {t for t in targets if any(_contains(p, t) for p in hit_paths)}


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
    with open(eval_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    hybrid = HybridRetriever(
        store_dir, collection_name,
        milvus_host=milvus_host, milvus_port=milvus_port,
        embed_url=embed_url, embed_model_id=embed_model_id,
    )

    rows = []
    for item in eval_set:
        query = item["query"]
        expected = set(item.get("expected_clauses", []))
        related = set(item.get("related_clauses", []))

        if not expected:
            # 超范围测试用例：不计入召回指标，单独记录
            rows.append({
                "id": item["id"],
                "query": query,
                "expected": [],
                "related": list(related),
                "hit_expected": [],
                "hit_related": [],
                "missed_expected": [],
                "missed_related": list(related),
                "expected_recall": None,
                "related_recall": None,
                "pass": None,
                "notes": item.get("notes", ""),
            })
            continue

        hits = hybrid.retrieve(RetrievalQuery(text=query, top_k=top_k, skip_rerank=skip_rerank))
        hit_paths = [h.node_path for h in hits]

        # 包含关系判命中（_hit）：返回块包含或等于目标条即命中，非严格相等
        hit_expected = _hit(expected, hit_paths)
        missed_expected = expected - hit_expected
        expected_recall = len(hit_expected) / len(expected)

        hit_related = _hit(related, hit_paths) if related else set()
        related_recall = len(hit_related) / len(related) if related else None

        passed = expected_recall >= 0.5

        rows.append({
            "id": item["id"],
            "query": query,
            "expected": list(expected),
            "related": list(related),
            "hit_expected": list(hit_expected),
            "hit_related": list(hit_related),
            "missed_expected": list(missed_expected),
            "missed_related": list(related - hit_related),
            "expected_recall": expected_recall,
            "related_recall": related_recall,
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
    passed = [r for r in scored if r["pass"]]

    avg_expected = sum(r["expected_recall"] for r in scored) / len(scored) if scored else 0
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
        "avg_related_recall": avg_related,
        "missed_queries": [r["id"] for r in scored if not r["pass"]],
    }


def _print_report(rows: list[dict], top_k: int) -> None:
    scored = [r for r in rows if r["expected_recall"] is not None]

    # 逐条结果表
    t = Table(title=f"逐条结果（top_k={top_k}）", show_header=True, header_style="bold cyan")
    t.add_column("ID", width=5)
    t.add_column("查询", max_width=30, no_wrap=False)
    t.add_column("期望召回", width=8, justify="right")
    t.add_column("关联召回", width=8, justify="right")
    t.add_column("漏召", max_width=20, no_wrap=False)

    for r in rows:
        if r["expected_recall"] is None:
            t.add_row(r["id"], r["query"][:28], "[dim]N/A[/dim]", "[dim]N/A[/dim]", "[dim]超范围[/dim]")
            continue
        exp_pct = f"{r['expected_recall']:.0%}"
        rel_pct = f"{r['related_recall']:.0%}" if r["related_recall"] is not None else "-"
        missed = ", ".join(r["missed_expected"]) or "[green]无[/green]"
        pass_color = "green" if r["pass"] else "red"
        t.add_row(
            r["id"],
            r["query"][:28],
            f"[{pass_color}]{exp_pct}[/{pass_color}]",
            rel_pct,
            missed,
        )

    console.print(t)

    # 汇总
    avg_expected = sum(r["expected_recall"] for r in scored) / len(scored) if scored else 0
    related_scored = [r for r in scored if r["related_recall"] is not None]
    avg_related = (
        sum(r["related_recall"] for r in related_scored) / len(related_scored)
        if related_scored else 0
    )
    passed = [r for r in scored if r["pass"]]

    console.print(f"\n[bold]汇总（top_k={top_k}）[/bold]")
    console.print(f"  有效查询数：{len(scored)} / {len(rows)}")
    console.print(f"  通过数：{len(passed)} / {len(scored)}")
    console.print(f"  [bold]期望条款平均召回率：{avg_expected:.1%}[/bold]  ← 核心指标")
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
    """批量评测检索质量，输出期望/关联条款召回率报告。"""

    if eval_path is None:
        eval_path = ROOT.parent / "benchmark" / "L3_retrieval" / "data" / "gb50016_eval.json"

    collection_name = build_collection_name(store_dir.name)

    if output_path is None:
        output_path = ROOT / "data" / "eval_results" / f"{store_dir.name}_eval.json"

    run_eval(
        eval_path, store_dir, milvus_host, milvus_port, collection_name,
        embed_url, embed_model_id, top_k, skip_rerank, output_path,
    )


if __name__ == "__main__":
    main()
