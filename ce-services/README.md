# 任务层 · CostAgent（ce-services）

知识层（`../ce-code`，深圳房建组价知识库 :8100）的**任务服务**。任务层是知识服务的**纯 HTTP
客户端**——不 `import retrieval`、不连 Milvus / 向量库 / PG，只打知识服务取数原语，再叠加
**LLM 选码 + 确定性组价 + HITL 红线**的任务逻辑。

> **2026-06-18 收敛**：项目聚焦深圳房建组价。**CostAgent（构件 → 选码 → 组价）为唯一主线**；
> 原规范 RAG 消费方 `/qa`（code-qa skill）、`/compliance`（compliance-check skill）已退役
> （知识层后端 /search /clause 已删）。需求/设计见 `PRD.md`，进度见 `TODO.md`。

## 拓扑

```
ce-code 知识服务 :8100  (组价取数原语 /bill/match /price/compose /quota；retrieval+PG+Milvus 唯一 owner)
        ▲ HTTP
        │
ce-services 任务服务 :8101  (CostAgent)
  POST /cost/compose = bill_match 召回候选 → LLM 选码 → price_compose 组价   ← P1 选码闭环（建设中）
```

端到端：**构件描述 → `bill_match` 拿清单候选 → CostAgent 内 LLM 在候选内选码（红线：只建议
不定稿、低置信 HITL 复核）→ `price_compose` 组价**。知识层只召回候选 + 取数；选码（Top-1）
本就归任务层（PRD §6）。

| 端点 | 职责 | 状态 |
|---|---|---|
| `POST /cost/compose` | 构件描述 → 候选召回 → LLM 选码 → 组价 | 🟡 P1 建设中 |
| `GET /health` | 健康检查（含 knowledge_url / llm_url） | ✅ |

## 目录

```
ce-services/
├── main.py                 # 统一入口 :8101（cost 路由 P1 就位后挂载）
├── pyproject.toml          # 独立 uv 项目（仅 fastapi/uvicorn/requests/pydantic）
├── common/
│   ├── config.py           # LLM_URL / LLM_MODEL_ID / KNOWLEDGE_URL（env 可覆盖）
│   ├── llm.py              # 裸 Qwen3-8B vLLM JSON 直调（call_qwen3，选码复用）
│   └── cost_client.py      # 造价取数 HTTP 客户端：bill_match / price_compose / quota
└── cost/                   # P1 建设中：selection.py 选码 / orchestration.py 串链 / router.py 端点
```

## 启动（服务器）

全栈需 **2 个进程**：知识服务 :8100（先起，任务层依赖它）+ 任务服务 :8101。

```bash
cd ce-code     && uv run python -m retrieval.server   # ① :8100 知识服务（必须先起）
cd ce-services && uv sync && uv run python main.py    # ② :8101 任务服务（CostAgent）
# 或 uvicorn 形式：uv run uvicorn main:app --host 0.0.0.0 --port 8101
```

> 后台常驻**勿用 nohup**（服务器 Exit 125 静默失败），用 `setsid` 或 tmux。

健康检查：
```bash
curl http://localhost:8100/health   # {"status":"ok","service":"retrieval",...}
curl http://localhost:8101/health   # {"status":"ok","service":"tasks","routes":[...],...}
```

## 配置（env 覆盖，见 common/config.py）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BCRAG_KNOWLEDGE_URL` | `http://localhost:8100` | 知识服务地址 |
| `BCRAG_LLM_URL` | `http://localhost:8099` | Qwen3-8B vLLM 地址 |
| `BCRAG_LLM_MODEL_ID` | `qwen3-8b` | LLM model_id |

## 红线（输出侧）

- **选码 `need_review`**：低置信选码只建议不定稿，转 HITL 人工复核
- **`no_source` 不杜撰**：未命中信息价的工料机透传缺口，不编价
- **`spec` 必填**：调用前须确认国标版本（2013/2024），按版本隔离取数
- **LLM 不算钱**：组价/单价为确定性公式（P2），LLM 只做选码
