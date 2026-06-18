#!/usr/bin/env python3
"""parser 启动脚本 —— 阶段 0 解析编排（registry 驱动的通用 dispatcher，从 ce-code 根运行）。

不再硬编码任何工具：遍历 ``parser.factory.REGISTRY``，凡是声明了阶段0 实跑入口（``run_cli``）的
解析工具，就把它的 click 命令挂载成 ``python -m parser <工具名>``。新增可跑工具 = 实现 ``parse`` +
``run_cli`` 并 ``register``，本入口零改动；占位工具（无 ``run_cli``）不出现在 CLI。

阶段0（MinerU 等）最贵、只跑一次，产物缓存于 data/parsed/<std>/auto/（不可变）；下游切分 /
表征 / 索引见根 ``build.py``，从该缓存的 *_content_list.json 起读。

用法（从 ce-code 根，单行命令）：
  python -m parser mineru --pdf data/raw/<std>.pdf            # 调远程 MinerU API
"""

from __future__ import annotations

import click

from ingest.parser.factory import REGISTRY


@click.group()
def cli() -> None:
    """阶段 0 解析编排（按注册工具动态挂载 run CLI）。"""


# 遍历注册表：凡声明 run_cli 的工具，挂载其阶段0 命令（占位工具无 run_cli → 跳过）
for _name, _tool in sorted(REGISTRY.items()):
    _build = getattr(_tool, "run_cli", None)
    if callable(_build):
        cli.add_command(_build(), name=_name)


if __name__ == "__main__":
    cli()
