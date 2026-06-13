"""raw 表征 —— 节点字面原文（返回用，免费）。

PRD §3.1 多表征表：raw 载体=存储，作用=返回展示用的字面原文，成本=免费。
不进语义/稀疏召回（那是 dense/sparse 的事），只承载「命中后给用户看什么」。
"""
from __future__ import annotations

from .base import Representation


class RawRepr(Representation):
    """raw 表征：节点自身正文（不含子节点、不拼祖先），供命中后展示。"""

    kind = "raw"

    def build(self, node: dict) -> dict:
        """产 raw 表征：节点 content 原文。

        参数：
            node (dict): schema.Node。
        返回：
            dict: schema.Representation —— {kind, text}（text 为节点 content 原文）。
        """
        return {"kind": self.kind, "text": node.get("content", "")}
