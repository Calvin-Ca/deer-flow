# 建筑规范 RAG · 任务层（ce-services）

知识层（`../ce-code`）的**任务服务**集合。每个任务服务是知识服务（:8100）的**纯 HTTP
客户端**——不 `import retrieval`、不连 Milvus / 向量库，只打知识服务 `/search` 拿裸条款，
再叠加各自的任务逻辑（生成 / 编排）。

## 拓扑

```
ce-code 知识服务 :8100  (检索原语 /search /expand /clause；retrieval + rerank 唯一 owner)
        ▲ HTTP /search
        │
   ┌────┴─────────────────────────┐
ce-services/qa :8102            ce-services/compliance :8101
  /qa = search + 生成            /compliance = 参数提取→并行检索→逐维度判定→反思
  (code-qa skill 后端)           (compliance-check skill 后端)
```

| 服务 | 端口 | 端点 | 职责 | skill |
|---|---|---|---|---|
| qa | 8102 | `/qa` `/health` | 检索 + Qwen3 结构化生成 | `code-qa` |
| compliance | 8101 | `/compliance` `/health` | 项目级合规检查端到端编排 | `compliance-check` |

## 目录

```
ce-services/
├── pyproject.toml          # 独立 uv 项目（仅 fastapi/uvicorn/requests/pydantic）
├── common/
│   ├── config.py           # LLM_URL / LLM_MODEL_ID / KNOWLEDGE_URL（env 可覆盖）
│   └── knowledge_client.py # 知识服务 HTTP 客户端：search / expand / get_clause
├── qa/
│   ├── server.py           # :8102 /qa /health
│   └── generation.py       # 检索结果 → 结构化回答（强制引用/强条区分/无依据拒答）
└── compliance/
    ├── server.py           # :8101 /compliance /health
    ├── orchestration.py    # 端到端流水线（检索经 knowledge_client.search）
    ├── params.py           # 自由文本 → 结构化建筑参数
    └── queries.py          # 结构化参数 → 合规检索查询矩阵
```

## 启动（服务器上，常驻）

前置：知识服务 :8100 必须先起（任务层依赖它）。见 `../ce-code/README.md`。

```bash
cd ce-services
uv sync                                # 首次：装 fastapi/uvicorn/requests

uv run python qa/server.py             # qa 服务 :8102
uv run python compliance/server.py     # 合规服务 :8101
# 或 uvicorn 形式： uv run uvicorn qa.server:app --host 0.0.0.0 --port 8102
```

> 后台常驻**勿用 nohup**（stone 服务器 Exit 125 静默失败），用 `setsid` 或 tmux。

健康检查：
```bash
curl http://localhost:8102/health      # {"status":"ok","service":"qa",...}
curl http://localhost:8101/health      # {"status":"ok","service":"compliance",...}
```

## 配置（env 覆盖，见 common/config.py）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BCRAG_KNOWLEDGE_URL` | `http://localhost:8100` | 知识服务地址 |
| `BCRAG_LLM_URL` | `http://localhost:8099` | Qwen3-8B vLLM 地址 |
| `BCRAG_LLM_MODEL_ID` | `qwen3-8b` | LLM model_id |

## 行为等价说明

合规编排的检索从「进程内 `retrieval.engine.search`」改为「HTTP 知识服务 `/search`」。
知识服务 `/search` 内部按 `bm25_top_k = vector_top_k = top_k*2` 调 `retrieval.engine.search`，
与重构前 orchestration 直调参数（`top_k=15, bm25/vector_top_k=30, skip_rerank=True`）
逐字一致，因此检索结果（RRF 合并、引用扩展、强条不截断）保持不变。回归用
`../ce-code/scripts/07_eval.py` 卡强条召回率。
