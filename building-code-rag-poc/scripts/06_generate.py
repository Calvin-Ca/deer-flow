"""阶段 1 第三步：生成层——检索结果 → 结构化回答。

流程（按 CLAUDE.md §4.4）：
  Query + 检索结果
    → 格式化 prompt（含条款原文 + 元数据）
    → Qwen3-8B（vLLM，JSON 模式，http://localhost:8099）
    → 结构化回答（answer + applicable_clauses + referenced_clauses
                   + uncertain_aspects + out_of_scope_warnings）

使用方式：
  # 端到端：检索 + 生成（需要服务器上的 Milvus + vLLM）
  .venv/bin/python scripts/06_generate.py \\
    --store-dir data/vector_store/GB_50378-2006 \\
    --query "住宅建筑绿色评价的节地指标要求" \\
    --skip-rerank

  # 只生成（从 JSON 读取已检索结果，本地 Mac 可用）
  .venv/bin/python scripts/06_generate.py \\
    --retrieved-json path/to/retrieved.json \\
    --query "防火墙耐火极限是多少"

  # 保存输出
  .venv/bin/python scripts/06_generate.py \\
    --store-dir data/vector_store/GB_50016-20142018 \\
    --query "..." \\
    --output data/answers/answer_001.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
你是一名专业的建筑规范查询助手，严格基于所提供的规范条款回答用户问题。

**核心约束（必须遵守）**：
1. 每个事实陈述必须引用具体条款号（如"第5.3.4条"），若规范中无依据，明确说明"本规范未涉及"，绝不编造内容
2. 强制性条款（含"必须""严禁""不应""不得"）和推荐性条款（含"宜""可"）必须显式区分，不得合并陈述
3. 用户场景超出当前规范适用范围时，需明确告知，并在 out_of_scope_warnings 中说明
4. 回答面向通用用户（设计师、施工人员、公众），避免过度使用专业缩写
5. 末尾免责：所有回答仅供参考，不替代专业审查

**输出要求**：返回合法的 JSON 对象，不输出任何 JSON 以外的文字。
"""


def build_user_message(query: str, clauses: list[dict]) -> str:
    # 按来源分组：检索召回 vs 引用图扩展
    retrieved = [c for c in clauses if c.get("_source") != "ref_expand"]
    ref_expanded = [c for c in clauses if c.get("_source") == "ref_expand"]
    n_mandatory = sum(1 for c in clauses if c.get("is_mandatory"))

    def clause_block(c: dict) -> list[str]:
        # is_mandatory 显式写入，要求模型原样复制，不自行判断
        mandatory_flag = "true【强制性，必须/严禁/不应/不得】" if c.get("is_mandatory") else "false【推荐性，宜/可】"
        return [
            f"--- 条款 {c['clause_path']} | is_mandatory={mandatory_flag} ---",
            f"规范：{c.get('standard_id', '未知')}",
            f"正文：{c.get('content', '').strip()}",
            "",
        ]

    lines: list[str] = []
    lines.append(f"用户问题：{query}")
    lines.append("")
    lines.append(
        f"从规范库检索到 {len(clauses)} 条相关条款（强制性 {n_mandatory} 条）。"
        f"每条的 is_mandatory 值已明确标注，请在输出 JSON 中原样复制，不要自行判断。"
    )
    lines.append("")

    if retrieved:
        lines.append(
            f"【A. 检索召回条款 {len(retrieved)} 条】"
            f"——这些条款直接或语义相关地回答了用户问题，应填入 applicable_clauses。"
        )
        lines.append("")
        for c in retrieved:
            lines.extend(clause_block(c))

    if ref_expanded:
        lines.append(
            f"【B. 引用扩展条款 {len(ref_expanded)} 条】"
            f"——这些条款是被 A 类条款所引用而自动拉取的关联条款，应填入 referenced_clauses。"
        )
        lines.append("")
        for c in ref_expanded:
            lines.extend(clause_block(c))

    lines.append("---")
    lines.append(
        "请严格基于以上条款回答问题，输出合法 JSON，不输出任何 JSON 以外的文字："
    )
    lines.append("")
    lines.append("""\
{
  "answer": "面向通用用户的自然语言回答。必须在正文中引用条款号。强制性条款（is_mandatory=true）明确标注为"强制要求"，推荐性条款（is_mandatory=false）标注为"推荐"。末尾附：本回答仅供参考，不替代专业审查。",
  "applicable_clauses": [
    {
      "clause": "仅填条款编号，如 4.4.1，不加"条款"前缀",
      "standard": "规范编号",
      "text": "相关原文（保持完整语义）",
      "is_mandatory": true或false（从A类条款的 is_mandatory 标注中原样复制）,
      "relevance": "direct（直接回答问题）或 contextual（背景参考）"
    }
  ],
  "referenced_clauses": [
    {
      "clause": "仅填条款编号，如 4.4.1，不加"条款"前缀",
      "standard": "规范编号",
      "text": "原文关键句",
      "is_mandatory": true或false（从B类条款的 is_mandatory 标注中原样复制）
    }
  ],
  "uncertain_aspects": ["需要专业人员核实的方面，若无则空数组"],
  "out_of_scope_warnings": ["超出本规范适用范围的提示，若无则空数组"]
}""")
    lines.append("")
    lines.append("/no_think")  # 禁用 Qwen3 thinking，避免干扰 JSON 输出

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# vLLM 调用
# ---------------------------------------------------------------------------

def call_qwen3(
    system_prompt: str,
    user_message: str,
    llm_url: str,
    model_id: str,
    temperature: float = 0.1,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    resp = requests.post(
        f"{llm_url}/v1/chat/completions",
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()

    raw_content = resp.json()["choices"][0]["message"]["content"].strip()

    # vLLM 偶尔在 JSON 外包一层 markdown 代码块，去掉它
    if raw_content.startswith("```"):
        lines = raw_content.splitlines()
        raw_content = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    return json.loads(raw_content)


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
# 检索集成（从 05_retrieve.py 导入）
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
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "retrieve", ROOT / "scripts" / "05_retrieve.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    collection = f"building_code_{store_dir.name}".lower().replace("-", "_")

    return mod.retrieve(
        query=query,
        store_dir=store_dir,
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        collection_name=collection,
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
