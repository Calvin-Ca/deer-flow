"""检索层基类 —— Retriever（RetrievalQuery → list[RetrievedChunk]）。

一个 Retriever 子类 = **一路召回策略**（dense 向量 / bm25 关键词 / graph 图 / hybrid 混合）。
统一契约：吃 ``RetrievalQuery``（core.query），吐已排序的 ``RetrievedChunk``（core.retrieval）列表。

> 单路 retriever（dense/bm25）内部以「索引行 dict」做召回（复用旧 engine 的数值逻辑，行为逐字
> 保持），在 ``retrieve`` 出口转 RetrievedChunk；hybrid 在行 dict 层做 RRF 合并 + 引用扩展 + rerank，
> 最后统一转 RetrievedChunk。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.query import RetrievalQuery
from core.retrieval import RetrievedChunk


class Retriever(ABC):
    """检索策略基类：RetrievalQuery → list[RetrievedChunk]。

    子类约定：
        类属性 ``name`` (str)：策略名（dense / bm25 / hybrid / graph）。
        方法 ``retrieve(query)``：产已排序的 RetrievedChunk 列表。
    """

    name: str

    @abstractmethod
    def retrieve(self, query: RetrievalQuery) -> list[RetrievedChunk]:
        """执行检索。

        参数：
            query (RetrievalQuery): 查询契约（text / top_k / intent / filters / skip_rerank）。
        返回：
            list[RetrievedChunk]: 已排序命中（含来源 + 评分）。
        """
        raise NotImplementedError
