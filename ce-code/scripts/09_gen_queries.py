"""阶段 2 第二步：根据结构化建筑参数生成合规检索查询矩阵（CLI 入口）。

重构说明：规则逻辑已收敛到 ``service/queries.py``，本文件退化为薄 CLI。

使用方式：
  .venv/bin/python scripts/09_gen_queries.py --params-json /tmp/params.json
  .venv/bin/python scripts/09_gen_queries.py --params '{"building_type":"住宅",...}'
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.queries import gen_queries  # noqa: E402

console = Console()


@click.command()
@click.option(
    "--params-json", "params_json_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="08 脚本输出的参数 JSON 文件。",
)
@click.option("--params", "params_str", default=None, help="直接传 JSON 字符串。")
@click.option("--output", "output_path", default=None, help="查询矩阵写入 JSON 文件。")
def main(
    params_json_path: Path | None,
    params_str: str | None,
    output_path: str | None,
) -> None:
    """根据建筑参数生成 GB 50016 合规维度查询矩阵。"""
    if params_json_path:
        params = json.loads(params_json_path.read_text(encoding="utf-8"))
    elif params_str:
        params = json.loads(params_str)
    else:
        console.print("[red]请指定 --params-json 或 --params[/red]")
        raise SystemExit(1)

    queries = gen_queries(params)

    t = Table(title=f"查询矩阵（共 {len(queries)} 个维度）", show_header=True, header_style="bold cyan")
    t.add_column("维度", style="cyan", width=20)
    t.add_column("检索查询", no_wrap=False)
    for q in queries:
        t.add_row(q["dimension"], q["query"])
    console.print(t)

    if output_path:
        Path(output_path).write_text(
            json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[green]✓ 查询矩阵已写入 {output_path}[/green]")


if __name__ == "__main__":
    main()
