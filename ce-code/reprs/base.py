"""表征基类 —— Representation（PRD §3.1 多表征 / 表征轴的可插拔策略契约）。

一个 Representation 子类 = 把节点投影成**一种「可被检索的样子」**（一个语义投影面）。
检索是多表征的可组合并集；注册表（``reprs/__init__.py`` 的 ``REGISTRY``）按 ``kind``
登记各表征实例，profile 的 ``reprs`` 列表决定本次启用哪些，可插拔、可开关、可消融。

无状态表征（raw / sparse / dense / context_aug）实例可单例复用、在注册表里直接 new；
有状态表征（波3 的 ``summary`` / ``questions`` 需持 LLM 客户端）在子类 ``__init__``
注入依赖后再 ``register``——基类形态正是为这类有状态表征留的扩展点。

向量归属：``dense`` / ``context_aug`` 只产**待嵌入文本**，向量由索引期 04 用 embedding
模型统一计算（模型「唯一 owner」在检索栈，表征层不加载模型）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Representation(ABC):
    """表征策略基类：节点 → 一种可被检索的投影（schema.Representation dict）。

    子类约定：
        类属性 ``kind`` (str)：ReprKind，注册表键（与产出 dict 的 "kind" 一致）。
        方法 ``build(node)``：产该表征的 schema.Representation（{kind, text?, vector?,
            data?, meta?}），按表征语义只填相关字段。
    """

    kind: str

    @abstractmethod
    def build(self, node: dict) -> dict:
        """把节点投影成本表征的 schema.Representation dict。

        参数：
            node (dict): schema.Node。
        返回：
            dict: schema.Representation —— 至少含 "kind"，按表征语义填 text/vector/data/meta。
        """
        raise NotImplementedError
