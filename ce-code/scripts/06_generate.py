"""阶段 1 第三步：生成层 —— 检索结果 → 结构化回答（CLI 入口）。

重构说明：生成逻辑已收敛到 ``service/generation.py``，检索走 ``retrieval.engine.search``，
本文件退化为薄 CLI——保留命令行参数与 rich 展示。

使用方式：
  # 端到端：检索 + 生成（需要服务器上的 Milvus + vLLM）
  .venv/bin/python scripts/06_generate.py \\
    --store-dir data/vector_store/GB_50016-20142018 \\
    --query "防火墙耐火极限是多少" --skip-rerank

  # 只生成（从 JSON 读取已检索结果，本地 Mac 可用）
  .venv/bin/python scripts/06_generate.py \\
    --retrieved-json path/to/retrieved.json \\
    --query "防火墙耐火极限是多少"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retrieval.config import collection_name  # noqa: E402
from retrieval.engine import search  # noqa: E402
from service.generation import SYSTEM_PROMPT, build_user_message, call_qwen3  # noqa: E402

console = Console()

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_response(query: str, response: dict) -> None:
    console.print(f"\n[bold cyan]查询：[/bold cyan]{query}\n")

    answer = response.get("answer", "")
    console.print(Panel(answer, title="回答", border_style="green"))

    applicable = response.get("applicable_clauses", [])
    if applicable:
        t = Table(title="适用条款", show_header=True, header_style="bold yellow")
        t.add_column("条款号", style="cyan", width=10)
        t.add_column("强条", width=4)
        t.add_column("相关性", width=10)
        t.add_column("原文片段", no_wrap=False, max_width=60)
        for c in applicable:
            mandatory = "[red]✓[/red]" if c.get("is_mandatory") else ""
            snippet = (c.get("text") or "")[:80].replace("\n", " ")
            t.add_row(c.get("clause", ""), mandatory, c.get("relevance", ""), snippet)
        console.print(t)

    referenced = response.get("referenced_clauses", [])
    if referenced:
        t2 = Table(title="引用条款", show_header=True, header_style="bold blue")
        t2.add_column("条款号", style="cyan", width=10)
        t2.add_column("强条", width=4)
        t2.add_column("原文片段", no_wrap=False, max_width=70)
        for c in referenced:
            mandatory = "[red]✓[/red]" if c.get("is_mandatory") else ""
            snippet = (c.get("text") or "")[:80].replace("\n", " ")
            t2.add_row(c.get("clause", ""), mandatory, snippet)
        console.print(t2)

    uncertainties = response.get("uncertain_aspects", [])
    if uncertainties:
        console.print("[bold yellow]⚠ 不确定方面：[/bold yellow]")
        for u in uncertainties:
            console.print(f"  • {u}")

    warnings = response.get("out_of_scope_warnings", [])
    if warnings:
        console.print("[bold red]⚠ 适用范围提示：[/bold red]")
        for w in warnings:
            console.print(f"  • {w}")


# ---------------------------------------------------------------------------
# 检索（直接走 retrieval）
# ---------------------------------------------------------------------------

def run_retrieval(
    query: str,
    store_dir: Path,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    top_k: int,
    skip_rerank: bool,
) -> list[dict]:
    return search(
        query=query,
        store_dir=store_dir,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        collection_name=collection_name(store_dir.name),
        embed_url=embed_url,
        embed_model_id=embed_model_id,
        top_k=top_k,
        bm25_top_k=top_k * 2,
        vector_top_k=top_k * 2,
        skip_rerank=skip_rerank,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--query", "-q", required=True, help="用户查询（自然语言）。")
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="向量索引目录（data/vector_store/<standard>/）。与 --retrieved-json 二选一。",
)
@click.option(
    "--retrieved-json", "retrieved_json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="已检索结果 JSON 文件（跳过检索步骤，本地测试用）。",
)
@click.option("--top-k", default=20, show_default=True, help="最终检索条款数。")
@click.option("--milvus-host", default="localhost", show_default=True)
@click.option("--milvus-port", default=19530, show_default=True)
@click.option("--embed-url", default="http://localhost:8097", show_default=True)
@click.option("--embed-model-id", default="/model", show_default=True)
@click.option("--skip-rerank", is_flag=True, help="跳过 Rerank（调试用）。")
@click.option(
    "--llm-url", default="http://localhost:8099", show_default=True,
    help="Qwen3 vLLM 服务地址。",
)
@click.option("--llm-model-id", default="qwen3-8b", show_default=True)
@click.option("--temperature", default=0.1, show_default=True)
@click.option("--max-tokens", default=4096, show_default=True)
@click.option(
    "--output", "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="将结构化回答保存到 JSON 文件。",
)
def main(
    query: str,
    store_dir: Path | None,
    retrieved_json: Path | None,
    top_k: int,
    milvus_host: str,
    milvus_port: int,
    embed_url: str,
    embed_model_id: str,
    skip_rerank: bool,
    llm_url: str,
    llm_model_id: str,
    temperature: float,
    max_tokens: int,
    output_path: Path | None,
) -> None:
    """检索 + Qwen3 生成结构化规范回答。"""

    if store_dir is None and retrieved_json is None:
        console.print("[red]错误：必须指定 --store-dir 或 --retrieved-json 之一[/red]")
        raise SystemExit(1)

    # 1. 获取检索结果
    if retrieved_json:
        console.print(f"[dim]从文件加载检索结果：{retrieved_json}[/dim]")
        with open(retrieved_json, encoding="utf-8") as f:
            clauses = json.load(f)
    else:
        console.print(f"[dim]检索 {store_dir.name}...[/dim]")
        clauses = run_retrieval(
            query, store_dir, milvus_host, milvus_port,
            embed_url, embed_model_id, top_k, skip_rerank,
        )

    console.print(
        f"[dim]检索到 {len(clauses)} 条条款，"
        f"其中强条 {sum(1 for c in clauses if c.get('is_mandatory'))} 条[/dim]"
    )

    # 2. 构建 prompt
    user_msg = build_user_message(query, clauses)

    # 3. 调用 Qwen3
    console.print(f"[dim]调用 Qwen3 ({llm_url})...[/dim]")
    try:
        response = call_qwen3(
            SYSTEM_PROMPT, user_msg, llm_url, llm_model_id, temperature, max_tokens,
        )
    except requests.RequestException as e:
        console.print(f"[red]LLM 调用失败：{e}[/red]")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]LLM 返回了非法 JSON：{e}[/red]")
        raise SystemExit(1)

    # 4. 展示
    print_response(query, response)

    # 5. 保存
    if output_path:
        result = {
            "query": query,
            "retrieved_clauses_count": len(clauses),
            "response": response,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        console.print(f"[green]✓ 回答已保存至 {output_path}[/green]")


if __name__ == "__main__":
    main()
