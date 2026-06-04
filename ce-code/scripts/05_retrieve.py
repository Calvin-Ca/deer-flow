"""阶段 1 第二步：混合检索 + 引用扩展 + Rerank —— CLI 入口（逻辑在 retrieval.engine）。

重构说明：检索逻辑已收敛到 ``retrieval/engine.py``，本文件退化为薄 CLI——
保留命令行参数、rich 表格展示与批量评测，检索直接调用 ``retrieval.engine.search``。

使用方式：
  # 单条查询
  .venv/bin/python scripts/05_retrieve.py \\
    --store-dir data/vector_store/GB_50016-20142018 \\
    --query "24米住宅疏散楼梯宽度"

  # 批量评测
  .venv/bin/python scripts/05_retrieve.py \\
    --store-dir data/vector_store/GB_50016-20142018 \\
    --eval-set data/eval_set/gb50016_eval.json \\
    --output data/eval_results/gb50016_retrieval.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

# 让 `python scripts/05_retrieve.py` 也能 import retrieval（POC 根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retrieval.config import collection_name  # noqa: E402
from retrieval.engine import search  # noqa: E402

console = Console()

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 批量评测
# ---------------------------------------------------------------------------

def run_eval(
    eval_path: Path,
    store_dir: Path,
    milvus_host: str,
    milvus_port: int,
    collection: str,
    embed_url: str,
    embed_model_id: str,
    top_k: int,
    skip_rerank: bool,
    output_path: Path,
) -> None:
    with open(eval_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    results = []
    recall_scores = []

    for item in eval_set:
        query = item["query"]
        expected = set(item.get("expected_clauses", []))
        if not expected:
            continue

        hits = search(
            query, store_dir, milvus_host, milvus_port, collection,
            embed_url, embed_model_id, top_k, top_k * 2, top_k * 2, skip_rerank,
        )
        hit_paths = {h["clause_path"] for h in hits}
        recalled = expected & hit_paths
        recall = len(recalled) / len(expected)

        mandatory_expected = set(item.get("expected_clauses", [])) if item.get("must_be_mandatory") else set()
        mandatory_recalled = mandatory_expected & hit_paths
        mandatory_recall = len(mandatory_recalled) / len(mandatory_expected) if mandatory_expected else None

        results.append({
            "id": item["id"],
            "query": query,
            "expected": list(expected),
            "recalled": list(recalled),
            "missed": list(expected - hit_paths),
            "recall": recall,
            "mandatory_recall": mandatory_recall,
        })
        recall_scores.append(recall)

    avg_recall = sum(recall_scores) / len(recall_scores) if recall_scores else 0
    mandatory_recalls = [r["mandatory_recall"] for r in results if r["mandatory_recall"] is not None]
    avg_mandatory_recall = sum(mandatory_recalls) / len(mandatory_recalls) if mandatory_recalls else 0

    summary = {
        "total_queries": len(results),
        "avg_recall": avg_recall,
        "avg_mandatory_recall": avg_mandatory_recall,
        "results": results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    console.print(f"\n[bold]评测结果[/bold]")
    console.print(f"  查询数：{len(results)}")
    console.print(f"  平均召回率：{avg_recall:.1%}")
    console.print(f"  强条平均召回率：{avg_mandatory_recall:.1%}  ← 核心指标")
    console.print(f"[green]✓ 详细结果写入 {output_path}[/green]")


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_results(query: str, results: list[dict]) -> None:
    console.print(f"\n[bold]查询：[/bold]{query}")
    console.print(f"[bold]召回 {len(results)} 条条款[/bold]\n")

    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("条款号", style="cyan", width=12)
    t.add_column("强条", width=4)
    t.add_column("来源", width=10)
    t.add_column("Rerank", justify="right", width=7)
    t.add_column("正文片段", no_wrap=False, max_width=60)

    for r in results:
        mandatory = "[red]✓[/red]" if r.get("is_mandatory") else ""
        rerank_score = f"{r['_rerank_score']:.3f}" if "_rerank_score" in r else "-"
        snippet = (r.get("content") or "")[:80].replace("\n", " ")
        t.add_row(r["clause_path"], mandatory, r.get("_source", ""), rerank_score, snippet)

    console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="data/vector_store/<standard>/",
)
@click.option("--query", "-q", default="", help="单条检索查询。")
@click.option(
    "--eval-set", "eval_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
@click.option(
    "--output", "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
)
@click.option("--top-k", default=20, show_default=True, help="最终返回条款数（强条不截断）。")
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--collection", default="", help="Milvus collection 名（默认从 store-dir 推断）。")
@click.option("--embed-url", default="http://localhost:8097", show_default=True,
              help="vLLM embedding 服务地址。")
@click.option("--embed-model-id", default="/model", show_default=True)
@click.option("--skip-rerank", is_flag=True, help="跳过 Rerank（调试用）。")
def main(
    store_dir: Path,
    query: str,
    eval_path: Path | None,
    output_path: Path | None,
    top_k: int,
    milvus_host: str,
    milvus_port: int,
    collection: str,
    embed_url: str,
    embed_model_id: str,
    skip_rerank: bool,
) -> None:
    """混合检索 + 引用扩展 + Rerank（Milvus + vLLM）。"""

    if not collection:
        collection = collection_name(store_dir.name)

    if eval_path:
        if output_path is None:
            output_path = ROOT / "data" / "eval_results" / f"{store_dir.name}_retrieval.json"
        run_eval(eval_path, store_dir, milvus_host, milvus_port, collection,
                 embed_url, embed_model_id, top_k, skip_rerank, output_path)
    elif query:
        results = search(
            query, store_dir, milvus_host, milvus_port, collection,
            embed_url, embed_model_id, top_k, top_k * 2, top_k * 2, skip_rerank,
        )
        print_results(query, results)
    else:
        console.print("[red]请指定 --query 或 --eval-set[/red]")


if __name__ == "__main__":
    main()
