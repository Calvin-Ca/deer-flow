"""解析层工厂 —— 按 ``profile.parser_strategy`` 取解析模型（可插拔注册表）。

``REGISTRY`` 按 ``name`` 登记各 ``Parser`` 实例，``select(name)`` 取用。新增解析模型 = new 一个
Parser 子类并 ``register``，下游切分/表征/索引/检索零改动。当前成员：mineru（实装）、
unstructured（占位）。
"""
from __future__ import annotations

from parser.base import Parser
from parser.mineru import MineruParser
from parser.unstructured import UnstructuredParser

DEFAULT_PARSER = "mineru"

REGISTRY: dict[str, Parser] = {}


def register(parser: Parser) -> None:
    """登记一个解析实例（name 重复则报错，避免静默覆盖）。"""
    if parser.name in REGISTRY:
        raise ValueError(f"解析模型 name={parser.name!r} 已注册，勿重复登记")
    REGISTRY[parser.name] = parser


def select(name: str) -> Parser:
    """按名从注册表取已登记的解析工具单例；未注册则报错并列出可选项。"""
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"未知 parser_strategy={name!r}（已注册：{sorted(REGISTRY)}）"
        ) from None


register(MineruParser())
register(UnstructuredParser())
