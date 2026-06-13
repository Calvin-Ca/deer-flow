"""context_aug 表征 —— 拼祖先链的语境增强文本（small-to-big 入口，免费）。

PRD §3.1：context_aug 载体=向量 + BM25，作用=拼祖先链的语境增强文本（small-to-big
入口），成本=免费。叶子节点单看正文常缺语境（"应符合本表规定"脱离所在章节难判），
故在其文本前拼上祖先标题链 → 既利语义召回，也作 small-to-big「小粒度匹配、大粒度
返回」的索引侧入口。

**接管 extract/ancestors.py**：祖先链 ``ancestor_titles`` 已由建树器 TreeBuilder 在
nodes.json 里作「固有事实」一次算定，本表征**直接复用、不重算**（ancestors.py 的 v1
重算逻辑随 T5 退役）。向量同 dense，由索引期 04 计算。
"""
from __future__ import annotations

from .base import Representation

_PATH_SEP = " / "   # 祖先标题之间
_BODY_SEP = " ‖ "   # 祖先链与本条正文之间（PRD §3.1 示例用此分隔符）


class ContextAugRepr(Representation):
    """context_aug 表征：祖先标题链 ‖ 本条正文（复用 TreeBuilder 算定的 ancestor_titles）。"""

    kind = "context_aug"

    def build(self, node: dict) -> dict:
        """产 context_aug 表征：祖先标题链 ‖ 本条正文。

        参数：
            node (dict): schema.Node（读其 ancestor_titles / content / title）。
        返回：
            dict: schema.Representation —— {kind, text, meta}；text 形如
                "5 建筑分类… / 5.3 防火分区… ‖ <本条正文>"；无祖先时退化为正文本身。
                meta.ancestors 记拼入的祖先层数（审计）。
        """
        ancestors = [t for t in (node.get("ancestor_titles") or []) if t]
        body = node.get("content", "").strip() or node.get("title", "").strip()
        prefix = _PATH_SEP.join(ancestors)
        text = f"{prefix}{_BODY_SEP}{body}" if prefix else body
        return {"kind": self.kind, "text": text, "meta": {"ancestors": len(ancestors)}}
