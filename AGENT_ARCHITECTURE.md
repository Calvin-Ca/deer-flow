# AGENT_ARCHITECTURE · 技术架构图

> 依据仓库实际代码（2026-07）绘制：deer-flow super-agent harness（`backend/` + `frontend/`）+ 建筑领域三层（`ce-services/` 任务层 → `ce-code/` 知识层 → 基础设施），以及数据流水线与评测环。各图均标注对应源码位置。

---

## 1. 全景拓扑

```mermaid
flowchart TB
    subgraph ENTRY["入口层"]
        USER(("用户"))
        FE["Web 前端 Next.js :3000<br/>frontend/"]
        NGINX["Nginx 统一入口 :2026<br/>/api/langgraph/* → Gateway 运行时<br/>/api/* → Gateway REST<br/>/ → 前端"]
        IM["IM Channels<br/>backend/app/channels/<br/>飞书 / Slack / Telegram / 钉钉 / 微信(iLink)"]
        DESKTOP["Desktop 打包<br/>desktop/"]
    end

    subgraph DEERFLOW["deer-flow super-agent harness（backend/）"]
        GW["Gateway API :8001<br/>app/gateway/（FastAPI + 内嵌 LangGraph 运行时<br/>RunManager + run_agent + StreamBridge）"]
        LEAD["Lead Agent<br/>packages/harness/deerflow/agents/lead_agent/<br/>18 层 middleware 链 · ThreadState"]
        TOOLS["工具装配 get_available_tools<br/>沙箱 bash/read/write · 内置工具<br/>MCP 工具 · subagent task"]
        SKILLS["Skills（git 纳管）<br/>skills/public/norm-qa<br/>skills/public/cost-agent"]
        MEM["记忆 / 沙箱 / 子代理<br/>memory · sandbox(local) · subagents"]
    end

    subgraph CE_TASK["ce-services 任务层 :8101（知识服务的纯 HTTP 客户端）"]
        ROUTE["前置路由+编排 routing/<br/>prerouter（确定性分流，零 LLM）<br/>orchestrator（复合拆解→派发→综合，32b）"]
        NORMQA["Norm-QA norm/<br/>standard_router（T-A2 规范确定化）<br/>pipeline → generation（带引用）→ guards"]
        COSTA["CostAgent cost/<br/>graph.py：HITL 13 步 langgraph 状态机<br/>selection 选码(32b) · pricing 确定性算钱<br/>gates/clarify/provenance/session"]
        MCPT["MCP façade「ce-task」/mcp<br/>common/mcp_server.py<br/>orchestrate · norm_qa · cost_compose"]
    end

    subgraph CE_KNOW["ce-code 知识层 :8100（retrieval + PG + Milvus 唯一 owner）"]
        KAPI["knowledge_api（统一入口）<br/>service/knowledge_api.py<br/>/bill/match /price/compose /quota<br/>/search /expand /clause"]
        MCPC["MCP façade「ce-cost」/mcp<br/>service/mcp_server.py<br/>bill_match · quota_lookup · price_compose"]
        RETR["规范条文 hybrid 检索<br/>retrieval/（bm25+dense+rrf+rerank+引用扩展）<br/>index/ · feature/"]
        COSTQ["组价取数原语 cost/<br/>query.py（get_quota/compose_price）<br/>bill_match.py（dense 召回+重排）"]
    end

    subgraph INFRA["基础设施（服务器 172.19.3.136）"]
        PG[("PostgreSQL ce-postgres :5433<br/>库 ce_cost（rootless docker）")]
        MILVUS[("Milvus :19530<br/>cost_bill_spec_kb / _2013 + 条文索引")]
        LLM8B["Qwen3-8B vLLM :8099<br/>（生成/选码桶 A）"]
        LLM32B["Qwen3-32B（ORCH_LLM）<br/>（拆解/综合/选码桶 B）"]
        EMB["bge-large-zh Embedding :8097"]
        RERANK["Rerank :8095（Docker GPU）"]
        VLM["Qwen2.5-VL-7B :8098"]
    end

    USER --> NGINX --> FE
    NGINX --> GW
    IM -->|langgraph-sdk| GW
    DESKTOP --> NGINX
    GW --> LEAD --> TOOLS
    LEAD -.加载.- SKILLS
    LEAD --- MEM
    TOOLS -->|"MCP(http) ce-task_*"| MCPT
    TOOLS -->|"MCP(http) ce-cost_*"| MCPC
    SKILLS -.->|"bash 兜底 qa.py/cost.py"| CE_TASK

    MCPT --> ROUTE
    MCPT --> NORMQA
    MCPT --> COSTA
    ROUTE -->|派发子任务| NORMQA
    ROUTE -->|派发子任务| COSTA
    NORMQA -->|"HTTP /search"| KAPI
    COSTA -->|"HTTP /bill/match /price/compose"| KAPI
    NORMQA --> LLM8B
    COSTA --> LLM32B
    ROUTE --> LLM32B

    KAPI --> RETR
    KAPI --> COSTQ
    MCPC --> COSTQ
    RETR --> MILVUS
    RETR --> EMB
    RETR --> RERANK
    COSTQ --> PG
    COSTQ --> MILVUS
    COSTQ --> EMB
```

