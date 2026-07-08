"""任务层配置 —— LLM 与知识服务地址（env 可覆盖）。

任务层不依赖 ce-code/retrieval，因此不复用 ``retrieval.config.DEFAULTS``；
这里只保留任务服务真正需要的三项：调 vLLM 的 LLM_URL/MODEL，以及知识服务地址。
embedding / Milvus 等检索依赖归知识服务所有，任务层一概不碰。
"""
from __future__ import annotations

import os
from pathlib import Path

# Qwen3-8B vLLM（桶 A：判定 / 生成 / 反思 / 选码直接调；延迟敏感路径）
LLM_URL = os.environ.get("BCRAG_LLM_URL", "http://localhost:8099")
LLM_MODEL_ID = os.environ.get("BCRAG_LLM_MODEL_ID", "qwen3-8b")

# ── 桶 B（Qwen3-32B vLLM）：真·推理 + 选码消歧 ──
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

# ce-code 新拆分入口：
#   RAG_URL —— 条文/候选/证据检索（ce-rag，默认 :8100）
#   DB_URL  —— 结构化真值取数（ce-db，默认 :8102）
# 兼容：若未单独配置 BCRAG_RAG_URL / BCRAG_DB_URL，则回退历史单入口 BCRAG_KNOWLEDGE_URL。
_LEGACY_KNOWLEDGE_URL = os.environ.get("BCRAG_KNOWLEDGE_URL")
RAG_URL = os.environ.get("BCRAG_RAG_URL", _LEGACY_KNOWLEDGE_URL or "http://localhost:8100")
DB_URL = os.environ.get("BCRAG_DB_URL", _LEGACY_KNOWLEDGE_URL or "http://localhost:8102")
# 兼容别名：旧代码/脚本仍可能引用 KNOWLEDGE_URL，把它钉到 RAG 入口。
KNOWLEDGE_URL = RAG_URL

# ── 口径默认（§8 块1 / T9-1，PRD §4.0/C-05）：组价/价格缺省口径 = 深圳·2013 ──
# PRD 钉死唯一默认作答口径为深圳·2013（2024 为试行版、非默认）；缺版本**不反问**，直接归一
# 到默认口径并在输出带口径声明（meta.caliber）。
# ⚠️ 数据前置：2013 组价数据（定额/价格/映射）未就绪（supports_compose=False）——默认 2013 时
# /price/compose 会如实 501「数据未就绪」+ 给出路（宁缺毋造，不静默换版本）；/search/bill-match 可用
# （2013 清单向量库已建）。2013 数据入库后本默认无需改代码即完全可用；过渡期若要体验完整组价，
# export CE_COST_DEFAULT_SPEC=2024 一行切换。
COST_DEFAULT_SPEC = os.environ.get("CE_COST_DEFAULT_SPEC", "2013")
COST_DEFAULT_REGION = os.environ.get("CE_COST_DEFAULT_REGION", "深圳")

# ── 意图混合路由：确定性低置信时用 32b 兜底补能力分类 ──
# 确定性 prerouter 命中强信号 → 零延迟直配（多数流量、金标回归）；判 route_confidence="low"
# （落 norm 兜底、只泛词/纯默认、无版本锁）→ 调 routing/intent_fallback.classify_intent（桶 B 32b，
# 复用 ORCH_LLM_*）。**红线闸（EH-03 出界 / caliber 口径 / feature）两条路都确定性、LLM 不碰**；
# LLM 不可达/越界 → fail-safe 跌回确定性。默认开；无 LLM 或要纯确定性 export CE_ROUTE_LLM_FALLBACK=0。
ROUTE_LLM_FALLBACK = os.environ.get("CE_ROUTE_LLM_FALLBACK", "1").lower() not in ("0", "false", "no")

# ── 规范问答联网兜底（§8 块2 / T9-4，PRD FR-K07）：仅 FR-K 开放，FR-P/FR-I 永不联网 ──
# 三道闸在服务端确定性执行（norm/web_fallback.py），弱模型不碰查询口径/可信度筛查。
# 默认开；无外网环境 export CE_NORM_WEB_FALLBACK=0 关闭（回落零召回直接拒答）。
NORM_WEB_FALLBACK = os.environ.get("CE_NORM_WEB_FALLBACK", "1").lower() not in ("0", "false", "no")

# ── HITL 可中断组价图（langgraph）配置 ──
# checkpointer 持久化文件：SqliteSaver 落盘于此，进程重启后会话状态仍在（设计原则 4「可跨会话恢复」）。
# 默认放 ce-services 根下、随 .gitignore 排除；env 可覆盖到别处。
_HITL_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / ".hitl_checkpoints.db")
HITL_CHECKPOINT_DB = os.environ.get("CE_HITL_CHECKPOINT_DB", _HITL_DEFAULT_DB)

# 编码闸置信阈值 τ：select_code 自评 confidence ≥ τ 且无多候选才自动过，否则停闸复核。
# 保守起步偏高（§6「保守起步设高，跑顺了再放松」）。
HITL_CONFIDENCE_TAU = float(os.environ.get("CE_HITL_CONFIDENCE_TAU", "0.75"))

# ── 双阈值门控（PRD §4.4 三段式）：≥τ_high 直配自动过 / [τ_low, τ_high) 停闸人工确认 /
# <τ_low 低置信段（额外提示补充特征描述）。τ_high 承担原单 τ 的「自动过」判据（缺省沿用
# CE_HITL_CONFIDENCE_TAU 以兼容既有部署调参），τ_low 只影响停闸时的分段提示与审计标注。
HITL_TAU_HIGH = float(os.environ.get("CE_HITL_TAU_HIGH", str(HITL_CONFIDENCE_TAU)))
HITL_TAU_LOW = float(os.environ.get("CE_HITL_TAU_LOW", "0.60"))

# ── 选码置信外部校准（路 2：用 bill_match cosine score 客观校准 LLM 自报置信）──
# 背景：Qwen3-8B 自报 confidence 几乎恒 0.95（无区分度），致 confidence<阈值 兜底与 HITL 闸从不触发。
# 改用候选 cosine score 算客观置信，与自报「保守取 min」（只拉低、不抬高）。三参数 env 可调，
# **默认偏保守（多停），需按 benchmark 暴露的真实 score 分布精调**（cosine 量纲随 embedder 而变）。
# 绝对贴合度：选中候选 cosine < FLOOR 视作不贴切（→0）、> CEIL 视作贴切（→1）、之间线性内插。
CE_SELECT_SCORE_FLOOR = float(os.environ.get("CE_SELECT_SCORE_FLOOR", "0.35"))
CE_SELECT_SCORE_CEIL = float(os.environ.get("CE_SELECT_SCORE_CEIL", "0.65"))
# 间距：选中候选比次优候选高出 ≥ MARGIN_FULL 即视作无歧义（→1）；为负（逆检索而选）→ 0。
CE_SELECT_MARGIN_FULL = float(os.environ.get("CE_SELECT_MARGIN_FULL", "0.10"))
