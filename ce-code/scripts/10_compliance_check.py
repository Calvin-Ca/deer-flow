"""阶段 2 核心：项目级合规检查 —— 端到端编排（CLI 入口）。

重构说明：编排逻辑已收敛到 ``service/orchestration.py``（被合规服务 :8101 复用），
本文件退化为薄 CLI——保留命令行参数与 rich 报告展示。

使用方式：
  .venv/bin/python scripts/10_compliance_check.py \\
    --project "地上11层住宅楼，总高32米，每层850平方米，地下一层车库" \\
    --store-dir data/vector_store/GB_50016-20142018
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.orchestration import LLM_MODEL_ID, LLM_URL, compliance_check  # noqa: E402

console = Console()


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------

def print_report(report: dict) -> None:
    console.print(Panel(
        f"[bold]{report.get('building_category', '未知类别')}[/bold]\n"
        f"强条共 {report['mandatory_clauses_total']} 条",
        title="合规检查报告",
        border_style="green",
    ))

    for dim in report.get("dimensions", []):
        t = Table(
            title=f"[cyan]{dim['dimension']}[/cyan]",
            show_header=True,
            header_style="bold",
            show_lines=True,
        )
        t.add_column("条款号", style="cyan", width=10)
        t.add_column("合规状态", width=12)
        t.add_column("说明", no_wrap=False, max_width=55)
        for c in dim.get("clauses", []):
            status = c.get("compliance_status", "")
            color = {
                "符合": "green", "不符合": "red",
                "需核实": "yellow", "需补充信息": "yellow", "不适用": "dim",
            }.get(status, "white")
            note = c.get("note") or (c.get("text") or "")[:60]
            t.add_row(c.get("clause", ""), f"[{color}]{status}[/{color}]", note)
        console.print(t)

    if report.get("uncertain_params"):
        console.print(f"\n[yellow]⚠ 需补充参数：{report['uncertain_params']}[/yellow]")
    if report.get("missed_dimensions_warning"):
        console.print(f"[yellow]⚠ 可能遗漏的维度：{report['missed_dimensions_warning']}[/yellow]")
    console.print(f"\n[dim]{report['disclaimer']}[/dim]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--project", "-p", required=True, help="项目自由文本描述。")
@click.option(
    "--store-dir", "store_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="向量索引目录（data/vector_store/<standard>/）。",
)
@click.option("--llm-url", default=LLM_URL, show_default=True)
@click.option("--llm-model-id", default=LLM_MODEL_ID, show_default=True)
@click.option("--skip-reflection", is_flag=True, help="跳过反思校验步骤（加速调试）。")
@click.option("--output", "output_path", default=None, help="报告写入 JSON 文件。")
def main(
    project: str,
    store_dir: Path,
    llm_url: str,
    llm_model_id: str,
    skip_reflection: bool,
    output_path: str | None,
) -> None:
    """项目级合规检查：参数提取 → 并行检索 → 按维度判定 → 反思校验。"""
    # CLI 直跑时把编排的 logging 进度打到控制台
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        report = compliance_check(project, store_dir, llm_url, llm_model_id, skip_reflection)
    except requests.RequestException as e:
        console.print(f"[red]服务调用失败：{e}[/red]")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]LLM 返回了非法 JSON：{e}[/red]")
        raise SystemExit(1)

    print_report(report)

    if output_path:
        Path(output_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[green]✓ 报告已写入 {output_path}[/green]")


if __name__ == "__main__":
    main()