**分层铁律**（代码中强制）：

| 边界 | 规则 | 落点 |
|---|---|---|
| harness ↔ app | `app.*` 可 import `deerflow.*`，反向禁止 | `backend/tests/test_harness_boundary.py`（CI） |
| 任务层 ↔ 知识层 | ce-services 是**纯 HTTP 客户端**：不 import retrieval、不连 PG/Milvus | `ce-services/common/{knowledge_client,cost_client}.py` |
| 存储 owner | retrieval + PG + Milvus 只有 ce-code 一个 owner | `ce-code/service/knowledge_api.py` |
| 国标版本隔离 | `spec`（2013/2024）必填无默认，缺省 → 400；PG 复合主键 `(code, spec_version)`、每版本独立 Milvus collection | `ce-code/config.py SPEC_REGISTRY` + `cost/schema.sql` |
| LLM 不算钱 | 组价/汇总为确定性公式 + pydantic 闸门，LLM 只做选码/拆解/综合 | `ce-services/cost/pricing.py` |
| 弱模型不驱动流程 | 能力分流用确定性规则（零 LLM）；是否停闸由服务端图决定 | `ce-services/routing/prerouter.py` · `cost/graph.py` |

---

## 2. Agent 调用链（deer-flow → CE 能力）

领域能力通过 **skill（提示词导引）+ MCP 工具（结构化执行）** 双通道接入 lead agent，注册于根 `extensions_config.json`：

```mermaid
sequenceDiagram
    participant U as 用户
    participant A as Lead Agent（deer-flow）
    participant T as ce-task :8101/mcp
    participant K as ce-code :8100
    participant L as Qwen3（8B/32B）

    U->>A: "满堂脚手架工程量怎么计算？"
    Note over A: skill norm-qa 红线：版本不猜先 ask_clarification
    A->>U: 反问：查哪版规范（2013/2024）？
    U->>A: 2024 房建
    A->>T: ce-task_norm_qa(query, standard=gb50854-2024)
    T->>T: resolve_standard（T-A2 漂移夺回）
    T->>K: POST /search（hybrid 检索）
    K-->>T: clauses（零召回 → 直接拒答 C-03）
    T->>L: generation.answer（带引用生成）
    T->>T: guards.audit_answer（C-01/02 校验闸）
    T-->>A: {answer, cited_clauses, meta}
    A-->>U: 带条文引用的回答（前端结构化渲染）
```

**两个 MCP façade 的分工**（均为 FastMCP streamable-HTTP、复用编排/取数内核、不反代自家 REST）：

| Server | 挂载 | 工具 | 性质 |
|---|---|---|---|
| `ce-cost` | :8100/mcp（`ce-code/service/mcp_server.py`） | `bill_match` / `quota_lookup` / `price_compose` | 纯数据原语，无状态只读，红线下沉到原语边界 |
| `ce-task` | :8101/mcp（`ce-services/common/mcp_server.py`） | `orchestrate` / `norm_qa` / `cost_compose` | 带 LLM 编排的任务能力，无状态一把出结果 |

