# 任务层 · Norm-QA + CostAgent（ce-services）

知识层（`../ce-code`，:8100）的**任务服务**。任务层是知识服务的**纯 HTTP 客户端**——不 `import
retrieval`、不连 Milvus / PG，只打知识服务原语，再叠加生成 / 选码逻辑。两条主线共进程（:8101）：

> - **Norm-QA（造价规范问答，当前优先）**：打 :8100 `/search` 检索造价规范条文 → Qwen3 带引用作答。
> - **CostAgent（构件→选码→组价，P1 暂停）**：打 :8100 `/bill/match` 等取组价数据 → LLM 选码 → 组价。
>
> 防火 RAG 消费方 `/qa` `/compliance` 已退役。需求/设计见 `PRD.md`，进度见 `TODO.md`。

## 拓扑

```
ce-code 知识服务 :8100  (统一入口 service.knowledge_api：规范条文检索 /search /expand /clause
                         + 组价取数 /bill/match /price/compose /quota；retrieval+PG+Milvus 唯一 owner)
        ▲ HTTP
        │
ce-services 任务服务 :8101
  POST /norm/qa      = /search 检索规范条文 → Qwen3 带引用作答            ← 当前优先（建设中）
  POST /cost/compose = bill_match 候选 → LLM 选码 → price_compose 组价     ← P1（暂停）
```

| 端点 | 职责 | 状态 |
|---|---|---|
| `POST /norm/qa` | 造价规范条文检索 + Qwen3 带引用作答 | ⏳ 建设中 |
| `POST /cost/compose` | 构件描述 → 候选召回 → LLM 选码 → 组价 | ⏸ P1 暂停 |
| `GET /health` | 健康检查（含 knowledge_url / llm_url） | ✅ |

## 目录

```
ce-services/
├── main.py                 # 统一入口 :8101（挂 norm 路由；cost 路由 P1 恢复后挂）
├── pyproject.toml          # 独立 uv 项目（仅 fastapi/uvicorn/requests/pydantic）
├── common/
│   ├── config.py           # LLM_URL / LLM_MODEL_ID / KNOWLEDGE_URL（env 可覆盖）
│   ├── llm.py              # 裸 Qwen3-8B vLLM JSON 直调（call_qwen3，两线复用）
│   ├── knowledge_client.py # 规范条文检索 HTTP 客户端：search / expand / get_clause
│   └── cost_client.py      # 造价取数 HTTP 客户端：bill_match / price_compose / quota
├── norm/                   # Norm-QA：generation.py 带引用生成 / router.py /norm/qa 端点
└── cost/                   # P1 暂停：selection.py 选码 / orchestration.py 串链 / router.py 端点
```

## 启动（服务器）

全栈需 **2 个进程**：知识服务 :8100（先起，任务层依赖它）+ 任务服务 :8101。

```bash
cd ce-code     && uv run python -m service.knowledge_api   # ① :8100 知识服务（必须先起）
cd ce-services && uv sync && uv run python main.py         # ② :8101 任务服务
# 或 uvicorn 形式：uv run uvicorn main:app --host 0.0.0.0 --port 8101
```

调用示例（Norm-QA，须带 `standard` 代号）：
```bash
curl -s -X POST http://localhost:8101/norm/qa -H 'Content-Type: application/json' -d '{"query":"满堂脚手架工程量怎么计算","standard":"gb50854-2024","top_k":10}'
```

> 后台常驻**勿用 nohup**（服务器 Exit 125 静默失败），用 `setsid` 或 tmux。

健康检查：
```bash
curl http://localhost:8100/health   # {"status":"ok","service":"retrieval",...}
curl http://localhost:8101/health   # {"status":"ok","service":"tasks","routes":["/norm/qa"],...}
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
