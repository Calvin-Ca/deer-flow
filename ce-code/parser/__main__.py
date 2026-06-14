#!/usr/bin/env python3
"""parser CLI —— 阶段 0 解析编排（MinerU PDF → 统一元素缓存，从 ce-code 根运行）。

把解析层 ``parser`` 的两个 CLI 收口成一个 click group，作阶段 0 的包级入口
（``python -m parser <子命令>``）。阶段 0（MinerU）最贵、只跑一次，产物缓存于
data/parsed/<std>/auto/（不可变）；下游切分 / 表征 / 索引见根 ``build.py``，从该缓存的
*_content_list.json 起读。

子命令：
  single  单 PDF 解析（默认走远程 MinerU API，--local 用本地 CLI）。← parser.pdf_parser
  split   大 PDF 分块解析（仅 --local 路径，规避本地 OOM）。            ← parser.split_parse

用法（从 ce-code 根）：
  python -m parser single --input data/raw/<std>.pdf
  python -m parser split  --input data/raw/<big>.pdf --chunk-size 80
  （或直接 python -m parser.pdf_parser / -m parser.split_parse）
"""

from __future__ import annotations

import click

from parser.pdf_parser import main as _single_cmd
from parser.split_parse import main as _split_cmd


@click.group()
def cli() -> None:
    """阶段 0 解析编排（MinerU）。"""


cli.add_command(_single_cmd, name="single")
cli.add_command(_split_cmd, name="split")


if __name__ == "__main__":
    cli()
