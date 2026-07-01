"""任务层配置 —— LLM 与知识服务地址（env 可覆盖）。

任务层不依赖 ce-code/retrieval，因此不复用 ``retrieval.config.DEFAULTS``；
这里只保留任务服务真正需要的三项：调 vLLM 的 LLM_URL/MODEL，以及知识服务地址。
embedding / Milvus 等检索依赖归知识服务（:8100）所有，任务层一概不碰。
"""
from __future__ import annotations

import os
from pathlib import Path

# Qwen3-8B vLLM（桶 A：判定 / 生成 / 反思 / 选码直接调；延迟敏感路径）
LLM_URL = os.environ.get("BCRAG_LLM_URL", "http://localhost:8099")
LLM_MODEL_ID = os.environ.get("BCRAG_LLM_MODEL_ID", "qwen3-8b")

# ── 桶 B（Qwen3-32B vLLM）：真·推理 + 选码消歧（AGENT_DEV §9.3 模型分层，T-A5 接线）──
# 用于：① 复合拆解/综合（routing/orchestrator）；② 选码候选消歧（cost/selection.select_code）——
#   「8b 最不可靠、选错最贵、非 2s 直配路径，可承受 32b 延迟」（§9.3）。取价/生成/直配/澄清仍走 8b。
# 部署：32b 在另一台服务器（config.yaml 记 172.19.2.2:8001/v1，model=/models/Qwen3-32B-AWQ），
#   服务器成对设 BCRAG_ORCH_LLM_URL + BCRAG_ORCH_LLM_MODEL_ID 指过去即升桶 B。
# **未部署 32b 时 URL 与 model 成对回落 8b**（避免「8b 端点 + 32b model id」不匹配打不通）：
#   仅当显式设了 32b URL 才把 model 缺省成 qwen3-32b，否则回落 8b model —— 故本地/未部署默认
#   一切照旧走 8b（选码不受影响），GPU 一松、env 一设即升 32b，零风险接线。
_ORCH_LLM_URL_ENV = os.environ.get("BCRAG_ORCH_LLM_URL")
ORCH_LLM_URL = _ORCH_LLM_URL_ENV or LLM_URL
ORCH_LLM_MODEL_ID = os.environ.get(
    "BCRAG_ORCH_LLM_MODEL_ID",
    "qwen3-32b" if _ORCH_LLM_URL_ENV else LLM_MODEL_ID,
)

# 选码消歧走同一桶 B；单列句柄便于独立指向另一 32b 实例，默认同编排器 32b（成对回落 8b）。
SELECT_LLM_URL = os.environ.get("BCRAG_SELECT_LLM_URL", ORCH_LLM_URL)
SELECT_LLM_MODEL_ID = os.environ.get("BCRAG_SELECT_LLM_MODEL_ID", ORCH_LLM_MODEL_ID)

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

# ── 选码置信外部校准（路 2：用 bill_match cosine score 客观校准 LLM 自报置信）──
# 背景：Qwen3-8B 自报 confidence 几乎恒 0.95（无区分度），致 confidence<阈值 兜底与 HITL 闸从不触发。
# 改用候选 cosine score 算客观置信，与自报「保守取 min」（只拉低、不抬高）。三参数 env 可调，
# **默认偏保守（多停），需按 benchmark 暴露的真实 score 分布精调**（cosine 量纲随 embedder 而变）。
# 绝对贴合度：选中候选 cosine < FLOOR 视作不贴切（→0）、> CEIL 视作贴切（→1）、之间线性内插。
CE_SELECT_SCORE_FLOOR = float(os.environ.get("CE_SELECT_SCORE_FLOOR", "0.35"))
CE_SELECT_SCORE_CEIL = float(os.environ.get("CE_SELECT_SCORE_CEIL", "0.65"))
# 间距：选中候选比次优候选高出 ≥ MARGIN_FULL 即视作无歧义（→1）；为负（逆检索而选）→ 0。
CE_SELECT_MARGIN_FULL = float(os.environ.get("CE_SELECT_MARGIN_FULL", "0.10"))
