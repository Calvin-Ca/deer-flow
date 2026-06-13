# 建筑规范 RAG · 任务层（ce-services）

知识层（`../ce-code`）的**任务服务**集合。所有任务服务是知识服务（:8100）的**纯 HTTP
客户端**——不 `import retrieval`、不连 Milvus / 向量库，只打知识服务 `/search` 拿裸条款，
再叠加各自的任务逻辑（生成 / 编排）。

## 拓扑

```
ce-code 知识服务 :8100  (检索原语 /search /expand /clause；retrieval + rerank 唯一 owner)
        ▲ HTTP /search
        │
ce-services 任务服务 :8101  (qa + compliance 共进程)
  /qa         = search + 生成        (code-qa skill 后端)
  /compliance = 参数提取→并行检索→逐维度判定→反思  (compliance-check skill 后端)
```

| 端点 | 职责 | skill |
|---|---|---|
| `POST /qa` | 检索 + Qwen3 结构化生成 | `code-qa` |
| `POST /compliance` | 项目级合规检查端到端编排 | `compliance-check` |
| `GET /health` | 健康检查（含 knowledge_url / llm_url） | — |

## 目录

```
ce-services/
├── main.py                 # 统一入口：:8101，include qa + compliance 路由
├── pyproject.toml          # 独立 uv 项目（仅 fastapi/uvicorn/requests/pydantic）
├── common/
│   ├── config.py           # LLM_URL / LLM_MODEL_ID / KNOWLEDGE_URL（env 可覆盖）
│   └── knowledge_client.py # 知识服务 HTTP 客户端：search / expand / get_clause
├── qa/
│   ├── router.py           # APIRouter：/qa 端点逻辑
│   ├── server.py           # 独立启动入口（单独测试用，生产用 main.py）
│   └── generation.py       # 检索结果 → 结构化回答（强制引用/强条区分/无依据拒答）
└── compliance/
    ├── router.py           # APIRouter：/compliance 端点逻辑
    ├── server.py           # 独立启动入口（单独测试用，生产用 main.py）
    ├── orchestration.py    # 端到端流水线（检索经 knowledge_client.search）
    ├── params.py           # 自由文本 → 结构化建筑参数
    └── queries.py          # 结构化参数 → 合规检索查询矩阵
```

## 启动（服务器，两种方式二选一）

全栈需 **2 个进程**：知识服务 :8100 + 任务服务 :8101（先起知识服务，任务层依赖它）。

### 方式 A — Docker 一键全栈（推荐）

`docker/ce-services/docker-compose.yaml` 用 `include` 组合知识服务，`depends_on: service_healthy` 保证顺序。

```bash
cp docker/ce-services/.env.example docker/ce-services/.env   # 首次：填 DATA_DIR
docker compose -f docker/ce-services/docker-compose.yaml up -d
# 仅知识服务：docker compose -f docker/ce-code/docker-compose.yaml up -d
```

### 方式 B — 直接运行（两进程）

```bash
cd ce-code     && uv run python -m retrieval.server   # ① :8100 知识服务（必须先起）
cd ce-services && uv sync && uv run python main.py   # ② :8101 任务服务（qa + compliance）
# 或 uvicorn 形式：uv run uvicorn main:app --host 0.0.0.0 --port 8101
```

> 后台常驻**勿用 nohup**（stone 服务器 Exit 125 静默失败），用 `setsid` 或 tmux。

健康检查：
```bash
curl http://localhost:8100/health   # {"status":"ok","service":"retrieval",...}
curl http://localhost:8101/health   # {"status":"ok","service":"tasks","routes":["/qa","/compliance"],...}
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
`../ce-code` 的 `python -m tools.eval` 卡强条召回率（⚠️ 仍 v1 口径，T10 待改）。
