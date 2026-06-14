"""sparse 表征 —— BM25 条文号 / 术语精确召回文本（免费）。

PRD §3.1：sparse 载体=BM25，作用=条文号 / 术语精确召回，成本=免费。
把 node_path（保证裸条文号 "5.3.4" 作为可检索 token，即便 title 为空）、title、
content 拼成一段词项丰富文本，交索引期 BM25 分词建倒排。向量化不在此（那是 dense）。
"""
from __future__ import annotations

from .base import Representation


class SparseRepr(Representation):
    """sparse 表征：node_path + title + content 的词项丰富拼接（供 BM25 分词索引）。"""

    kind = "sparse"

    def build(self, node: dict) -> dict:
        """产 sparse 表征：node_path + title + content 拼接。

        参数：
            node (dict): schema.Node。
        返回：
            dict: schema.Representation —— {kind, text}（供 BM25 分词索引）。
        """
        parts = [node.get("node_path", ""), node.get("title", ""), node.get("content", "")]
        text = "\n".join(p for p in (s.strip() for s in parts) if p)
        return {"kind": self.kind, "text": text}
