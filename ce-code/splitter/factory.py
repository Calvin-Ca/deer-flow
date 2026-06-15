"""切分层工厂 —— 按 ``profile.structure_strategy`` 取切分策略（可插拔注册表）。

``REGISTRY`` 按 ``name`` 登记各 ``Splitter`` 实例，``select(name)`` 取用。新增切法 = new 一个
Splitter 子类并 ``register``，下游粒度/表征/检索零改动（不同 splitter = 不同 profile = 隔离索引，
tools/eval 直接对比召回）。当前成员：toc（实装）、semantic / tree（占位）。
"""
from __future__ import annotations

from splitter.base import Splitter
from splitter.semantic_splitter import SemanticSplitter
from splitter.toc_splitter import TocSplitter
from splitter.tree_splitter import TreeSplitter

DEFAULT_STRATEGY = "toc"

REGISTRY: dict[str, Splitter] = {}


def register(splitter: Splitter) -> None:
    """登记一个切分实例（name 重复则报错，避免静默覆盖）。"""
    if splitter.name in REGISTRY:
        raise ValueError(f"切分策略 name={splitter.name!r} 已注册，勿重复登记")
    REGISTRY[splitter.name] = splitter


def select(name: str) -> Splitter:
    """按名从注册表取已登记的切分策略单例；未注册则报错并列出可选项。"""
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"未知 structure_strategy={name!r}（已注册：{sorted(REGISTRY)}）"
        ) from None


register(TocSplitter())
register(SemanticSplitter())
register(TreeSplitter())
