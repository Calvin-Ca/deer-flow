"""RetrievalQuery IR —— 检索入参契约（service 产 → retrieval 吃）。

替代旧 ``search(query, ..., top_k, skip_rerank)`` 散参为一个显式对象。业务层只做三件事
（PRD §职责边界）：① 选 intent；② 业务对象 → text + filters；③ 设 top_k。检索策略全包在
检索层，不下放。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 检索意图（通道权重/策略选择，PRD §4.1）。clause_lookup=算量取数（查条款），
# cost_match=清单匹配/组价（造价轨，待建）。
Intent = str  # Literal["clause_lookup", "cost_match"]，留宽松供扩展


@dataclass
class RetrievalQuery:
    """一次检索请求。

    字段：
        text        查询文本。
        standard    规范代号（解析到 store 目录，见 config.resolve_store_dir）。
        top_k       返回条数（上下文窗预算，业务层设定）。
        intent      clause_lookup | cost_match —— 策略选择参数。
        filters     元数据过滤（region 硬隔离 + standard/discipline/version，可选）。
        skip_rerank 跳过 cross-encoder 精排（调试/低延迟）。
    """

    text: str
    standard: str = "gb50016"
    top_k: int = 20
    intent: str = "clause_lookup"
    filters: dict = field(default_factory=dict)
    skip_rerank: bool = False

    def to_dict(self) -> dict:
        return {
            "text": self.text, "standard": self.standard, "top_k": self.top_k,
            "intent": self.intent, "filters": self.filters, "skip_rerank": self.skip_rerank,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RetrievalQuery":
        return cls(
            text=d.get("text", "") or d.get("query", ""),
            standard=d.get("standard", "gb50016"),
            top_k=int(d.get("top_k", 20)),
            intent=d.get("intent", "clause_lookup"),
            filters=dict(d.get("filters") or {}),
            skip_rerank=bool(d.get("skip_rerank", False)),
        )
