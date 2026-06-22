"""表征层（feature/）—— 阶段 2：Chunk 树 → 挂多表征（PRD §3.1 / §3.2）。

承旧 ``reprs/``。把节点投影成多种「可被检索的样子」（``ChunkFeature``），汇成 **sidecar**
（``node_path → {kind: ChunkFeature}``）——表征**不挂 Chunk**，索引期现算（见 ``pipeline.build_features``）。
可插拔注册表见 ``pipeline``（按 ``profile.features`` 选启用）。当前实装免费 4 项（raw / sparse /
dense / context_aug），占位 keyword / graph。
"""
from __future__ import annotations

from feature.base import Feature
from feature.pipeline import DEFAULT_ENABLED, REGISTRY, build_features, build_one, register

__all__ = ["Feature", "build_features", "build_one", "register", "REGISTRY", "DEFAULT_ENABLED"]
