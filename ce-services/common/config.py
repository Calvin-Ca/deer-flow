"""任务层配置 —— LLM 与知识服务地址（env 可覆盖）。

任务层不依赖 ce-code/retrieval，因此不复用 ``retrieval.config.DEFAULTS``；
这里只保留任务服务真正需要的三项：调 vLLM 的 LLM_URL/MODEL，以及知识服务地址。
embedding / Milvus 等检索依赖归知识服务（:8100）所有，任务层一概不碰。
"""
from __future__ import annotations

import os

# Qwen3-8B vLLM（判定 / 生成 / 反思直接调）
LLM_URL = os.environ.get("BCRAG_LLM_URL", "http://localhost:8099")
LLM_MODEL_ID = os.environ.get("BCRAG_LLM_MODEL_ID", "qwen3-8b")

# ce-code 知识服务（裸检索原语 /search /expand /clause）
KNOWLEDGE_URL = os.environ.get("BCRAG_KNOWLEDGE_URL", "http://localhost:8100")
