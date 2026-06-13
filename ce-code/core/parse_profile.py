"""parse_profile —— 解析/索引流水线的**配置契约**（对应 PRD §3.2）。

一份 profile 描述「一次 build 用哪种切法、挂哪些表征、按哪层粒度建索引」，命名隔离、
多套共存，产物可追溯到配置（``data/structured/{std}/{profile}/`` 与
``data/vector_store/{std}/{profile}/``）。三个阶段共读同一份 profile：
- **结构层（02）** 看 ``structure_strategy``（选哪种切分策略，见 splitters/）+
  ``terminal_stage``（到 structure 即止）；切分不依赖粒度，粒度是索引期在结构上选的
  视图，不是切树（PRD §3.2）。
- **表征层（reprs runner，T5/T8）** 看 ``reprs``：启用哪些「语义投影」。
- **索引层（04，T4）** 看 ``index_granularity`` 选粒度视图 emit、看 ``small_to_big``
  决定检索期是否上探父节点。

设计转向（2026-06-12）后旧字段 ``chunk_granularity`` / ``enrichment`` /
``structure_depth`` 已废弃——粒度不再是「切树」而是 ``view(tree, level)``（见 view.py），
增强深度被「表征注册表」取代。本模块独立于 02（数字前缀文件不可 import），供
02 / reprs runner / 04 共同 import。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 免费表征（无 LLM / 无新依赖）：raw 返回原文、sparse BM25、dense 向量、context_aug 拼祖先链。
# table_struct / modal / condition / summary / questions 是后续批次（第 4 步）追加项。
DEFAULT_REPRS: tuple[str, ...] = ("raw", "sparse", "dense", "context_aug")


@dataclass
class ParseProfile:
    """解析/索引流水线配置（PRD §3.2 parse_profile 配置块）。

    参数：
        name (str): 配置名，作产物子目录（data/structured/{std}/{name}/、
            data/vector_store/{std}/{name}/），同一规范多 profile 隔离共存做 A/B。
        terminal_stage (str): 流水线终止阶段——
            structure 仅切分（阶段 1，产 nodes.json）；
            reprs     切分 + 挂表征（阶段 2，产带 reprs 的节点库）；
            index     继续到阶段 3（按 index_granularity 选视图 emit → 向量 + BM25）。
        structure_strategy (str): 切分策略名（阶段 1 用哪个 splitter，见 splitters/
            REGISTRY）。缺省 "toc"——基于 PDF 原生目录的多层级切分（建筑规范首选，
            PRD §一 核心设计原则 1）。换切法只重跑阶段 1，下游粒度/表征不动。
        index_granularity (str): 阶段 3 在节点树上选哪层粒度视图入索引——
            section | clause | paragraph（见 view.view）。**不切树**，树本身完整保留，
            换粒度只重跑阶段 3。
        reprs (list[str]): 启用的表征 kind 列表（ReprKind 子集），缺省 DEFAULT_REPRS。
        small_to_big (bool): 检索期是否靠 parent_id 上探父节点、回补整条/整节上下文
            （小粒度匹配、大粒度返回，PRD §3.1 ③）。
    返回：
        无（dataclass，实例化后传入各阶段）。
    """

    name: str = "default"
    terminal_stage: Literal["structure", "reprs", "index"] = "index"
    structure_strategy: str = "toc"  # 切分策略名（splitters/ REGISTRY 键，缺省原生目录多层级）
    index_granularity: Literal["section", "clause", "paragraph"] = "clause"
    reprs: list[str] = field(default_factory=lambda: list(DEFAULT_REPRS))
    small_to_big: bool = True
