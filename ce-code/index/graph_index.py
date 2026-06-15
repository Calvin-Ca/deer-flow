"""图索引 —— 知识图谱索引（占位·未实装，面向 Phase C 造价 KG）。

预留「构件→清单→定额→工料机」多跳关系的图索引插槽。规范内引用图（references/referenced_by）
当前作 Chunk 固有事实随 metadata 落盘、检索期一跳扩展即可，无需独立图库；本占位面向**真 KG**
（P0 用 PG 关联表模拟、P1 迁 Neo4j，见 DEV §3.3）。本轮只立接口骨架。
"""
from __future__ import annotations

from pathlib import Path

from ir.chunk import Chunk


def build(units: list[Chunk], store_dir: Path) -> None:
    """建图索引（占位·未实装）。"""
    raise NotImplementedError(
        "graph_index 未实装（占位）。规范内引用图随 metadata 落盘 + 检索期一跳扩展；"
        "本占位面向 Phase C 造价 KG（PG 关联表 / Neo4j）。"
    )
