"""表征层基类 —— Feature（PRD §3.1 多表征 / 表征轴的可插拔策略契约）。

一个 Feature 子类 = 把 Chunk 投影成**一种「可被检索的样子」**（一个语义投影面）。检索是多
表征的可组合并集；注册表（``feature/pipeline.py`` 的 ``REGISTRY``）按 ``kind`` 登记各表征实例，
``profile.features`` 决定本次启用哪些，可插拔、可开关、可消融。

无状态表征（raw / sparse / dense / context_aug）实例可单例复用、在 pipeline 里直接 new；有状态
表征（LLM 的 summary / questions 需持客户端）在子类 ``__init__`` 注入依赖后再 ``register``。

向量归属：``dense`` / ``context_aug`` 只产**待嵌入文本**（``ChunkFeature.text``），向量由索引期
（index/）用 embedding 模型统一计算（模型唯一 owner 在检索栈，表征层不加载模型→仍属「免费」）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.chunk import Chunk
from core.feature import ChunkFeature


class Feature(ABC):
    """表征策略基类：Chunk → 一种可被检索的投影（ChunkFeature）。

    子类约定：
        类属性 ``kind`` (str)：FeatureKind，注册表键（与产出 ChunkFeature.kind 一致）。
        方法 ``build(chunk)``：产该表征的 ChunkFeature，按表征语义只填相关字段。
    """

    kind: str

    @abstractmethod
    def build(self, chunk: Chunk) -> ChunkFeature:
        """把 Chunk 投影成本表征的 ChunkFeature。

        参数：
            chunk (Chunk): 切分层产出的节点。
        返回：
            ChunkFeature: 至少含 kind，按语义填 text/vector/data/meta。
        """
        raise NotImplementedError
