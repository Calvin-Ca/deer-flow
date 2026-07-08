# ce-code MCP 拆分方案（ce-rag / ce-db）

## 目标

把当前混合的知识能力拆成两类边界清晰的 MCP：

- `ce-rag`
  - 面向非结构化/半结构化检索
  - 负责混合检索、候选召回、引用扩展、证据返回
  - 输出允许包含“相关候选”和分数
- `ce-db`
  - 面向结构化真值取数
  - 负责查表、join、过滤、聚合、口径锁定
  - 输出应是确定字段值或空结果，不做语义猜测

旧入口 `service/knowledge_api.py` 与 `service/mcp_server.py` 先保留，作为兼容层；新入口独立新增。

## 文件迁移清单

### 现状 -> 新位置

| 现状 | 新边界 | 说明 |
|---|---|---|
| `retrieval/*` | `rag/service.py` 调用 | 继续保留原检索内核，不重写 |
| `service/retrieve_service.py` | `rag/service.py` 替代对外编排 | RAG 对外职责收口 |
| `cost/bill_match.py` | `rag/service.py::match_bill_item` | 语义召回，应归 RAG |
| `cost/query.py::get_bill` | `db/service.py::bill_get` | 已知编码直取，归 DB |
| `cost/query.py::get_quota` | `db/service.py::quota_get` | 结构化取数，归 DB |
| `cost/query.py::query_resource_price` | `db/service.py::price_query` | 结构化取价，归 DB |
| `cost/query.py::compose_price` | `db/service.py::price_compose` | 结构化组价取数，归 DB |
| `aux_table / fee_rate / price_composition` 查询缺口 | `db/dao.py` + `rag/projection_search.py` | DB 做真值查找，RAG 做文本投影检索 |
| `service/mcp_server.py` | `service/rag_mcp_server.py` + `service/db_mcp_server.py` | 新 MCP 分拆 |
| `service/knowledge_api.py` | `service/rag_api.py` + `service/db_api.py` | 新 REST 分拆 |

### 新增文件

| 文件 | 作用 |
|---|---|
| `rag/contracts.py` | RAG 证据返回契约 |
| `rag/projection_search.py` | 半结构化表/规则的文本投影检索 |
| `rag/service.py` | RAG 对外编排层 |
| `db/dao.py` | PG 只读查询补全层 |
| `db/service.py` | DB 对外编排层 |
| `service/rag_mcp_server.py` | `ce-rag` MCP |
| `service/db_mcp_server.py` | `ce-db` MCP |
| `service/rag_api.py` | `ce-rag` REST + MCP 入口 |
| `service/db_api.py` | `ce-db` REST + MCP 入口 |

## 新 Tool API 列表

### ce-rag

| Tool | 用途 |
|---|---|
| `search_clause` | 规范条文混合检索 |
| `expand_clause_refs` | 对命中条文做引用扩展 |
| `get_clause` | 按条款号直取单条条文 |
| `match_bill_item` | 构件描述 -> 清单候选召回 |
| `search_aux_table` | 辅助/参数表文本投影检索 |
| `search_price_rule` | 费用构成/费率规则检索 |
| `retrieve_evidence` | 统一证据前门，按 corpus 分发 |

### ce-db

| Tool | 用途 |
|---|---|
| `bill_get` | 按清单编码精确查询 |
| `bill_list` | 清单项列表/过滤查询 |
| `quota_get` | 按定额编号精确查询 |
| `quota_list` | 定额列表/按 bill_code 关联查询 |
| `price_query` | 名称模糊查信息价 |
| `price_compose` | 清单 -> 定额 -> 工料机 -> 信息价 |
| `fee_rate_lookup` | 费率规则结构化查询 |
| `price_composition_get` | 查询费用构成规则 |
| `aux_table_get` | 精确取辅助表 |
| `aux_table_list` | 辅助表列表/过滤 |
| `resource_lookup` | 人材机主数据查询 |

## 设计纪律

- `ce-rag` 不返回“这就是最终真值”的语义；只返回候选证据、相关度和来源。
- `ce-db` 不提供 `raw_sql`，不提供“描述 -> 最佳答案”这类模糊查询。
- `bill_match` 从旧 `ce-cost` 语义上迁出，归 `ce-rag`。
- 所有 `ce-db` 返回都带 `doc_id/spec_version/region` 等口径字段。
- 所有 `ce-rag` 返回都带 `truth_level`：
  - `semantic_candidate`
  - `text_projection`
  - `ground_truth_row`

## 兼容策略

- 保留旧 `service/knowledge_api.py` 与 `service/mcp_server.py`，不立刻删。
- `ce-services` 后续可逐步从 `ce-cost_*` 迁到 `ce-rag_*` + `ce-db_*`。
- 新配置建议：
  - `ce-rag` -> `http://host:8100/mcp`
  - `ce-db` -> `http://host:8102/mcp`
