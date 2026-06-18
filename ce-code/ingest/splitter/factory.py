"""切分层工厂 —— 按 ``profile.structure_strategy`` 取切分策略（可插拔注册表）。

``REGISTRY`` 按 ``name`` 登记各 ``Splitter`` 实例，``select(name)`` 取用。新增切法 = new 一个
Splitter 子类并 ``register``，下游粒度/表征/检索零改动（不同 splitter = 不同 profile = 隔离索引，
tools/eval 直接对比召回）。当前成员：toc（实装）、semantic / tree（占位）。

一、规则/固定类(baseline)
固定长度切分:按 token/字符数硬切。最简单,但会切断语义,几乎只用作对照基线。
滑动窗口 + 重叠:固定长度基础上加 overlap,缓解边界截断。重叠是用冗余换召回完整性。
分隔符递归切分:按 段落→句子→标点 的分隔符层级递归往下切,直到满足目标长度(LangChain RecursiveCharacterTextSplitter 是典型)。比纯固定长度尊重自然边界,工程默认首选之一。

二、结构感知类(强结构文档主力)
文档结构切分:按 Markdown/HTML 标题层级、章节编号切,chunk 对齐文档的逻辑单元。你的规范/定额属于这一族。
布局感知切分:基于 PDF 版面(标题、正文、表格、图)切,依赖 MinerU 这类版面解析的输出。处理表格/多栏/页眉页脚时必须用。
代码感知切分:按函数/类/import 块切,保持代码语义完整(对你不太相关,但同族)。

三、语义类(靠内容而非规则定边界)
语义切分(semantic chunking):对相邻句子算 embedding 相似度,在相似度"断崖"处切。边界跟着语义走,但要先 embedding,成本和延迟更高。
命题切分(propositional):用 LLM 把文本拆成原子命题(一条独立事实一个 chunk)。粒度最细、检索最精准,但 LLM 成本高、可能丢上下文。
主题切分(TextTiling 等):用词汇分布变化探测主题转移点,在话题切换处断。
"""
from __future__ import annotations

from ingest.splitter.base import Splitter
from ingest.splitter.semantic_splitter import SemanticSplitter
from ingest.splitter.toc_splitter import TocSplitter
from ingest.splitter.tree_splitter import TreeSplitter

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
