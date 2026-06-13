"""切分注册表 —— 阶段 1 结构层「切分轴」（PRD §3.1 / §3.2）。

把「文档怎么切成检索单元结构」做成可插拔策略：``REGISTRY`` 按 ``name`` 登记各
``Splitter`` 实例，``parse_profile.structure_strategy`` 决定本次用哪种切法，可 A/B、
可消融（不同 splitter = 不同 profile = 隔离索引，tools/eval 直接对比召回）。

当前唯一/默认成员 ``toc``（``TocSplitter``，基于原生目录的多层级切分，核心设计原则 1）。
其余切法（标题法 / 定长窗 / 语义切 / 表格行切）是规划中的扩展点——多 splitter 的真实
需求来自造价轨表格类文档（Phase C），届时 ``register`` 新实例即并入，下游粒度/表征不动。
"""
from __future__ import annotations

from splitter.base import Splitter, SplitResult  # noqa: F401  (SplitResult 转出供消费方 import)
from splitter.toc import TocSplitter

# name → 切分实例。新增切法 = new 一个 Splitter 子类并 register。
REGISTRY: dict[str, Splitter] = {}

DEFAULT_STRATEGY = "toc"  # 默认切法：原生目录多层级（建筑规范首选，PRD §一 核心设计原则）


def register(splitter: Splitter) -> None:
    """登记一个切分实例到 REGISTRY（name 重复则报错，避免静默覆盖）。

    参数：
        splitter (Splitter): 切分实例（其 ``name`` 作注册键）。
    返回：
        无。
    """
    if splitter.name in REGISTRY:
        raise ValueError(f"切分策略 name={splitter.name!r} 已注册，勿重复登记")
    REGISTRY[splitter.name] = splitter


def get(name: str) -> Splitter:
    """按名取切分策略；未注册则报错并列出可选项（语义化失败）。

    参数：
        name (str): 策略名（= profile.structure_strategy）。
    返回：
        Splitter: 已注册的切分实例。
    异常：
        ValueError: name 未注册。
    """
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"未知 structure_strategy={name!r}（已注册：{sorted(REGISTRY)}）"
        ) from None


register(TocSplitter())
