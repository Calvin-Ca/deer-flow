# ce-rag / ce-db / ce-task 服务器运行级联调手册

适用范围：
- `ce-code/service/rag_api.py` -> `:8100` -> `ce-rag`
- `ce-code/service/db_api.py` -> `:8102` -> `ce-db`
- `ce-services/main.py` -> `:8101` -> `ce-task`

目标：
- 先确认 3 个进程本身可启动。
- 再确认 `ce-rag` / `ce-db` 的 REST 原语可用。
- 再确认 3 个 `/mcp` 端点能被 MCP 客户端发现工具。
- 最后确认任务层链路已经切到 `ce-rag` + `ce-db`。

## 1. 环境前提

- 服务器已拉到包含提交 `d8c4a9f0` 及之后文档更新的代码。
- `ce-code` 与 `ce-services` 都已执行过 `uv sync`。
- PostgreSQL / Milvus / rerank / LLM 等底层依赖已按服务器现状可用。
- 若 gateway/前端要一起验，`extensions_config.json` 必须已包含 `ce-rag` / `ce-db` / `ce-task`。

建议先看当前配置：

```bash
git rev-parse HEAD
grep -n '"ce-rag"\|"ce-db"\|"ce-task"' extensions_config.json
```

## 2. 启动顺序

必须先起知识层，再起任务层。

```bash
cd ce-code
uv run python -m service.rag_api
```

新开一个终端：

```bash
cd ce-code
uv run python -m service.db_api
```

再开一个终端：

```bash
cd ce-services
uv run python main.py
```

如果要后台常驻，优先用 `tmux` 或 `setsid`，不要用 `nohup`。

## 3. 健康检查

三个健康检查都应返回 `status=ok`。

```bash
curl -s http://127.0.0.1:8100/health | jq
curl -s http://127.0.0.1:8102/health | jq
curl -s http://127.0.0.1:8101/health | jq
```

期望重点：
- `:8100/health` 返回 `service: "ce-rag"`，并带 `ready_standards`。
- `:8102/health` 返回 `service: "ce-db"`，并带 PG `target`。
- `:8101/health` 返回 `rag_url` 指向 `:8100`、`db_url` 指向 `:8102`。

若 `:8101/health` 里仍只看到旧 `knowledge_url` 但没有 `rag_url` / `db_url`，说明任务层不是新代码。

## 4. REST 冒烟测试

### 4.1 ce-rag 条文检索

```bash
curl -s http://127.0.0.1:8100/search/clause \
  -H 'Content-Type: application/json' \
  -d '{
    "query":"满堂脚手架工程量怎么计算",
    "standard":"gb50854-2024",
    "top_k":5
  }' | jq
```

期望重点：
- 有 `clauses`。
- 同时有新字段 `evidence`。
- `service` 不必显式返回，但响应结构不能报 404/500。

### 4.2 ce-rag 清单候选召回

```bash
curl -s http://127.0.0.1:8100/search/bill-match \
  -H 'Content-Type: application/json' \
  -d '{
    "description":"C30现浇钢筋混凝土矩形柱",
    "spec":"2024",
    "top_k":5
  }' | jq
```

期望重点：
- 有候选结果。
- 记下首个候选 `code`，后续给 `ce-db` 做串链验证。

### 4.3 ce-db 清单真值查询

优先使用上一步拿到的 `code`。下面用 `$CODE` 表示：

```bash
CODE=把上一步首个候选编码填这里
curl -s "http://127.0.0.1:8102/bill/$CODE?spec=2024" | jq
```

期望重点：
- 直接命中一条清单真值。
- 这里是 `/bill/{code}`。

### 4.4 ce-db 组价取数

```bash
CODE=把上一步首个候选编码填这里
curl -s "http://127.0.0.1:8102/price/compose/%E6%B7%B1%E5%9C%B3/$CODE?spec=2024" | jq
```

期望重点：
- 若 2024 组价数据已就绪，应返回定额与工料机明细。
- 若出现业务上的 `no_source`，这是允许的，表示缺价透传，不是系统故障。
- 若错误路径也能通，说明服务器可能还跑着旧入口，不符合本次拆分目标。

### 4.5 ce-services 任务层直测

规范问答：

```bash
curl -s http://127.0.0.1:8101/norm/qa \
  -H 'Content-Type: application/json' \
  -d '{
    "query":"现浇混凝土矩形柱按什么计量",
    "standard":"gb50854-2024",
    "top_k":10
  }' | jq
```

组价：

```bash
curl -s http://127.0.0.1:8101/cost/compose \
  -H 'Content-Type: application/json' \
  -d '{
    "description":"C30现浇钢筋混凝土矩形柱",
    "spec":"2024",
    "region":"深圳"
  }' | jq
```

期望重点：
- `norm/qa` 应返回引用条文，不是空回答。
- `cost/compose` 应出现 `selection`，并且 `price` 来自 `ce-db`。
- 若任务层报错里出现旧文案只指向 `知识服务 :8100`，优先怀疑服务没重启到新代码。

## 5. MCP 端点检查

