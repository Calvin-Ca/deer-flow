"""解析层基类 —— Parser（PRD §3.2 阶段 0→1：原始文档 → Document IR）。

一个 Parser 子类 = **一种解析工具**（MinerU / Unstructured / …）。统一契约只有一个方法 ``adapt``
（阶段1：原始产物 → ``Document``）；下游切分层只吃 Document，**不关心上游用哪种解析工具**——这是
「多解析工具可插拔」的边界。注册表（``parser/factory.py``）按 ``name`` 登记，``profile.parser_strategy``
决定本次用哪种。

可选扩展（不进 ABC，由能跑的工具各自鸭子类型提供，``parser.__main__`` 通用挂载）：
    ``run(...)``      阶段0 实跑：原始文档 → 解析产物（如 PDF → content_list.json）。
    ``run_cli()``     阶段0 的 click 命令（``run`` 的薄壳）。
占位工具（如 Unstructured）只实现 ``adapt``、无 ``run``/``run_cli``，故不出现在阶段0 CLI。

输入约定：各解析工具的「原始产物」形态不同（MinerU 吃 content_list.json 反序列化的 list[dict]），
故 ``adapt`` 入参用宽松 ``raw``，由各子类自行解释；统一**输出 Document**。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.document import Document


class Parser(ABC):
    """解析工具基类：原始产物 → Document IR（阶段1 统一契约 ``adapt``）。

    子类约定：
        类属性 ``name`` (str)：解析工具名，注册表键（= profile.parser_strategy 取值）。
        方法 ``adapt(raw, *, standard_id, source_file)``：产 Document（阶段1）。
        可选 ``run(...)`` / ``run_cli()``：阶段0 实跑（仅可跑的工具提供）。
    """

    name: str

    @abstractmethod
    def adapt(self, raw: object, *, standard_id: str = "", source_file: str = "") -> Document:
        """把解析工具的原始产物归一成 Document（阶段1）。

        参数：
            raw (object): 解析工具的原始产物（如 MinerU content_list.json 的 list[dict]）。
            standard_id (str): 规范唯一标识（写入 Document）。
            source_file (str): 原始产物路径（相对 data/parsed/，写入 Document.source_file）。
        返回：
            Document: 统一元素流（list[Block]）。
        """
        raise NotImplementedError
