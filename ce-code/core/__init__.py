"""core —— 贯穿全层的契约包（不含编排，不依赖任何业务层）。

三层（切分 splitter / 表征 reprs / 检索 retrieval）与编排（parse.py / build.py）只认
这里的契约：
  schema         节点契约（单一真值）：Node / Representation / Provenance + 受控词表。
  parse_profile  解析/索引流水线配置契约：structure_strategy / reprs / index_granularity。
  view           粒度视图：索引期在节点树上选「emit 哪一层」的确定性纯函数。
"""
from __future__ import annotations

from core import parse_profile, schema, view

__all__ = ["schema", "parse_profile", "view"]
