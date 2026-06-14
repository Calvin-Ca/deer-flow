"""混合检索单查询调试 CLI —— 薄封装 ``retrieval.engine.search``（看单条 query 命中）。

职责单一：给检索引擎套命令行参数 + rich 表格展示，人肉抽查「某条 query 召回了哪些
条款」。检索逻辑全在 ``retrieval/engine.py``，本文件只管参数解析与结果展示。

批量评测（eval-set / 召回率报告）见 ``tools/eval.py``，两处不重复实现。

使用方式（从 ce-code 根，单行命令）：
  python -m tools.retrieve_cli --store-dir data/vector_store/<std>/<profile> --query "24米住宅疏散楼梯宽度"
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from retrieval.config import collection_name
from retrieval.engine import search

console = Console()


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_results(query: str, results: list[dict]) -> None:
    """把单查询的命中条款渲染成 rich 表格打印到终端。

    参数：
        query (str): 检索查询原文。
        results (list[dict]): ``engine.search`` 的返回（每条带 clause_path / _source /
            _rerank_score / content 等字段）。
    返回：
        无（直接打印）。
    """
    console.print(f"\n[bold]查询：[/bold]{query}")
    console.print(f"[bold]召回 {len(results)} 条条款[/bold]\n")

    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("条款号", style="cyan", width=12)
    t.add_column("来源", width=10)
    t.add_column("Rerank", justify="right", width=7)
    t.add_column("正文片段", no_wrap=False, max_width=60)

    for r in results:
        rerank_score = f"{r['_rerank_score']:.3f}" if "_rerank_score" in r else "-"
        snippet = (r.get("content") or "")[:80].replace("\n", " ")
        t.add_row(r["clause_path"], r.get("_source", ""), rerank_score, snippet)

    console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="data/vector_store/<standard>/<profile>",
)
@click.option("--query", "-q", required=True, help="单条检索查询。")
@click.option("--top-k", default=20, show_default=True, help="最终返回条款数。")
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
    top_k: int,
    milvus_host: str,
    milvus_port: int,
    collection: str,
    embed_url: str,
    embed_model_id: str,
    skip_rerank: bool,
) -> None:
    """单查询混合检索 + 引用扩展 + Rerank（Milvus + vLLM），打印命中条款。"""

    if not collection:
        collection = collection_name(store_dir.name)

    results = search(
        query, store_dir, milvus_host, milvus_port, collection,
        embed_url, embed_model_id, top_k, top_k * 2, top_k * 2, skip_rerank,
    )
    print_results(query, results)


if __name__ == "__main__":
    main()
