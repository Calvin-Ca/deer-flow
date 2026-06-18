"""Unstructured 解析模型 —— 占位（未实装）。

预留「另一种解析模型」插槽：Unstructured（https://github.com/Unstructured-IO/unstructured）
对 PDF/DOCX/HTML 等做版面解析。本轮只立接口骨架，``parse`` 抛 NotImplementedError；真正接入时
实现 raw → ``Document``（与 MineruParser 同契约），``register`` 进 factory 即并入，下游切分/表征/
索引/检索零改动。
"""
from __future__ import annotations

from ingest.ir.document import Document
from ingest.parser.base import Parser


class UnstructuredParser(Parser):
    """Unstructured 解析模型（占位·未实装）。"""

    name = "unstructured"

    def adapt(self, raw: object, *, standard_id: str = "", source_file: str = "") -> Document:
        raise NotImplementedError(
            "UnstructuredParser 未实装（占位）。当前仅 mineru 可用；接入时实现 raw → Document。"
        )
