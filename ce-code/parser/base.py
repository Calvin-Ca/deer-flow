"""解析层基类 —— Parser（PRD §3.2 阶段 0→1：原始文档 → Document IR）。

一个 Parser 子类 = **一种解析模型**（MinerU / Unstructured / …）把原始产物归一成 ``Document``
（``core.document``）。下游切分层只吃 Document，**不关心上游用哪种解析模型**——这是「多解析
模型可插拔」的边界。注册表（``parser/factory.py``）按 ``name`` 登记，``profile.parser_strategy``
决定本次用哪种。

输入约定：各解析模型的「原始产物」形态不同（MinerU 吃 content_list.json 反序列化的 list[dict]），
故 ``parse`` 入参用宽松 ``raw``，由各子类自行解释；统一**输出 Document**。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.document import Document


class Parser(ABC):
    """解析模型基类：原始产物 → Document IR。

    子类约定：
        类属性 ``name`` (str)：解析模型名，注册表键（= profile.parser_strategy 取值）。
        方法 ``parse(raw, *, standard_id, source_file)``：产 Document。
    """

    name: str

    @abstractmethod
    def parse(self, raw: object, *, standard_id: str = "", source_file: str = "") -> Document:
        """把解析模型的原始产物归一成 Document。

        参数：
            raw (object): 解析模型的原始产物（如 MinerU content_list.json 的 list[dict]）。
            standard_id (str): 规范唯一标识（写入 Document）。
            source_file (str): 原始产物路径（相对 data/parsed/，写入 Document.source_file）。
        返回：
            Document: 统一元素流（list[Block]）。
        """
        raise NotImplementedError
