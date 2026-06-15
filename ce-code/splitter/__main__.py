#!/usr/bin/env python3
"""splitter 启动脚本 —— 阶段 1 切分编排（registry 驱动的通用 dispatcher，从 ce-code 根运行）。

与 ``parser.__main__`` 同构：不硬编码任何切法，遍历 ``splitter.factory.REGISTRY``，凡声明了实跑
入口（``run_cli``）的切法，就把它的 click 命令挂载成 ``python -m splitter <切法名>``。新增可跑切法 =
实现 ``split`` + ``run_cli`` 并 ``register``，本入口零改动；占位切法（无 ``run_cli``）不出现在 CLI。

切分只到结构层：读阶段0 缓存 ``data/parsed/<std>/auto/*_content_list.json`` → parser 解析（仅格式
归一）成 Document → splitter 切分（目录打标 + 建树）→ 出 ``chunks.json``（``catalog_blocks.json`` 为
本层目录打标快照，顺带落盘）。不碰表征 / 索引（那是根 ``build.py`` 的事），故不依赖 Milvus / 嵌入服务。

用法（从 ce-code 根，单行命令）：
  python -m splitter toc --input data/parsed/<std>/auto/xxx_content_list.json --subsplit number
  python -m splitter toc --input xxx_content_list.json --toc-max-depth 2 --preview
"""

from __future__ import annotations

import click

from splitter.factory import REGISTRY


@click.group()
def cli() -> None:
    """阶段 1 切分编排（按注册切法动态挂载 run CLI）。"""


# 遍历注册表：凡声明 run_cli 的切法，挂载其阶段1 命令（占位切法无 run_cli → 跳过）
for _name, _spl in sorted(REGISTRY.items()):
    _build = getattr(_spl, "run_cli", None)
    if callable(_build):
        cli.add_command(_build(), name=_name)


if __name__ == "__main__":
    cli()
