"""parser —— ① 解析层：PDF → MinerU → 统一元素块（结构层的上游）。

  mineru_client   远程 MinerU API 客户端（默认解析方式，:8000）。
  pdf_parser      PDF 解析 CLI（默认走 API，--local 用本地 CLI）。
  split_parse     大 PDF 分块解析（仅 --local 路径，规避本地 OOM）。
  format_adapter  MinerU v1 content_list → 下游通用统一块 schema（page 归一、HTML 表格
                  解析成矩形二维表、text_level 透传、block_idx 溯源）；切分前通用适配，
                  不内聚进某一切法（splitter）。

阶段 0（MinerU）最贵、只跑一次，产物缓存于 ``data/parsed/<std>/``（不可变）；编排见包级入口
``__main__.py``（``python -m parser``）。format_adapter 的产物喂给切分层 ``splitter``（见根 ``build.py``）。
"""
from __future__ import annotations

from parser.format_adapter import FormatAdapter

__all__ = ["FormatAdapter"]