这一步不是验证业务结果，而是验证 agent 能不能发现工具。

### 5.1 initialize

`ce-rag`：

```bash
curl -s http://127.0.0.1:8100/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc":"2.0",
    "id":"init-rag",
    "method":"initialize",
    "params":{
      "protocolVersion":"2025-03-26",
      "capabilities":{},
      "clientInfo":{"name":"curl","version":"0.0.0"}
    }
  }' | jq
```

`ce-db`：

```bash
curl -s http://127.0.0.1:8102/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc":"2.0",
    "id":"init-db",
    "method":"initialize",
    "params":{
      "protocolVersion":"2025-03-26",
      "capabilities":{},
      "clientInfo":{"name":"curl","version":"0.0.0"}
    }
  }' | jq
```

`ce-task`：

```bash
curl -s http://127.0.0.1:8101/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc":"2.0",
    "id":"init-task",
    "method":"initialize",
    "params":{
      "protocolVersion":"2025-03-26",
      "capabilities":{},
      "clientInfo":{"name":"curl","version":"0.0.0"}
    }
  }' | jq
```

期望重点：
- `serverInfo.name` 分别是 `ce-rag` / `ce-db` / `ce-task`。

### 5.2 tools/list

`ce-rag`：

```bash
curl -s http://127.0.0.1:8100/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc":"2.0",
    "id":"tools-rag",
    "method":"tools/list",
    "params":{}
  }' | jq
```

期望至少看到这些工具名：
- `search_clause`
- `expand_clause_refs`
- `get_clause`
- `match_bill_item`
- `search_aux_table`
- `search_price_rule`
- `retrieve_evidence`

`ce-db`：

```bash
curl -s http://127.0.0.1:8102/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc":"2.0",
    "id":"tools-db",
    "method":"tools/list",
    "params":{}
  }' | jq
```

期望至少看到这些工具名：
- `bill_get`
- `bill_list`
- `quota_get`
- `quota_list`
- `price_query`
- `price_compose`
- `fee_rate_lookup`
- `price_composition_get`
- `aux_table_get`
- `aux_table_list`
- `resource_lookup`

`ce-task`：

```bash
curl -s http://127.0.0.1:8101/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc":"2.0",
    "id":"tools-task",
    "method":"tools/list",
    "params":{}
  }' | jq
```

期望至少看到这些工具名：
- `orchestrate`
- `norm_qa`
- `cost_compose`
- `quota_lookup`
- `price_lookup`
- `start_cost_session`

## 6. gateway / agent 联调

如果还要验证 DeerFlow 侧是否真的接入新 MCP，按下面顺序做：

1. 确认 `extensions_config.json` 中 `ce-rag` / `ce-db` / `ce-task` 为 enabled。
2. 重启 gateway。
3. 若 gateway 有 MCP 缓存，执行一次 `touch extensions_config.json` 后再重启。
4. 观察 gateway 日志，确认已加载 3 个 server 的工具。

建议的对话验证用例：

1. `现浇混凝土矩形柱按什么计量，按 gb50854-2024 回答`
   期望：`ce-task_norm_qa` 或 `ce-task_orchestrate` -> `ce-rag search_clause`
2. `C30现浇钢筋混凝土矩形柱，按 2024 在深圳组价`
   期望：`ce-task_cost_compose` 或 `ce-task_orchestrate` -> `ce-rag match_bill_item` -> `ce-db price_compose`
3. `深圳钢筋信息价是多少`
   期望：`ce-task_price_lookup` 或 `ce-task_orchestrate` -> `ce-db price_query`

判定标准：
- 前端或日志里应出现 `ce-rag_*` / `ce-db_*` / `ce-task_*`。
- 不应依赖旧兼容工具作为主链路。

## 7. 常见失败点

- `:8101` 正常启动但 `/mcp` 404
  - 大概率是没走新 `main.py`，或 `mcp` 依赖未装，或进程未重启。
- `:8101/health` 还指向旧 `knowledge_url`
  - 大概率是 `ce-services/common/config.py` 没更新到新版本，或环境变量覆盖了预期值。
- `ce-rag` 正常但 `ce-db` 全部 500
  - 优先查 PG 连接、只读账号、相关结构化表是否齐全。
- `ce-task cost_compose` 能选码但不能取价
  - 先单独打 `:8102/price/compose/...`，确认是 `ce-db` 问题还是任务层问题。
- gateway 对话里没有出现 `ce-rag_*` / `ce-db_*` / `ce-task_*`
  - 大概率是 gateway 没重启、配置缓存没刷新，或者 agent allow-list 未包含新工具名。

## 8. 建议回传结果

服务器联调完成后，建议把下面结果贴回：

- 三个 `/health` 的摘要。
- `ce-rag / ce-db / ce-task` 三个 `initialize` 返回里的 `serverInfo.name`。
- `tools/list` 各自工具数。
- 一条 `norm/qa` 成功样例。
- 一条 `cost/compose` 成功样例。
- 若失败，附具体 endpoint、HTTP 状态码、响应体。
