"""轻量文本归一 —— 占位（按需扩展）。

当前知识层文本归一由各层就近处理（FormatAdapter 的 strip / splitter.toc_splitter 的 _norm /
references 的空白压缩）。本模块预留为「跨层共享的文本清洗」收口点，暂只放最小工具，
真正需要时（如造价电子表清洗）再充实。
"""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    """压缩连续空白为单空格并去首尾空白（跨规范号/标题归一常用）。"""
    return _WS_RE.sub(" ", text or "").strip()
