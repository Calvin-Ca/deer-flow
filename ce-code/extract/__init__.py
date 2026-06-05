"""extract —— 构建层:把解析层产出的条款树(v1 dict)富化为多表征 v2 条款。

各 enricher 都是 **原地富化**(输入 v1 条款 list,补 v2 字段),互相解耦、可单测,
不依赖 Milvus / LLM 的部分纯 stdlib:

  references.annotate_references     波1  引用边分型 + referenced_by 反向边
  strength.annotate_strength         波1  modal_strength / is_mandatory_clause(黑体)
  ancestors.attach_ancestor_titles   波3  祖先标题链(章/节)
  tables.structure_tables            波2  表格 → 可查询结构化 JSON(待补)
  scope.attach_scope                 波3  适用范围谓词(规则 + LLM,待补)

编排见 build.py(将 v1 条款跑完整条富化链 → 写 v2 + to_v1_compat 桥接)。
"""
from __future__ import annotations

from . import ancestors, references, strength

__all__ = ["references", "strength", "ancestors"]
