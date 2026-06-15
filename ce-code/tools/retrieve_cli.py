"""混合检索单查询调试 CLI —— 薄封装 ``retrieval.HybridRetriever``（看单条 query 命中）。

职责单一：给检索器套命令行参数 + rich 表格展示，人肉抽查「某条 query 召回了哪些条款」。检索逻辑
全在 ``retrieval/``（hybrid + RRF + 引用扩展 + rerank），本文件只管参数解析与结果展示。

批量评测（eval-set / 召回率报告）见 ``tools/eval.py``，两处不重复实现。

使用方式（从 ce-code 根，单行命令）：
  python -m tools.retrieve_cli --store-dir data/vector_store/<std>/<profile> --query "24米住宅疏散楼梯宽度"
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from config import collection_name
from ir.query import RetrievalQuery
from ir.retrieval import RetrievedChunk
from retrieval.hybrid_retriever import HybridRetriever

console = Console()


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_results(query: str, results: list[RetrievedChunk]) -> None:
    """把单查询的命中条款渲染成 rich 表格打印到终端。

    参数：
        query (str): 检索查询原文。
        results (list[RetrievedChunk]): hybrid.retrieve 的返回（每条带 node_path / source /
            scores / content 等）。
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
        rerank_score = f"{r.scores['rerank']:.3f}" if "rerank" in r.scores else "-"
        snippet = (r.content or "")[:80].replace("\n", " ")
        t.add_row(r.node_path, r.source, rerank_score, snippet)

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

    hybrid = HybridRetriever(
        store_dir, collection,
        milvus_host=milvus_host, milvus_port=milvus_port,
        embed_url=embed_url, embed_model_id=embed_model_id,
    )
    results = hybrid.retrieve(RetrievalQuery(text=query, top_k=top_k, skip_rerank=skip_rerank))
    print_results(query, results)


if __name__ == "__main__":
    main()