有状态的 HITL 组价会话**不走 MCP**——由前端 `cost-hitl` marker 卡片直连 :8101 session 端点驱动（见 §3）。

---

## 3. CostAgent HITL 状态机（ce-services/cost/graph.py）

独立 langgraph 图，可中断可恢复，每步带 provenance；compute（含 LLM，checkpoint 保证只跑一次）与 gate（幂等 interrupt）双拆，避免 resume 重跑 LLM 漂移：

```mermaid
flowchart LR
    S([setup]) --> LM[list_match<br/>候选召回+选码 compute]
    LM --> FG{feature_gate<br/>缺特征?}
    FG -->|反问补全 ≤2轮| LM
    FG --> LG{list_gate<br/>确认编码}
    LG -->|有码| CP[compose<br/>组价取数 compute]
    LG -->|无码| SK[skip]
    CP --> QG{quota_gate} --> PG2{price_gate} --> NG{quantity_gate}
    NG --> ADV{advance<br/>还有构件?}
    SK --> ADV
    ADV -->|下一件| LM
    ADV -->|办完| RG{rates_gate<br/>费率录入·项目级}
    RG --> PMG{params_gate<br/>政策参数录入}
    PMG --> RU[rollup<br/>总造价·确定性]
    RU --> RV{末尾 review<br/>始终暂停}
    RV --> D([done])
```

对外三端点（`cost/router.py`）：`POST /cost/session/start`（跑到首闸）→ `POST /cost/session/{id}/resume`（带用户决策续跑）→ `GET /cost/session/{id}/state`（钉码/override/audit_log 持久化）。另有无状态旧路 `POST /cost/compose`（一次性选码+取数）与确定性原语 `/cost/unit-price`、`/cost/rollup`。

---

## 4. 知识层数据流水线（ce-code，离线 → 在线）

```mermaid
flowchart TB
    subgraph OFFLINE["离线流水线（服务器执行，产物入 git）"]
        PDF["data/raw/*.pdf<br/>（不入 git）"]
        MINERU["MinerU 远程 API 172.19.2.2:8000<br/>python -m ingest.parser mineru"]
        PARSED["data/parsed/&lt;doc&gt;/auto/<br/>md + content_list.json"]
        SPLIT["python -m ingest.splitter toc<br/>（Chunk 树，单一真值）"]
        CHUNKS["data/structured/chunks/&lt;规范&gt;/default/chunks.json"]

        subgraph EXTRACT["组价抽取 cost/（chunks → jsonl，宁缺毋造）"]
            E1["bill_spec 清单项（50854/50856）"]
            E2["quota 定额三表（SJG 171/170，单位格锚定）"]
            E3["price 信息价（月度时效）"]
            E4["fee_rate 费率 · price_composition 费用构成"]
            E5["bill_quota 清单→定额 APPLIES 映射"]
        end

        JSONL["data/structured/cost/&lt;doc_id&gt;/*.jsonl"]
        LOADPG["python -m cost.load_pg --scan-dir<br/>（幂等 upsert）"]
        BINDEX["python -m cost.bill_index --spec 2024/2013<br/>（清单向量库）"]
        NINDEX["build.py view→feature→index<br/>（条文 bm25+dense 索引）"]
    end

    subgraph STORE["存储"]
        PGDB[("PG ce_cost：bill_spec · quota_item ·<br/>resource · resource_price · fee_rate ·<br/>bill_quota_map · price_composition …")]
        MV[("Milvus：cost_bill_spec_kb(2024) /<br/>_2013 + 条文 collection")]
        BM["data/vector_store/（BM25 等，不入 git）"]
    end

    subgraph ONLINE["在线服务 :8100"]
        API2["/bill/match（dense 召回+结构约束重排）<br/>/price/compose /quota（PG 只读，spec 版本过滤）<br/>/search /expand /clause（hybrid 检索）"]
    end

    PDF --> MINERU --> PARSED --> SPLIT --> CHUNKS --> EXTRACT --> JSONL --> LOADPG --> PGDB
    PGDB --> BINDEX --> MV
    CHUNKS --> NINDEX --> MV
    NINDEX --> BM
    PGDB --> API2
    MV --> API2
    BM --> API2
```

