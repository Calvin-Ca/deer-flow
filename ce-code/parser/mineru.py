"""MinerU 解析模型 —— MineruParser（content_list.json → Document IR）。

把 MinerU v1 ``content_list.json`` 反序列化出的 list[dict] 归一成 ``Document``。纯格式转换、
无结构语义（条文号 / 目录标签 / 树边在切分层算）。底层复用 ``parser.format_adapter.FormatAdapter``
的成熟逻辑（page 归一、HTML 表格展开 colspan/rowspan 成矩形二维表、text_level 透传、block_idx
溯源），本类只把其 list[dict] 产物包成 ``Block`` / ``Document``。

阶段 0 的 PDF→content_list.json 实跑由 ``parser.mineru_client`` / ``pdf_parser`` / ``split_parse``
负责（远程 API 默认 / ``--local`` 本地 CLI），产物缓存于 ``data/parsed/<std>/``（不可变·只跑一次）；
本类是「阶段 0 缓存 → 阶段 1 IR」的适配，不触发解析本身。
"""
from __future__ import annotations

from core.document import Block, Document
from parser.base import Parser
from parser.format_adapter import FormatAdapter


class MineruParser(Parser):
    """MinerU v1 content_list → Document（包 FormatAdapter）。"""

    name = "mineru"

    def parse(self, raw: object, *, standard_id: str = "", source_file: str = "") -> Document:
        """MinerU content_list 列表 → Document。

        参数：
            raw (list[dict]): MinerU v1 content_list.json 反序列化产物。
            standard_id (str): 规范标识。
            source_file (str): content_list.json 路径（相对 data/parsed/）。
        返回：
            Document: blocks 为统一元素（FormatAdapter 归一产物）。
        """
        items = raw if isinstance(raw, list) else []
        blocks = [Block.from_dict(d) for d in FormatAdapter.adapt(items)]
        return Document(standard_id=standard_id, source_file=source_file, blocks=blocks)
