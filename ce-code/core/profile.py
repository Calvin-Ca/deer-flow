"""ParseProfile —— 解析/索引流水线的**配置契约**（贯穿全层，非 IR）。

一份 profile 描述「一次 build 用哪种解析模型、哪种切法、挂哪些表征、按哪层粒度建索引」，
命名隔离、多套共存，产物可追溯到配置（``data/structured/{std}/{profile}/`` 与
``data/vector_store/{std}/{profile}/``）。各阶段共读同一份 profile：
- 解析层（parser/）看 ``parser_strategy``（选哪种解析模型；缺省 mineru）。
- 结构层（splitter/）看 ``structure_strategy``（选哪种切分；缺省 toc），并把 ``toc_max_depth``
  （切到第几级目录）、``subsplit``（是否在目录层之下按编号细分）透传给 ``split``。这两个是
  **切分期**深度闸（决定树建到哪一层）；与索引期的 ``index_granularity`` 视图选层互补、勿混。
- 表征层（feature/）看 ``features``：启用哪些「语义投影」。
- 索引层（index/）看 ``index_granularity`` 选粒度视图 emit、看 ``small_to_big`` 决定检索期
  是否上探父节点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# 免费表征（无 LLM / 无新依赖）：raw 返回原文、sparse BM25 词项、dense 向量、context_aug 拼祖先链。
# table_struct / modal / condition / keyword / graph / summary / questions 是后续批次追加项。
DEFAULT_FEATURES: tuple[str, ...] = ("raw", "sparse", "dense", "context_aug")


@dataclass
class ParseProfile:
    """解析/索引流水线配置。

    字段：
        name              配置名 = 产物子目录（同一规范多 profile 隔离做 A/B）。
        terminal_stage    流水线终止阶段：structure（仅切分，产 chunks.json）/ reprs（+ 挂表征）
                          / index（+ 选粒度建向量 + BM25）。
        parser_strategy   解析模型名（parser/ factory 键；缺省 mineru）。
        structure_strategy 切分策略名（splitter/ factory 键；缺省 toc=原生目录多层级）。
        toc_max_depth     切到第几级目录（号段层级，1=章/2=节/3=条…）；缺省 None=全目录深度。
                          超过该层级的目录条目/标题不单独建节点，正文并入最近祖先。透传给 split。
        subsplit          目录层之下按编号细分策略，透传给 split：缺省 "none"（树严格镜像目录，
                          不细分）/ "number"（目录骨架之下按编号号段再切出更细的编号子节点——节/
                          条/款/项皆可，不专指条；与 toc_max_depth 正交）。
        index_granularity 阶段 3 在树上选哪层粒度视图入索引（section | clause | paragraph）。
                          **不切树**，换粒度只重跑阶段 3（与切分期 toc_max_depth/subsplit 互补）。
        features          启用的表征 kind 列表（FeatureKind 子集），缺省 DEFAULT_FEATURES。
        small_to_big      检索期是否靠 parent_id 上探父节点、回补整条/整节上下文。
    """

    name: str = "default"
    terminal_stage: Literal["structure", "reprs", "index"] = "index"
    parser_strategy: str = "mineru"
    structure_strategy: str = "toc"
    toc_max_depth: int | None = None
    subsplit: str = "none"
    index_granularity: Literal["section", "clause", "paragraph"] = "clause"
    features: list[str] = field(default_factory=lambda: list(DEFAULT_FEATURES))
    small_to_big: bool = True
