"""extract —— 建树期「固有事实」抽取（纯 stdlib，无 Milvus / LLM）。

  references.annotate_references   引用边分型（strong/weak/exclude/cross_standard）
                                   + referenced_by 反向边

被建树器 ``pipeline/tree_builder.py`` import：引用图分型是 PRD §3.1 的「固有事实」，
在建树时一次算定、落 nodes.json，故留在本包。

历史（2026-06-12 设计转向后随 T5 退役、已删除）：
  - ``build.py``      v1 富化链编排器（v1 条款 → v2 + to_v1_compat 桥）。
  - ``strength.py``   黑体强条 / 语气标注——「强条 / 法律强制」整套机制废除（PRD §3.1）。
  - ``ancestors.py``  祖先标题链——已由 ``TreeBuilder._attach_ancestors`` 接管。
  多表征（语气 / 表格 / 条件 / 摘要…）改由 ``reprs/`` 注册表（reprs.enrich）承担。
"""
from __future__ import annotations

from . import references

__all__ = ["references"]
