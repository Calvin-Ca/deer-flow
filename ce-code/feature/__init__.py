"""表征层（feature/）—— 阶段 2：Chunk 树 → 挂多表征（PRD §3.1 / §3.2）。

承旧 ``reprs/``。把节点投影成多种「可被检索的样子」（``ChunkFeature``），原地挂到 ``Chunk.features``。
可插拔注册表见 ``pipeline``（按 ``profile.features`` 选启用）。当前实装免费 4 项（raw / sparse /
dense / context_aug），占位 keyword / graph。
"""
from __future__ import annotations

from feature.base import Feature
from feature.pipeline import DEFAULT_ENABLED, REGISTRY, attach, enrich, register

__all__ = ["Feature", "enrich", "attach", "register", "REGISTRY", "DEFAULT_ENABLED"]
