"""阶段 2 第一步：从自由文本提取结构化建筑参数（CLI 入口）。

重构说明：提取逻辑已收敛到 ``service/params.py``，本文件退化为薄 CLI。

使用方式：
  .venv/bin/python scripts/08_extract_params.py \\
    --description "地上11层住宅楼，总高32米，每层850平方米，地下一层车库"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import requests
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.params import LLM_MODEL_ID, LLM_URL, extract_params  # noqa: E402

console = Console()


@click.command()
@click.option("--description", "-d", required=True, help="项目自由文本描述。")
@click.option("--llm-url", default=LLM_URL, show_default=True)
@click.option("--llm-model-id", default=LLM_MODEL_ID, show_default=True)
@click.option("--output", "output_path", default=None, help="结果写入 JSON 文件。")
def main(description: str, llm_url: str, llm_model_id: str, output_path: str | None) -> None:
    """从自由文本描述提取结构化建筑参数（Qwen3-8B）。"""
    console.print("[dim]提取项目参数...[/dim]")
    try:
        params = extract_params(description, llm_url, llm_model_id)
    except requests.RequestException as e:
        console.print(f"[red]LLM 调用失败：{e}[/red]")
        raise SystemExit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]返回了非法 JSON：{e}[/red]")
        raise SystemExit(1)

    output = json.dumps(params, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(output, encoding="utf-8")
        console.print(f"[green]✓ 参数已写入 {output_path}[/green]")
    else:
        console.print(output)


if __name__ == "__main__":
    main()