---

## 5. deer-flow harness 内部（backend/，上游框架）

```
Gateway :8001（app/gateway/，FastAPI）
 ├─ 内嵌 LangGraph 运行时：RunManager + run_agent() + StreamBridge（SSE）
 ├─ REST routers：models / mcp / skills / memory / uploads / threads / artifacts / feedback / channels …
 └─ Lead Agent（make_lead_agent）
     ├─ 中间件链（18 层，严格顺序）：ThreadData → Uploads → Sandbox → DanglingToolCall
     │   → LLMError → Guardrail → SandboxAudit → ToolError → Summarization → TodoList
     │   → TokenUsage → Title → Memory → ViewImage → DeferredToolFilter → SubagentLimit
     │   → LoopDetection → Clarification（必须最后，拦 ask_clarification 中断）
     ├─ 工具：沙箱(bash/ls/read/write/str_replace，虚拟路径 /mnt/user-data) · 内置(present_files/
     │   ask_clarification/view_image) · MCP(lazy+mtime 失效) · 社区(tavily/jina/firecrawl) · task 子代理
     ├─ 模型工厂：create_chat_model（vLLM VllmChatModel，Qwen thinking 开关；本项目 agent 用 qwen-plus，
     │   Qwen3-8B function-calling 不可靠）
     ├─ 记忆：per-user memory.json（debounce 30s，LLM 抽取事实）
     └─ 追踪：Langfuse/LangSmith（graph 根挂 callback，run_id → 确定性 trace_id → 用户反馈回流打分）
```

---

## 6. 评测环（benchmark/，项目根）

| 组件 | 内容 |
|---|---|
| `benchmark/routing_eval/` | 路由/澄清金标（prerouter · standard_router · orchestrate 分流） |
| `benchmark/retrieval_eval/` | 清单匹配 / 条文召回金标（`ce-code/tools/eval_bill.py`：Top-1/3、Recall@k、MRR） |
| `benchmark/runner/` | Langfuse Datasets 上传 + `run_routing_experiment.py`（dataset.run_experiment 从 agent tool calls 打分） |
| `ce-services/tools/` | `benchmark.py` / `eval_select.py` 选码评测 / `prerouter_eval.py` / `standard_router_eval.py` |

评测经 Langfuse trace（`langfuse_session_id=thread_id`，prompt-variant 打 tag）与线上流量同环观测。

---

## 7. 端口与部署速查

| 端口 | 服务 | 归属 |
|---|---|---|
| :2026 | Nginx 统一入口（`/setup` 首启建管理员） | deer-flow |
| :3000 | Next.js 前端 | deer-flow |
| :8001 | Gateway API + LangGraph 运行时 | deer-flow |
| :8100 | 知识服务 knowledge_api（REST + `ce-cost` MCP） | ce-code（裸机 CPU，先起） |
| :8101 | 任务服务 main.py（REST + `ce-task` MCP） | ce-services（裸机 CPU） |
| :8095 | Rerank（Docker GPU，sudo daemon） | 基础设施 |
| :8097 / :8098 / :8099 | Embedding / VLM / Qwen3-8B vLLM | 基础设施 |
| :5433 | PG ce-postgres（rootless docker，库 ce_cost） | 基础设施 |
| :19530 | Milvus | 基础设施 |
| :19090 | Prometheus（+ Grafana 监控栈，`docker/`） | 运维 |

启动顺序：基础设施（PG/Milvus/vLLM）→ 知识服务 :8100 → 任务服务 :8101 → deer-flow（`make dev`）。后台常驻用 `setsid`/tmux，勿用 nohup。
