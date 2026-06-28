"""任务层配置 —— LLM 与知识服务地址（env 可覆盖）。

任务层不依赖 ce-code/retrieval，因此不复用 ``retrieval.config.DEFAULTS``；
这里只保留任务服务真正需要的三项：调 vLLM 的 LLM_URL/MODEL，以及知识服务地址。
embedding / Milvus 等检索依赖归知识服务（:8100）所有，任务层一概不碰。
"""
from __future__ import annotations

import os
from pathlib import Path

# Qwen3-8B vLLM（判定 / 生成 / 反思直接调）
LLM_URL = os.environ.get("BCRAG_LLM_URL", "http://localhost:8099")
LLM_MODEL_ID = os.environ.get("BCRAG_LLM_MODEL_ID", "qwen3-8b")

# ce-code 知识服务（裸检索原语 /search /expand /clause）
KNOWLEDGE_URL = os.environ.get("BCRAG_KNOWLEDGE_URL", "http://localhost:8100")

# ── HITL 可中断组价图（langgraph）配置 ──
# checkpointer 持久化文件：SqliteSaver 落盘于此，进程重启后会话状态仍在（设计原则 4「可跨会话恢复」）。
# 默认放 ce-services 根下、随 .gitignore 排除；env 可覆盖到别处。
_HITL_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / ".hitl_checkpoints.db")
HITL_CHECKPOINT_DB = os.environ.get("CE_HITL_CHECKPOINT_DB", _HITL_DEFAULT_DB)

# 编码闸置信阈值 τ：select_code 自评 confidence ≥ τ 且无多候选才自动过，否则停闸复核。
# 保守起步偏高（§6「保守起步设高，跑顺了再放松」）。
HITL_CONFIDENCE_TAU = float(os.environ.get("CE_HITL_CONFIDENCE_TAU", "0.75"))
