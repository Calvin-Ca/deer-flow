"""切分层基类 —— Splitter（PRD §3.1 结构层 / 切分轴的可插拔策略契约）。

一个 Splitter 子类 = **一种「把 Document(IR) 组织成 Chunk 树」的方法**。注册表
（``splitter/factory.py``）按 ``name`` 登记，``profile.structure_strategy`` 决定本次用哪种切法。

输出 ``SplitResult.chunks`` = list[Chunk]（即 ``chunks.json`` 单一真值）：
  - 既可是**保留 parent/child 的多层级树**（TOC 法）；
  - 也可是 ``parent_id=None`` 的**扁平块**（定长窗 / 语义切）——Chunk 两者都容。

**三件正交（PRD §3.1）**：切分只产「结构」；粒度视图（index 层 ``view(chunks, granularity)``）
在结构上选 emit 哪一层、表征层（feature/）对每个 Chunk 做投影——换切分，下游粒度/表征/检索不动。

**设计底线**：建筑规范多层级**首选以原生目录为骨架**还原（``TocSplitter``，当前默认）；任何切法
产出的 Chunk 都须带 ``provenance`` 回指原始块（可溯源底线）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.chunk import Chunk
from core.document import Document


@dataclass
class SplitResult:
    """切分结果。

    字段：
        chunks (list[Chunk]): Chunk 树（落 ``chunks.json`` 单一真值）。
        debug_blocks (list[dict] | None): 切分中间产物（落 ``structure.json`` 调试用），
            无中间形态的切法填 None。
    """

    chunks: list[Chunk] = field(default_factory=list)
    debug_blocks: list[dict] | None = None


class Splitter(ABC):
    """文档切分策略基类：Document(IR) → Chunk 树（SplitResult）。

    子类约定：
        类属性 ``name`` (str)：策略名，注册表键（= profile.structure_strategy 取值）。
        方法 ``split(document, *, max_depth, subsplit)``：产 SplitResult。

    **切分深度 vs 索引粒度（勿混）**：``max_depth`` / ``subsplit`` 是**切分期**参数，决定树里
    **存在到哪一层**（结构）；索引期的 ``index_granularity``（``view(chunks, granularity)``）则在
    已建好的树上**选哪层 emit**（视图）。二者互补：先用 max_depth/subsplit 决定建多深，再用粒度
    视图选取其中一层入索引——换切分深度只重跑切分，换粒度只重跑索引。
    """

    name: str

    @abstractmethod
    def split(self, document: Document, *, max_depth: int | None = None,
              subsplit: str = "none") -> SplitResult:
        """把 Document 切分成 Chunk 树结构。

        参数：
            document (Document): 解析层产物（统一元素流；含 standard_id / source_file / blocks）。
            max_depth (int | None): 切分深度上限（按 node_path 号段层级，1=章/2=节/3=条…）；超过
                该层级的目录条目/标题不单独建节点，正文并入最近祖先。None=不限（全目录深度）。
            subsplit (str): 目录层**之下**按编号细分的策略——"none"（不细分，树严格镜像目录）/
                "number"（按编号号段再切出更细的编号子节点，节/条/款/项皆可，不专指条）。
                子类可只支持其中部分取值并校验。
        返回：
            SplitResult: chunks（Chunk 列表）+ debug_blocks（可选中间产物）。
        """
        raise NotImplementedError
