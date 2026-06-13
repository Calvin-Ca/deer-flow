"""文档切分基类 —— Splitter（PRD §3.1 结构层 / 切分轴的可插拔策略契约）。

一个 Splitter 子类 = **一种「把解析后的扁平元素流组织成检索单元结构」的方法**。它是
结构层的可插拔策略：注册表（``splitter/__init__.py`` 的 ``REGISTRY``）按 ``name``
登记，profile 的 ``structure_strategy`` 决定本次用哪种切法，可 A/B、可消融。

输出 ``SplitResult.nodes`` = list[schema.Node]（即 ``nodes.json`` 单一真值）：
  - 既可以是**保留 parent/child 的多层级树**（TOC 法 / 标题法）；
  - 也可以是 ``parent_id=None`` 的**扁平块**（定长窗 / 语义切）——schema.Node 两者都容。

**与另两轴的关系（三件正交，PRD §3.1）**：切分只产「结构」；
  - 粒度视图 ``view(nodes, level)``（索引期 04）在结构上选「emit 哪一层」——切分换、视图不变；
  - 表征 ``reprs``（阶段 2）对每个节点做投影——切分换、表征不变。
所以换切分方法 = 换 Splitter，下游粒度/表征/检索骨架不动。

**设计底线（PRD §一 核心设计原则）**：建筑规范的多层级**首选以原生目录为骨架**还原
（``TocSplitter``，当前默认/唯一实现）；其余切法服务于「跨文档类适配」（如造价定额表
格文档），不用来在规范上 second-guess 目录。任何切法产出的节点都须带 ``provenance``
回指 MinerU 原始块（可溯源底线）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.parse_profile import ParseProfile


@dataclass
class SplitResult:
    """切分结果。

    字段：
        nodes (list[dict]): 节点列表（schema.Node 形态），落 ``nodes.json`` 单一真值。
        debug_blocks (list[dict] | None): 切分中间产物（落 ``structure.json`` 调试用），
            无中间形态的切法填 None。
    """

    nodes: list[dict]
    debug_blocks: list[dict] | None = None


class Splitter(ABC):
    """文档切分策略基类：解析后的统一元素流 → 节点结构（SplitResult）。

    子类约定：
        类属性 ``name`` (str)：策略名，注册表键（= profile.structure_strategy 取值）。
        方法 ``split(elements, *, standard_id, profile, ...)``：产 SplitResult。
    """

    name: str

    @abstractmethod
    def split(
        self,
        elements: list[dict],
        *,
        standard_id: str,
        profile: "ParseProfile",
        source_file: str = "",
        version: str = "",
        effective_date: str = "",
        status: str = "active",
    ) -> SplitResult:
        """把解析后的统一元素流切分成节点结构。

        参数：
            elements (list[dict]): FormatAdapter.adapt() 产出的统一元素列表（阶段 0→1）。
            standard_id (str): 规范唯一标识，逐节点盖章。
            profile (ParseProfile): 解析配置（切法可按需读其字段；TOC 法用于占位/扩展）。
            source_file (str): 原始 content_list.json 路径（写入节点 provenance 溯源）。
            version / effective_date / status (str): 规范级元数据，写入每个节点。
        返回：
            SplitResult: nodes（节点列表）+ debug_blocks（可选中间产物）。
        """
        raise NotImplementedError
