# DeerFlow 2.0 学习笔记（caic_note）

> 面向 agent 初学者的项目架构详解：先讲"什么是 agent"，再讲"DeerFlow 是怎么把 agent 工程化的"，最后讲"我自己怎么跑、怎么 debug"。

---

## 0. TL;DR — 一句话理解 DeerFlow

**DeerFlow 是一个"super agent harness（超级智能体外壳）"**：它本身不是一个 agent，而是一套用来 **组织 / 调度 / 隔离 / 持久化** 多个 agent 的 **运行时框架**。它把 LangGraph 当作底层执行图，在上面套了一层：

- **Lead Agent**（领头智能体）：负责接收用户消息、调用工具、必要时把任务派给子 agent。
- **Sub-agents**（子智能体）：在后台线程池里执行专项任务（如 `general-purpose`、`bash`）。
- **Sandbox**（沙箱）：给 agent 一个隔离的"文件系统 + shell"，限制它能动到哪里。
- **Skills**（技能）：把"做某类任务的提示词 + 允许的工具集合"打包成可插拔的 markdown 资源。
- **Memory**（长期记忆）：以文件形式按用户隔离地持久化 facts/context。
- **MCP**（Model Context Protocol）：把外部工具服务器接进来，作为 agent 的工具。
- **Gateway**（FastAPI 网关）：把这些能力以 REST + LangGraph 兼容的形式暴露给前端、IM 频道、嵌入式客户端。

整套系统的核心循环是：
**用户消息 → 一串 Middleware（中间件） → LLM 选工具 → 工具执行（可能创建子 agent / 写沙箱文件） → 结果回流 → 下一轮**。

---

## 1. Agent 关键概念（先打基础再看代码）

### 1.1 LLM vs Agent
- **LLM**：一个无状态的"输入 prompt，输出文本"的函数。
- **Agent**：在 LLM 之外加上了 `(状态 + 工具 + 循环)`。
  - **状态（State）**：消息历史、todo 列表、产物文件等。
  - **工具（Tools）**：模型可以"调用"的外部函数（如 `bash`、`read_file`、`task`）。
  - **循环（Loop）**：模型输出 → 看是不是工具调用 → 如果是，执行工具，把结果塞回消息历史 → 再让模型回应 → 直到模型给出纯文本回答。

DeerFlow 的 agent 循环由 **LangGraph** 驱动（`create_agent(...)`，见 `backend/packages/harness/deerflow/agents/lead_agent/agent.py`）。

### 1.2 Middleware（中间件）
LangGraph agent 在每一轮"模型调用前后"都允许插入钩子，DeerFlow 在这层上挂了 18 个中间件来做：
prompt 注入、工具错误处理、循环检测、token 统计、自动起标题、记忆写入、图片注入…… 这是整个工程的"血管"，比工具本身更重要。

### 1.3 ThreadState（线程状态）
一次对话 = 一个 `thread_id`。每个 thread 有一份 `ThreadState`，扩展自 LangChain 的 `AgentState`，加了：
- `sandbox`：当前沙箱 id
- `thread_data`：物理 workspace/uploads/outputs 路径
- `title`：自动生成的会话标题
- `artifacts`：本轮"展示给用户的文件"列表（自定义 reducer 去重）
- `todos`：plan mode 下的任务清单
- `uploaded_files` / `viewed_images`：上传文件、已读取的图片缓存

源码：`backend/packages/harness/deerflow/agents/thread_state.py`

### 1.4 Sub-agent（子智能体）
模型可以调用 `task(description, prompt, subagent_type)` 工具来把一个**独立任务**派给子 agent。
子 agent 在 **后台线程池** 里跑，跟主 agent 隔离上下文，结果通过 SSE 事件回流。并发上限默认 3，超时 15 分钟。

### 1.5 Sandbox（沙箱）
agent 看到的是虚拟路径（`/mnt/user-data/workspace`、`/mnt/skills`），实际写到磁盘上的物理路径是
`backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/...`。这层"虚拟 → 物理"翻译让：
- 一份 agent 提示词可以在本地 / Docker / k8s 沙箱模式下复用；
- 不同用户、不同线程的数据天然隔离。

### 1.6 Skill（技能）
一个 Skill = 一个目录 + 一个 `SKILL.md`（含 YAML frontmatter：`name`, `description`, `allowed-tools`...）。
启用某个 skill 时，它的 metadata 会被注入到 lead agent 的 system prompt 里，告诉模型"你现在有这能力，用以下工具去做"。可以理解为**给 agent 装外挂技能包**。

### 1.7 MCP（Model Context Protocol）
一种 Anthropic 提出的、让外部进程把工具暴露给 LLM 的协议。DeerFlow 用 `langchain-mcp-adapters` 来连多个 MCP server（stdio / SSE / HTTP），把它们的工具自动注册到 lead agent 的工具列表里。

### 1.8 Memory（长期记忆）
跨 thread 的、按用户隔离的"事实库"。每次对话结束后，一个**异步队列**把消息丢给 LLM 去抽取 facts/context，存到 `users/{user_id}/memory.json`。下一次新对话开始时，把 top-15 个 facts + 上下文摘要注入 prompt 的 `<memory>` 标签。

### 1.9 Streaming（流式输出）
LangGraph 支持 `stream_mode=["values", "messages", "custom"]`：
- `messages-tuple`：AI 文本的**增量**（per-id 拼起来才是完整消息）；工具调用/结果**只发一次**。
- `values`：完整 state 快照（不再重发 AI 文本，避免重复）。
- `custom`：通过 `StreamWriter` 主动发送的自定义事件（子 agent 进度等）。
- Gateway 把这些转成 SSE 给前端。

---

## 2. 仓库整体结构

```
deer-flow/
├── Makefile                    根入口：check / install / dev / stop
├── config.yaml                 主配置（模型、工具、沙箱、memory…）
├── extensions_config.json      扩展配置（MCP servers + skills 启用状态）
├── backend/                    Python 后端（重点）
├── frontend/                   Next.js 16 前端
├── skills/{public,custom}/     可加载的技能目录
├── docker/                     Docker 部署脚本
└── scripts/                    serve.sh / docker.sh / deploy.sh 等运维脚本
```

后端又分两层（**严格依赖方向：app → deerflow，禁止反向**，CI 用
`backend/tests/test_harness_boundary.py` 强制）：

```
backend/
├── app/                              应用层（不发布）
│   ├── gateway/                      FastAPI 网关（端口 8001）
│   └── channels/                     IM 频道桥接（飞书/Slack/TG/DingTalk/微信）
└── packages/harness/deerflow/        框架层（可发布的 deerflow-harness 包）
    ├── agents/                       Agent 编排
    ├── runtime/                      运行时（runs、events、checkpoint、stream bridge）
    ├── subagents/                    子 agent 调度
    ├── tools/                        工具加载与内置工具
    ├── sandbox/                      沙箱抽象 + 本地实现
    ├── mcp/                          MCP 集成
    ├── skills/                       技能加载
    ├── models/                       模型工厂（thinking / vision）
    ├── memory/ → agents/memory/      长期记忆
    ├── persistence/                  SQLite/DB 持久化
    ├── config/                       配置加载
    ├── community/                    第三方工具（tavily、jina、firecrawl…）
    ├── reflection/                   字符串路径 → 模块/类 的反射加载
    ├── guardrails/                   工具调用前置授权
    ├── tracing/                      LangSmith / Langfuse trace
    └── client.py                     `DeerFlowClient`：嵌入式 Python 客户端
```

---

## 3. 每个模块/文件夹的功能 + 设计动机

### 3.1 `backend/packages/harness/deerflow/agents/` — Agent 编排核心

| 文件/目录 | 作用 | 设计动机 |
|---|---|---|
| `lead_agent/agent.py` | `make_lead_agent(config)` 工厂：选模型、装工具、拼装 middleware、生成 system prompt，最终返回一个 LangGraph agent。 | 一个工厂方法 = 一次"运行时配置 → agent 实例"的可重入构造，便于 LangGraph Server / DeerFlowClient 都复用 |
| `lead_agent/prompt.py` | `apply_prompt_template(...)` 拼 system prompt：注入已启用 skills、subagent 名单、当前时间等。同时启动后台线程异步刷新 skill 缓存。 | system prompt 必须**保持稳定**以利用 KV-cache；动态信息走 `<system-reminder>` 注入到第一条 user message |
| `thread_state.py` | `ThreadState` schema + 两个自定义 reducer（`merge_artifacts`、`merge_viewed_images`）。 | LangGraph 的 state 是用 reducer 合并的；DeerFlow 加了去重/清空语义 |
| `factory.py` / `features.py` | 提供 agent 构造的辅助：feature flag、agent 元信息 | 把"哪些能力开"和"怎么开"解耦 |
| `memory/` | `updater.py`（LLM 抽 facts）+ `queue.py`（debounce 队列）+ `prompt.py`（抽取 prompt）+ `storage.py`（按用户 + agent 隔离存储） | **记忆写入异步化**，不阻塞会话；按用户隔离避免数据串号 |
| `middlewares/` | 见 §3.2 | — |

### 3.2 `agents/middlewares/` — 18 个中间件（按装配顺序）

这是理解 DeerFlow 行为的关键。装配位置：
- `tool_error_handling_middleware.py::build_lead_runtime_middlewares()` 装前半段；
- `lead_agent/agent.py::_build_middlewares()` 再 append 后半段。

| 顺序 | Middleware | 干什么 | 为什么需要 |
|---|---|---|---|
| 1 | `ThreadDataMiddleware` | 解析 `user_id`，建出 `users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}` | 给后续所有写文件的工具一个稳定的物理根目录 |
| 2 | `UploadsMiddleware` | 把本轮新上传的文件清单塞进上下文 | 让模型"看得见"附件 |
| 3 | `SandboxMiddleware` | 获取沙箱，把 `sandbox_id` 写入 state | 让 bash/read/write 等工具有目标 |
| 4 | `DanglingToolCallMiddleware` | 给"AIMessage 里有 tool_calls 但没对应 ToolMessage"的悬空调用打补丁（用户中断常造成） | 否则下一次 LLM 调用会 422 |
| 5 | `LLMErrorHandlingMiddleware` | 把 provider 异常包装成可恢复的 assistant 错误 | 单点容错，避免运行整个挂掉 |
| 6 | `GuardrailMiddleware`（可选） | 工具调用前置授权（allowlist / OAP policy / 自定义） | 安全：先放谁、再放谁 |
| 7 | `SandboxAuditMiddleware` | 审计 shell/文件操作 | 安全日志 |
| 8 | `ToolErrorHandlingMiddleware` | 工具异常 → 错误 ToolMessage | 让 agent 继续而不是崩 |
| 9 | `DynamicContextMiddleware` | 把当前日期 / memory 注入到第一条 user message 的 `<system-reminder>` | system prompt 不变 → 享受 prefix cache |
| 10 | `SummarizationMiddleware`（可选） | 上下文接近 token 限制时压缩历史 | 长会话不爆 |
| 11 | `TodoMiddleware`（plan mode 开启时） | 提供 `write_todos` 工具，强制只一个 in_progress | 复杂任务可追溯 |
| 12 | `TokenUsageMiddleware`（可选） | 累计 token 用量 | 计费 / 观测 |
| 13 | `TitleMiddleware` | 首轮结束后自动生成会话标题 | UX |
| 14 | `MemoryMiddleware` | 过滤出 user + final AI message，丢到 memory 队列 | 长期记忆异步落盘 |
| 15 | `ViewImageMiddleware`（仅 vision 模型） | 把 base64 图像注入 LLM 调用 | 多模态 |
| 16 | `DeferredToolFilterMiddleware`（可选） | 没开 tool_search 之前隐藏延迟工具 schema | 缩小工具列表，提速 |
| 17 | `SubagentLimitMiddleware`（subagent 开启时） | 截断超过 `MAX_CONCURRENT_SUBAGENTS=3` 的并发 task 调用 | 防止子 agent 爆 |
| 18 | `LoopDetectionMiddleware` | 检测反复同样的 tool call，硬停 | 防 agent 死循环 |
| 19 | `ClarificationMiddleware`（始终最后） | 拦截 `ask_clarification` 工具，`Command(goto=END)` 中断让用户回答 | 让 agent 能"主动提问" |

> 顺序很重要：例如 `DanglingToolCallMiddleware` 必须在送给模型前，`ClarificationMiddleware` 必须在最后才能拦截最终响应。

### 3.3 `agents/lead_agent/` — Lead Agent 入口
- `agent.py::make_lead_agent(config)` 注册在 `backend/langgraph.json` 里，是 LangGraph Server 启动时的 graph factory。
- `_get_runtime_config(config)` 把 `configurable + context` 合并出运行时参数（`thinking_enabled`、`model_name`、`is_plan_mode`、`subagent_enabled`、`agent_name`…）。
- 根据 `is_bootstrap` 走两条路：bootstrap（创建新自定义 agent 时只给 `setup_agent` 工具）vs 正常（完整工具集 + 可选 `update_agent`）。

### 3.4 `agents/subagents/` — 子 agent
- `executor.py`：用两个线程池 —— `_scheduler_pool`（3 个 worker，负责调度）+ `_execution_pool`（3 个 worker，负责跑）。流式事件：`task_started/running/completed/failed/timed_out`。
- `registry.py`：内置 `general-purpose`（除 task 外全工具）、`bash`（命令专家），可注册自定义。
- `token_collector.py`：归集子 agent 的 token 使用回主线程。
- `config.py`：并发 / 超时 / 工具白名单等。

设计动机：把"长耗时、独立上下文"的任务从主 agent 剥离，主 agent 只看子 agent 的最终结果，**主上下文不被噪声污染**。

### 3.5 `tools/` — 工具系统
- `tools.py::get_available_tools(groups, include_mcp, model_name, subagent_enabled, app_config)`：核心组装函数。
  1. 从 `config.yaml.tools[]` 反射出工具
  2. 加 MCP 工具（懒加载，按 mtime 失效）
  3. 加内置工具（在 `tools/builtins/`）
  4. 如启用 subagent，加 `task` 工具
- `tools/builtins/`：
  - `present_file_tool.py`：把 outputs 下的文件"亮"给用户（前端能看到）
  - `clarification_tool.py`：触发 ClarificationMiddleware 中断流程
  - `view_image_tool.py`：base64 读图（仅 vision 模型）
  - `setup_agent_tool.py` / `update_agent_tool.py`：让 agent 创建/修改自身的 `SOUL.md` 和 `config.yaml`
  - `task_tool.py`：调子 agent
  - `tool_search.py`：从一大堆 deferred tool 里按 query 加载需要的（节省 prompt 空间）

### 3.6 `sandbox/` — 沙箱抽象
- `sandbox.py`：抽象基类 `Sandbox`（`execute_command/read_file/write_file/list_dir`）+ `SandboxProvider`（`acquire/get/release`）。
- `local/`：单例进程内沙箱（开发态默认）。
- `community/aio_sandbox/`：Docker isolation 实现。
- `tools.py`：暴露给 agent 的 `bash / ls / read_file / write_file / str_replace`，里头做**虚拟路径翻译**和**按 `(sandbox_id, path)` 的并发串行化**。
- `middleware.py`：在 agent 生命周期内 acquire / release 沙箱。

为什么这么设计：让"agent 写代码 / 跑命令"的能力可以**安全升级**到 Docker / k8s，agent 端不用改任何 prompt。

### 3.7 `runtime/` — Gateway 内嵌的 LangGraph 兼容运行时
- `runs/manager.py` (`RunManager`)：内存里的 run 注册表 + 可选 `RunStore` 持久化。
- `runs/worker.py` (`run_agent`)：实际跑 agent 的协程。
- `stream_bridge/`：把 LangGraph stream 桥接成 SSE / WebSocket。
- `events/store/`：事件持久化（前端 join 已结束 run 时回放）。
- `checkpointer/`：状态检查点（async SQLite）。
- `journal.py`：`RunJournal` 给每个 LLM 调用打 tag（区分 lead_agent / middleware 调用，给 trace 用）。
- `user_context.py`：`contextvar` 形式的 `get_effective_user_id()`，无 auth 模式回退 `"default"`。

动机：DeerFlow 不再依赖独立的 LangGraph Server 进程，而是把它**塞进 Gateway**，nginx 把 `/api/langgraph/*` 重写到 Gateway 的同一端口。

### 3.8 `persistence/` — 数据持久化
SQLite 默认存储；`models/` 是 ORM；`run/` 是 run 元数据；`feedback/` 是用户反馈；`thread_meta/` 是线程元数据；`user/` 是用户表；`migrations/` 是 schema 升级脚本。

### 3.9 `mcp/`
- `MultiServerMCPClient` 多服务器
- 懒加载工具（`get_cached_mcp_tools()`）
- mtime 失效缓存
- OAuth token 自动刷新（HTTP/SSE 模式）

### 3.10 `skills/` & `skills/tool_policy.py`
- `storage.py`：扫 `deer-flow/skills/{public,custom}/` 下的 `SKILL.md`，结合 `extensions_config.json` 的启用状态返回 Skill 列表。
- `tool_policy.py::filter_tools_by_skill_allowed_tools()`：根据已启用 skills 的 `allowed-tools` 字段过滤当前 agent 能看到的工具。
- 设计动机：一个 agent 不应该看到**所有**工具，而是只看到"它的当前角色应该用的"工具。

### 3.11 `models/`
- `factory.py::create_chat_model(name, thinking_enabled, ...)`：根据 `config.yaml.models[]` 反射出 `ChatOpenAI` / `ChatAnthropic` / vLLM 等实例。
- 支持 `supports_thinking` + `when_thinking_enabled` 覆盖（用来给某些模型在"思考模式"下换更猛的配置）。
- 支持 `supports_vision` 开关。
- `vllm_provider.py::VllmChatModel`：保留 vLLM 的非标准 `reasoning` 字段。

### 3.12 `config/` 配置体系
- `AppConfig.from_file()`：解析 `config.yaml`，环境变量 `$VAR` 自动解析。
- 自动校验 `config_version` 与 example 文件的差异。
- `get_app_config()` 缓存解析结果，并按 mtime 自动重载（编辑配置不用重启）。
- 加载优先级：显式参数 → `DEER_FLOW_CONFIG_PATH` → backend/config.yaml → project root/config.yaml。

### 3.13 `community/`
**可选第三方工具，按需启用**：tavily（搜+取）、jina_ai（reader）、firecrawl（爬虫）、image_search（DuckDuckGo 图搜）、aio_sandbox（Docker 沙箱实现）。

### 3.14 `reflection/`
- `resolve_variable("module.path:var")` / `resolve_class(...)`：让配置文件用字符串路径动态加载类。
- 动机：模型、工具、沙箱、guardrails provider 都可以**只在配置里改字符串**就换实现，不用改代码。

### 3.15 `client.py` — Embedded Python Client
`DeerFlowClient`：**不开 HTTP 服务的情况下** 直接在 Python 里复用 DeerFlow 的 agent、memory、skills、uploads…… 返回 schema 跟 Gateway API 一致（CI 用 `TestGatewayConformance` 校验一致性）。

### 3.16 `app/gateway/` — FastAPI Gateway（应用层）

| 文件 | 作用 |
|---|---|
| `app.py` | FastAPI 应用工厂，挂 CORS / Auth / CSRF middleware，注册所有 router |
| `auth/` & `auth_middleware.py` & `langgraph_auth.py` | 用户认证、JWT、密码、credential 文件、langgraph 兼容认证 hook |
| `csrf_middleware.py` | CSRF 防护，与 CORS 配合 |
| `internal_auth.py` | 内部组件（如 channels）跨服务调用时的进程内 token |
| `config.py` / `deps.py` / `services.py` | gateway 自身配置 / FastAPI 依赖注入 / 服务句柄 |
| `path_utils.py` / `authz.py` / `utils.py` | 工具方法 |
| `routers/` | REST 路由模块（见下节 §3.16.1） |

#### 3.16.1 `routers/` 路由层详解

所有路由模块通过 `routers/__init__.py` 统一导出，在 `app.py` 里逐个 `include_router()`。每个模块持有独立的 `router = APIRouter(prefix=..., tags=[...])` 实例，**模块间几乎无直接依赖**，共享依赖通过两个横切层注入：

| 横切层 | 文件 | 作用 |
|--------|------|------|
| 依赖注入 | `deps.py` | 提供 `get_config`、`get_checkpointer`、`get_run_manager`、`get_stream_bridge` 等 singletons |
| 权限守卫 | `authz.py` | `@require_permission(resource, action)` 装饰器，基于 `AuthContext` 做行级授权 |

**核心执行链（Run 生命周期）**

```
POST /api/threads/{id}/runs/stream  →  thread_runs.py
POST /api/runs/stream               →  runs.py
    共同调用 services.start_run() → worker.run_agent()
```

| 文件 | 路由前缀 | 主要职责 |
|------|---------|---------|
| `thread_runs.py` | `/api/threads/{id}/runs` | Run 的完整 CRUD + SSE 流式端点（`/stream`、`/wait`、`/join`）；消息/事件/token 使用量查询；**核心 Run 调度入口** |
| `runs.py` | `/api/runs` | 无状态运行兼容端点；自动生成临时 `thread_id`；唯一跨路由依赖：复用 `thread_runs.RunCreateRequest` |

**线程管理**

| 文件 | 路由前缀 | 主要职责 |
|------|---------|---------|
| `threads.py` | `/api/threads` | Thread CRUD（创建/读取/搜索/删除）、状态查询（`/state`）、历史（`/history`）、checkpoint 管理 |

**认证与反馈**

| 文件 | 路由前缀 | 主要职责 |
|------|---------|---------|
| `auth.py` | `/api/v1/auth` | 登录/登出（HttpOnly cookie + JWT）；用户注册；密码常见词黑名单校验 |
| `feedback.py` | `/api/threads/{id}/runs` | Run 级别的 👍/👎 反馈 CRUD；通过 run_id 关联 |

**配置与扩展管理**

| 文件 | 路由前缀 | 主要职责 |
|------|---------|---------|
| `models.py` | `/api/models` | 列出可用 LLM 模型及其能力（thinking/reasoning_effort 支持情况）|
| `skills.py` | `/api/skills` | 技能 CRUD；启用/禁用；安装自定义 skill（含安全扫描）；刷新 prompt cache |
| `agents.py` | `/api/agents` | 自定义 Agent CRUD（读写 `SOUL.md` + `config.yaml`）；名称格式校验（hyphen-case）|
| `mcp.py` | `/api/mcp` | MCP 服务器配置读写（含 OAuth 配置）；驱动 extensions_config.json |
| `memory.py` | `/api/memory` | 全局记忆（用户/历史上下文）CRUD；fact 级别增删改；按 user_id 隔离 |

**文件与媒体**

| 文件 | 路由前缀 | 主要职责 |
|------|---------|---------|
| `uploads.py` | `/api/threads/{id}/uploads` | 文件上传（含 Markdown 转换）；文件列举/删除；路径遍历防护；沙箱集成 |
| `artifacts.py` | `/api/artifacts` | Agent 生成产物的下载/预览；ZIP 打包；MIME 检测；防 XSS（active content 转 attachment）|

**兼容与集成**

| 文件 | 路由前缀 | 主要职责 |
|------|---------|---------|
| `assistants_compat.py` | `/api/assistants` | LangGraph Platform assistants API 最小兼容存根，满足前端 `useStream` hook 初始化需求 |
| `suggestions.py` | `/api/suggestions` | 根据对话上下文直接调用 LLM 生成追问建议（短路调用，不走 Run 系统）|
| `channels.py` | `/api/channels` | IM 渠道（微信/钉钉/飞书等）状态查询和重启；委托给 `app.channels.service`（懒加载避免循环导入）|

**依赖关系图**

```
app.py
  ├── deps.py ◄──────── 几乎所有路由都 import（get_config, get_run_manager...）
  ├── authz.py ◄─────── thread_runs / threads / feedback / artifacts / uploads
  ├── services.py ◄──── thread_runs / runs（start_run, sse_consumer）
  │
  ├── thread_runs.py
  │      └── runs.py（复用 RunCreateRequest，唯一跨路由依赖）
  │
  └── 其余模块均独立，互不依赖
```

**核心设计原则**：路由模块是薄 HTTP 适配层，业务逻辑下沉到 `services.py`（Run 调度）和 `deerflow.*` 包（Agent/配置/持久化），路由间通过共享的 `deps.py` 单例通信，而不是直接互相导入。

#### 3.16.2 路由调用者分类

路由的实际调用方有三类，职责完全不同：

```
前端用户    → fetch / LangGraph SDK    → 管理面 + 对话面路由
IM 渠道     → langgraph-sdk + httpx    → 对话面路由 + 少量查询
Agent 工具  → 进程内直接写文件         → 不走任何 HTTP 路由
```

**前端用户调用的路由**

| 场景 | 路由 |
|------|------|
| 登录/登出 | `POST /api/v1/auth/login/local` |
| 设置面板（模型/技能/MCP/记忆/Agent） | `GET /api/models`、`GET/PUT /api/skills`、`GET/PUT /api/mcp/config`、`GET/.../PATCH/DELETE /api/agents`、`GET/POST/.../DELETE /api/memory` |
| 新建/搜索会话 | `POST /api/threads`、`POST /api/threads/search` |
| **发送消息（核心对话）** | `POST /api/threads/{id}/runs/stream` |
| 点击"停止"按钮 | `POST /api/threads/{id}/runs/{rid}/cancel` |
| 加载历史消息 | `GET /api/threads/{id}/messages` |
| 上传附件 | `POST /api/threads/{id}/uploads` |
| 下载/预览产物 | `GET /api/artifacts/...` |
| 点赞/踩 | `PUT /api/threads/{id}/runs/{rid}/feedback` |
| 生成追问建议 | `POST /api/threads/{id}/suggestions` |
| useStream hook 初始化 | `GET /api/assistants/search`（SDK 内部触发） |

**IM 渠道系统调用（携带 `internal_auth` header，绕过 cookie）**

| 调用 | 路由 | 触发时机 |
|------|------|---------|
| `client.threads.create()` | `POST /api/threads` | 新用户发来第一条消息 |
| `client.runs.stream(...)` | `POST /api/threads/{id}/runs/stream` | 飞书/钉钉流式回复 |
| `client.runs.wait(...)` | `POST /api/threads/{id}/runs/wait` | Slack/Telegram 同步回复 |
| `httpx GET /api/models` | `GET /api/models` | 用户发 `/models` 斜杠命令 |
| `httpx GET /api/memory` | `GET /api/memory` | 用户发 `/memory` 斜杠命令 |

**Agent 工具——不经过任何路由**

`setup_agent`、`update_agent`、沙箱工具、记忆系统在执行时全部**直接操作文件系统**，不回调任何 REST API。工具在 `make_lead_agent()` 里装配到 agent 实例，由 LangGraph 图引擎在 tool 节点自动调度：

```
POST /api/threads/{id}/runs/stream
  └─ services.start_run()
       └─ asyncio.create_task(run_agent(..., agent_factory=make_lead_agent))
            └─ make_lead_agent() → get_available_tools() + [setup_agent/update_agent]
            └─ agent.astream()   ← LangGraph 驱动工具调用（进程内，无 HTTP）
```

工具文件位置：`packages/harness/deerflow/tools/builtins/`，在 `lead_agent/agent.py` 第 415、433 行按模式装配：

```python
# bootstrap 模式（创建新 agent）：只有 setup_agent
tools = get_available_tools(...) + [setup_agent]

# 普通 / 自定义 agent 模式
extra_tools = [update_agent] if agent_name else []
tools = get_available_tools(...) + extra_tools
```

#### 3.16.3 管理 CRUD API 的设计动机

`/api/agents`、`/api/skills`、`/api/mcp`、`/api/memory` 是**控制面（Control Plane）**，不参与运行时执行。

**本质是配置文件的 HTTP 包装器**：

| 路由 | 实际写的文件 |
|------|------------|
| `PUT /api/agents/{name}` | `users/{uid}/agents/{name}/SOUL.md` + `config.yaml` |
| `PUT /api/skills/{name}` | `extensions_config.json` → `skills.<name>.enabled` |
| `PUT /api/mcp/config` | `extensions_config.json` → `mcpServers` |
| `POST /api/memory/facts` | `users/{uid}/memory.json` → `facts[]` |

包装的价值：Gateway 统一处理参数校验、防路径遍历、原子写（temp + rename）、缓存失效，前端只关注业务逻辑。

**运行时与管理面严格分离**：Agent 运行时通过 `get_app_config()`（mtime 自动重载）直接读文件，**不回调 REST API**，避免循环依赖（agent → gateway → agent）和无谓网络往返。

**创建 Agent 的两条平行路径**：

```
路径 A（人工）：前端填表单 → POST /api/agents → Gateway 写磁盘
路径 B（AI）：对话框自然语言 → runs/stream(is_bootstrap=true)
              → Lead Agent 调用 setup_agent 工具 → 直接写磁盘
              → onToolEnd("setup_agent") → 前端 GET /api/agents/{name}（带重试）
```

两条路径写出的文件格式完全一致，`/api/agents` 是人工管理入口，`setup_agent` 是 AI 辅助创建的运行时快捷方式，底层数据模型统一。`/api/skills` 的 install 端点与 `skill-creator` skill 的关系同理。

### 3.17 `app/channels/` — IM 桥接

每个文件对应一个 IM 平台（Slack / Feishu / Telegram / DingTalk / WeChat / WeCom / Discord）。它们：
1. 接收外部消息 → `MessageBus.publish_inbound()`
2. `ChannelManager._dispatch_loop()` 消费 → 查/建 thread → 通过 `langgraph-sdk` 调 Gateway
3. 流式回复（Feishu 卡片 patch、DingTalk AI Card streaming） / 同步回复（Slack/TG）
4. 处理 `/new /status /models /memory /help` 等斜杠命令

`store.py` 用 JSON 文件做 `channel:chat[:topic]` → `thread_id` 的映射。

### 3.18 `frontend/src/` — Next.js 16 前端

- **`app/`**：Next.js App Router。`/workspace/chats/[thread_id]` 是主聊天页。
- **`components/`**：
  - `ui/`（Shadcn 自动生成，禁手改）
  - `ai-elements/`（Vercel AI SDK 自动生成）
  - `workspace/`（聊天页：messages / artifacts / settings）
  - `landing/`（首页）
- **`core/`**（业务核心）：
  - `threads/`：用 hook 形式封装 `useThreadStream` / `useSubmitThread` —— 这是前端的"API 主接口"
  - `api/`：`getAPIClient()` 单例 LangGraph SDK 客户端
  - `artifacts/`、`messages/`、`memory/`、`skills/`、`mcp/`、`models/`、`uploads/`、`tools/`、`todos/`：跟后端 1:1 对应的领域模型
  - `i18n/`：en-US / zh-CN
  - `settings/`：localStorage 配置
  - `auth/`、`notification/`、`agents/`、`blog/`、`tasks/`、`config/`、`rehype/`、`streamdown/`、`utils/`
- **`hooks/`**：共享 React hooks
- **`lib/`**：`cn()` 工具
- **`styles/`**：Tailwind v4 + CSS variables 主题
- **`env.js`**：`@t3-oss/env-nextjs` + Zod 校验环境变量

数据流：
```
UI 输入 → useThreadStream (core/threads/hooks.ts)
       → LangGraph SDK getAPIClient()
       → /api/langgraph/* (nginx)
       → Gateway 内嵌 runtime (RunManager + run_agent + StreamBridge)
       → SSE 流回前端
       → 解析 messages-tuple/values/custom → 更新 thread state → 渲染
```

### 3.19 `skills/` 仓库内置技能
`skills/public/` 下有一堆现成的示例：
`academic-paper-review`、`chart-visualization`、`code-documentation`、`data-analysis`、`deep-research`、`frontend-design`、`image-generation`、`newsletter-generation`、`podcast-generation`、`ppt-generation`、`skill-creator`、`video-generation`、`web-design-guidelines` 等。
启用方式：`extensions_config.json` 中 `skills.<name>.enabled = true`，或通过 `PUT /api/skills/{name}`。

---

## 4. 关键设计动机串讲（why this way）

1. **Harness vs App 严格分层** —— 框架层 `deerflow.*` 不允许 import 应用层 `app.*`，CI 强制。
   动机：harness 要能独立发包、嵌入式使用（`DeerFlowClient`），不能跟 FastAPI/IM 频道耦合。

2. **Gateway 内嵌 LangGraph 运行时**（而不是另起 LangGraph Server 进程）
   动机：少一个进程 / 端口 / 容器，简化 Docker 部署，鉴权/CSRF 复用同一栈。

3. **Middleware-first 设计**
   动机：把"prompt 注入 / 错误处理 / 中断 / 记忆 / 审计 / 限流"都做成可插拔单元，新需求多数情况下只是再加一个 middleware，而不是改核心循环。

4. **System prompt 保持稳定，动态内容走 `<system-reminder>`**
   动机：吃 LLM 的 prefix cache（巨省 token 和延迟）。

5. **虚拟路径**（`/mnt/user-data/...`）
   动机：本地、Docker、k8s 三种沙箱实现下，模型看到的提示永远一样。

6. **MCP + Skills 双重扩展机制**
   动机：MCP 解决"接外部服务"，Skills 解决"组合本地工具+提示词成一个能力"。两者正交。

7. **每用户、每 thread 完全隔离**
   动机：可以 SaaS 化部署；某 thread 出问题不影响他人；memory 也按用户分桶。

8. **Subagent 走独立线程池 + 上下文隔离**
   动机：把长任务从主对话剥离，主上下文窗口不被无关 token 占满。

9. **配置改动不重启**（mtime-based reload + cache 失效）
   动机：开发态体验 + 生产态 hot-config。

10. **`DeerFlowClient` 与 Gateway 走同一份 schema**
    动机：嵌入式用户和 HTTP 用户得到一致的对象；CI 的 `TestGatewayConformance` 跑 Pydantic 校验防漂移。

---

## 5. 后端调试路径（cheatsheet）

### 5.1 启动方式选哪一个？

| 目的 | 命令 | 说明 |
|---|---|---|
| 整套跑起来（Gateway + 前端 + nginx），开发态 | `make dev` (项目根目录) | 访问 http://localhost:2026 |
| 只跑后端 Gateway | `cd backend && make dev` | 端口 8001，自带 reload |
| 单步 debug lead agent，不开 HTTP | `cd backend && PYTHONPATH=. uv run python debug.py` | 见 §5.2 |
| Docker 开发 | `make docker-start` | 见 docker/ |
| 生产部署 | `make up` / `./scripts/deploy.sh` | — |

### 5.2 `debug.py` —— 最快进入断点的方式

`backend/debug.py` 是为 VSCode 单步调试准备的 REPL：

```bash
cd backend
PYTHONPATH=. uv run python debug.py
```

它做的事：
1. 装 file handler 到 `debug.log`（避免日志污染交互终端）
2. 初始化 MCP 工具
3. 调 `make_lead_agent(config)` 拿到 agent
4. 在 prompt 循环里 `await agent.ainvoke(state, config=config)`

**在哪里打断点最有用**：
- `backend/packages/harness/deerflow/agents/lead_agent/agent.py::_make_lead_agent` —— 看 agent 怎么搭起来
- `backend/packages/harness/deerflow/agents/middlewares/*.py::before_model / after_model` —— 看具体一步在干啥
- `backend/packages/harness/deerflow/tools/tools.py::get_available_tools` —— 看工具列表怎么来的
- `backend/packages/harness/deerflow/sandbox/tools.py` —— 看 bash/read/write 是怎么翻译路径的
- `backend/packages/harness/deerflow/subagents/executor.py` —— 看子 agent 怎么调度的

### 5.2.1 单次消息完整断点路径（核对版）

路径以项目根 `deer-flow/` 为基准。

**HTTP → 运行时**

| 断点位置 | 说明 |
|---|---|
| `backend/app/gateway/routers/thread_runs.py` | `stream_run()` — 入口，调 `services.start_run()` |
| `backend/app/gateway/services.py` | `start_run()` — 校验模型、建 RunRecord、`create_task(run_agent(...))` |
| `backend/packages/harness/deerflow/runtime/runs/worker.py:L179` | `run_agent()` — set_status(running) |
| `backend/packages/harness/deerflow/runtime/runs/worker.py:L217` | `_build_runtime_context()` — 只读 `config["context"]`，不含 configurable |
| `backend/packages/harness/deerflow/runtime/runs/worker.py:L229` | `agent_factory(config=...)` → 进入 `make_lead_agent()` |
| `backend/packages/harness/deerflow/agents/lead_agent/agent.py:L365` | `_make_lead_agent()` — `_get_runtime_config()` 在此合并 configurable+context，**agent_name 的正确断点** |
| `backend/packages/harness/deerflow/runtime/runs/worker.py:L286` | `agent.astream()` 循环开始 |

**图节点 / 工具执行（第三方库）**

| 断点位置 | 说明 |
|---|---|
| `backend/.venv/lib/python3.12/site-packages/langchain/agents/factory.py:L1344` | `amodel_node()` — 模型节点入口 |
| `backend/.venv/lib/python3.12/site-packages/langchain/agents/factory.py:L1316` | `_execute_model_async()` — 调 `model.ainvoke()` |
| `backend/.venv/lib/python3.12/site-packages/langgraph/prebuilt/tool_node.py:L826` | `_afunc()` — 工具节点入口，解析所有 tool_calls |
| `backend/.venv/lib/python3.12/site-packages/langgraph/prebuilt/tool_node.py:L1159` | `_arun_one()` — 单个工具调用，触发 awrap_tool_call 链 |
| `backend/.venv/lib/python3.12/site-packages/langgraph/prebuilt/tool_node.py:L1067` | `_execute_tool_async()` — 调 `tool.ainvoke()` |

**Middleware 文件与 hook**（均在 `backend/packages/harness/deerflow/agents/middlewares/`）

| 文件 | hook（sync / async） | 行号 |
|---|---|---|
| `thread_data_middleware.py` | `before_agent` | L82 |
| `uploads_middleware.py` | `before_agent` | L188 |
| `dynamic_context_middleware.py` | `before_agent` / `abefore_agent` | L199 / L203 |
| `summarization_middleware.py` | `before_model` / `abefore_model` | L120 / L123 |
| `view_image_middleware.py` | `before_model` / `abefore_model` | L191 / L208 |
| `todo_middleware.py` | `before_model` / `abefore_model` / `after_model` / `aafter_model` | L68 / L105 / L119 / L173 |
| `token_usage_middleware.py` | `after_model` / `aafter_model` | L298 / L302 |
| `loop_detection_middleware.py` | `after_model` / `aafter_model` | L420 / L424 |
| `title_middleware.py` | `after_model` / `aafter_model` | L179 / L183 |
| `subagent_limit_middleware.py` | `after_model` / `aafter_model` | L71 / L75 |
| `memory_middleware.py` | `after_agent` | L53 |
| `dangling_tool_call_middleware.py` | `wrap_model_call` / `awrap_model_call` | L161 / L172 |
| `llm_error_handling_middleware.py` | `wrap_model_call` / `awrap_model_call` | L209 / L255 |
| `deferred_tool_filter_middleware.py` | `wrap_model_call` / `wrap_tool_call` / `awrap_model_call` / `awrap_tool_call` | L72 / L80 / L91 / L99 |
| `sandbox_audit_middleware.py` | `wrap_tool_call` / `awrap_tool_call` | L330 / L348 |
| `tool_error_handling_middleware.py` | `wrap_tool_call` / `awrap_tool_call` | L40 / L55 |
| `clarification_middleware.py` | `wrap_tool_call` / `awrap_tool_call` | L159 / L181 |

### 5.2.2 一次完整请求的中间件断点（按触发阶段）

> `self._agent.stream()` / `agent.astream()` 调用后**先进入 LangGraph 框架**，框架按图节点顺序把控制权交回项目代码。下面是回到项目代码后的完整触发顺序。
>
> 所有文件路径相对 `backend/packages/harness/deerflow/`，`sandbox/middleware.py` 在 `sandbox/` 下。

#### 阶段一：before_agent（每次请求只走一次）

| 文件 | 行号 | 备注 |
|------|------|------|
| `agents/middlewares/thread_data_middleware.py` | L82 | 创建 thread 目录，写 `thread_data` 到 state |
| `agents/middlewares/uploads_middleware.py` | L188 | 检测新上传文件，注入 `<uploaded_files>` 到最后一条 HumanMessage |
| `sandbox/middleware.py` | L52 | 获取沙箱实例，写 `sandbox_id` 到 state |
| `agents/middlewares/dynamic_context_middleware.py` | L199（sync）/ L203（async） | 把当前日期 + memory 注入第一条 HumanMessage 的 `<system-reminder>` |

#### 阶段二：每轮 LLM 调用（循环，直到模型不再返回 tool_calls）

**before_model（LLM 调用前）：**

| 文件 | 行号 | 备注 |
|------|------|------|
| `agents/middlewares/summarization_middleware.py` | L120（sync）/ L123（async） | 可选；接近 token 限制时压缩历史 |
| `agents/middlewares/todo_middleware.py` | L68（sync）/ L105（async） | plan_mode 时；在 prompt 里注入 todo 状态 |
| `agents/middlewares/view_image_middleware.py` | L191（sync）/ L208（async） | 视觉模型时；把 base64 图像注入消息 |

**wrap_model_call（洋葱层，由外到内包裹 LLM 调用本体）：**

| 文件 | 行号 | 备注 |
|------|------|------|
| `agents/middlewares/dangling_tool_call_middleware.py` | L161（sync）/ L172（async） | 最外层；修补悬空 tool_calls |
| `agents/middlewares/llm_error_handling_middleware.py` | L209（sync）/ L255（async） | 重试 + 熔断；把 LLM 异常转成可恢复消息 |
| `agents/middlewares/deferred_tool_filter_middleware.py` | L72（sync）/ L91（async） | 从 `request.tools` 里过滤掉未激活的延迟工具 |
| **[LLM 实际调用]** | — | LangGraph 框架内部 |

**after_model（LLM 调用后）：**

| 文件 | 行号 | 备注 |
|------|------|------|
| `agents/middlewares/todo_middleware.py` | L119（sync）/ L173（async） | plan_mode 时；解析模型输出中的 write_todos |
| `agents/middlewares/token_usage_middleware.py` | L298（sync）/ L302（async） | 记录 token 用量，写 attribution 到 AIMessage |
| `agents/middlewares/title_middleware.py` | L179（sync）/ L183（async） | 首轮结束后异步生成标题 |
| `agents/middlewares/subagent_limit_middleware.py` | L71（sync）/ L75（async） | 截断超过 MAX_CONCURRENT_SUBAGENTS=3 的 task 调用 |
| `agents/middlewares/loop_detection_middleware.py` | L420（sync）/ L424（async） | 检测重复 tool_calls；警告或强制清空 tool_calls |

#### 阶段三：每次工具调用（有 tool_calls 时，洋葱层由外到内）

| 文件 | 行号 | 备注 |
|------|------|------|
| `agents/middlewares/sandbox_audit_middleware.py` | L330（sync）/ L348（async） | 仅 bash 工具；高危命令直接 block |
| `agents/middlewares/tool_error_handling_middleware.py` | L39（sync）/ L54（async） | 捕获工具异常，转为错误 ToolMessage |
| `agents/middlewares/deferred_tool_filter_middleware.py` | L80（sync）/ L99（async） | 拦截调用未激活延迟工具，返回提示错误 |
| `agents/middlewares/clarification_middleware.py` | L159（sync）/ L180（async） | 最内层；拦截 ask_clarification，`goto=END` 中断 |
| **[工具实际执行]** | — | LangGraph 框架内部 |

工具执行完毕后循环回 **阶段二 before_model**，直到模型不再返回 tool_calls。

#### 阶段四：after_agent（每次请求只走一次，Agent 结束后）

| 文件 | 行号 | 备注 |
|------|------|------|
| `sandbox/middleware.py` | L68 | 释放沙箱 |
| `agents/middlewares/memory_middleware.py` | L53 | 过滤消息，异步入队记忆更新 |

### 5.2.3 单次请求所有项目代码函数入口断点（完整版）

> 按调用时序排列，路径相对 `deer-flow/backend/`。
> `harness/` = `packages/harness/deerflow/`，`mw/` = `packages/harness/deerflow/agents/middlewares/`。
> 只列每个函数的**第一行**作为断点，不含第三方库。

#### P0 — HTTP 入口

| 文件 | 行 | 函数 |
|------|-----|------|
| `app/gateway/routers/thread_runs.py` | 126 | `stream_run()` |

#### P1 — 请求处理 & 任务创建（`services.py`）

| 文件 | 行 | 函数 |
|------|-----|------|
| `app/gateway/services.py` | 65 | `normalize_stream_modes()` |
| `app/gateway/services.py` | 77 | `normalize_input()` |
| `app/gateway/services.py` | 123 | `merge_run_context_overrides()` |
| `app/gateway/services.py` | 140 | `inject_authenticated_user_context()` |
| `app/gateway/services.py` | 158 | `resolve_agent_factory()` |
| `app/gateway/services.py` | 172 | `build_run_config()` |
| `app/gateway/services.py` | 248 | `start_run()` |
| `app/gateway/services.py` | 356 | `sse_consumer()` |

#### P2 — Worker & Agent 构建（`worker.py` + `agent.py`）

| 文件 | 行 | 函数 |
|------|-----|------|
| `harness/runtime/runs/worker.py` | 120 | `run_agent()` |
| `harness/runtime/runs/worker.py` | 44 | `_build_runtime_context()` |
| `harness/runtime/runs/worker.py` | 88 | `_install_runtime_context()` |
| `harness/runtime/runs/worker.py` | 112 | `_agent_factory_supports_app_config()` |
| `harness/agents/lead_agent/agent.py` | 343 | `make_lead_agent()` |
| `harness/agents/lead_agent/agent.py` | 350 | `_make_lead_agent()` |
| `harness/agents/lead_agent/agent.py` | 29 | `_get_runtime_config()` |
| `harness/agents/lead_agent/agent.py` | 38 | `_resolve_model_name()` |
| `harness/agents/lead_agent/agent.py` | 240 | `_build_middlewares()` |
| `harness/agents/lead_agent/agent.py` | 53 | `_create_summarization_middleware()` |
| `harness/agents/lead_agent/agent.py` | 115 | `_create_todo_list_middleware()` |
| `harness/agents/lead_agent/agent.py` | 321 | `_available_skill_names()` |
| `harness/agents/lead_agent/agent.py` | 329 | `_load_enabled_skills_for_tool_policy()` |
| `harness/models/factory.py` | 50 | `create_chat_model()` |
| `harness/tools/tools.py` | 44 | `get_available_tools()` |
| `harness/agents/lead_agent/prompt.py` | 768 | `apply_prompt_template()` |
| `harness/agents/lead_agent/prompt.py` | 554 | `_get_memory_context()` |
| `harness/agents/lead_agent/prompt.py` | 626 | `get_skills_prompt_section()` |
| `harness/runtime/runs/manager.py` | 179 | `create_or_reject()` |
| `harness/runtime/runs/manager.py` | 82 | `create()` |
| `harness/runtime/runs/manager.py` | 124 | `set_status()` |

*以上完成后，`worker.py:286` 处调用 `agent.astream()`，进入 LangGraph 框架，再由框架回调以下 middleware 节点。*

#### P3 — before_agent（每次请求一次）

| 文件 | 行 | 函数 |
|------|-----|------|
| `mw/thread_data_middleware.py` | 82 | `before_agent()` |
| `mw/uploads_middleware.py` | 188 | `before_agent()` |
| `harness/sandbox/middleware.py` | 52 | `before_agent()` |
| `harness/sandbox/middleware.py` | 45 | `_acquire_sandbox()` |
| `mw/dynamic_context_middleware.py` | 203 | `abefore_agent()` |
| `mw/dynamic_context_middleware.py` | 156 | `_inject()` |

#### P4 — LLM 循环（每轮 LLM 调用，重复直到无 tool_calls）

**wrap_model_call（洋葱，外→内）：**

| 文件 | 行 | 函数 |
|------|-----|------|
| `mw/dangling_tool_call_middleware.py` | 172 | `awrap_model_call()` |
| `mw/dangling_tool_call_middleware.py` | 106 | `_build_patched_messages()` |
| `mw/llm_error_handling_middleware.py` | 255 | `awrap_model_call()` |
| `mw/deferred_tool_filter_middleware.py` | 91 | `awrap_model_call()` |

**before_model：**

| 文件 | 行 | 函数 |
|------|-----|------|
| `mw/summarization_middleware.py` | 123 | `abefore_model()` |
| `mw/todo_middleware.py` | 105 | `abefore_model()` |
| `mw/view_image_middleware.py` | 208 | `abefore_model()` |
| `mw/view_image_middleware.py` | 129 | `_should_inject_image_message()` |
| `mw/view_image_middleware.py` | 167 | `_inject_image_message()` |

**after_model：**

| 文件 | 行 | 函数 |
|------|-----|------|
| `mw/todo_middleware.py` | 173 | `aafter_model()` |
| `mw/token_usage_middleware.py` | 302 | `aafter_model()` |
| `mw/token_usage_middleware.py` | 259 | `_apply()` |
| `mw/title_middleware.py` | 183 | `aafter_model()` |
| `mw/subagent_limit_middleware.py` | 75 | `aafter_model()` |
| `mw/subagent_limit_middleware.py` | 41 | `_truncate_task_calls()` |
| `mw/loop_detection_middleware.py` | 424 | `aafter_model()` |
| `mw/loop_detection_middleware.py` | 380 | `_apply()` |
| `mw/loop_detection_middleware.py` | 231 | `_track_and_check()` |

#### P5 — wrap_tool_call（每个工具调用，洋葱外→内）

| 文件 | 行 | 函数 |
|------|-----|------|
| `mw/sandbox_audit_middleware.py` | 348 | `awrap_tool_call()` |
| `mw/sandbox_audit_middleware.py` | 294 | `_pre_process()` |
| `mw/tool_error_handling_middleware.py` | 55 | `awrap_tool_call()` |
| `mw/deferred_tool_filter_middleware.py` | 99 | `awrap_tool_call()` |
| `mw/clarification_middleware.py` | 181 | `awrap_tool_call()` |
| `mw/clarification_middleware.py` | 117 | `_handle_clarification()` |

*工具执行完毕后回到 P4 before_model，继续循环。*

#### P6 — after_agent（每次请求一次，Agent 结束后）

| 文件 | 行 | 函数 |
|------|-----|------|
| `harness/sandbox/middleware.py` | 68 | `after_agent()` |
| `mw/memory_middleware.py` | 53 | `after_agent()` |
| `harness/agents/memory/queue.py` | 43 | `add()` |

#### P7 — 流式回传（`worker.py` 消费 astream 结果）

| 文件 | 行 | 函数 |
|------|-----|------|
| `harness/runtime/runs/worker.py` | 557 | `_unpack_stream_item()` |
| `harness/runtime/runs/worker.py` | 532 | `_extract_human_message()` |

---

### 5.3 跑 Gateway（带 HTTP）

```bash
cd backend
make gateway        # 仅 Gateway
# 或
make dev            # Gateway + uvicorn reload
```

健康检查：`curl http://localhost:8001/health`
OpenAPI：`http://localhost:8001/docs`（设 `GATEWAY_ENABLE_DOCS=false` 关掉）

### 5.4 看日志

- `backend/debug.log`：`debug.py` 模式下的所有日志
- 启动 Gateway 时，root logger 走标准输出
- 想升降级模块日志：改 `config.yaml::log_level` 或具体模块 logger（无需重启 —— config 是 mtime reload）

### 5.5 跑测试

```bash
cd backend
make test                                              # 全跑
PYTHONPATH=. uv run pytest tests/test_<feature>.py -v  # 单文件
make lint && make format                               # ruff
```

特别注意：
- 改 harness 时必看 `tests/test_harness_boundary.py`（强制 import 边界）
- 沙箱模式检测 → `tests/test_docker_sandbox_mode_detection.py`
- 记忆更新 → `tests/test_memory_updater.py`
- Client 与 Gateway schema 一致性 → `tests/test_client.py::TestGatewayConformance`

### 5.6 跟踪（tracing）

- 在 `.env` 配 LangSmith 或 Langfuse 即可自动上报。
- `RunJournal`（`runtime/journal.py`）会给每个 LLM 调用打 tag（比如 `middleware:summarize`），方便在 trace UI 里区分 lead_agent 调用 vs middleware 内部调用。

### 5.7 拿到一个 thread_id 后想看现场

```
backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/
├── workspace/       agent 工作区
├── uploads/         用户上传的文件
└── outputs/         present_files 暴露给前端的产物
```

- `user_id` 在无 auth 模式下是 `"default"`。
- 想清掉本地 thread 数据：`DELETE /api/threads/{id}`（同时 LangGraph 删 state，Gateway 删上面这堆目录）。

### 5.8 常见的"调不通"排查思路

| 现象 | 第一反应去看 |
|---|---|
| agent 不调任何工具 | `get_available_tools()` 返回是不是空 / skill 的 `allowed-tools` 是不是把工具滤光了 |
| 模型回 422 / tool_calls missing | `DanglingToolCallMiddleware` 是不是漏装 / 状态被截断了 |
| 子 agent 卡住 | `subagents/executor.py` 看池子是不是满；并发上限 3 / 超时 15min |
| 沙箱写不进文件 | `sandbox/tools.py` 路径翻译；`is_local_sandbox()` 是不是 True；权限 |
| 配置改了没生效 | 看 `config.yaml` mtime；`get_app_config()` 是按 mtime reload 的，但**已构造的 agent 不会自动重建** —— 调 `DeerFlowClient.reset_agent()` 或重启 |
| MCP 工具没刷新 | `extensions_config.json` mtime 是否更新 |
| 记忆没出现在 prompt | `memory.injection_enabled`、`max_injection_tokens`、`fact_confidence_threshold`；记忆是按 `(user_id, agent_name)` 缓存的 |
| 前端没拿到流 | nginx `/api/langgraph/*` 路由；CORS（`GATEWAY_CORS_ORIGINS`） |

---

## 6. 学习路径建议（个人向）

1. **先把整套跑起来**：`make dev` → 在前端发一句 hi → 看到完整 SSE 流。
2. **看 `backend/debug.py`** 单步进入 `make_lead_agent`，理解一次 invoke 的全流程。
3. **逐个读 18 个 middleware**（每个文件都很小）—— 这是 DeerFlow 的"灵魂"。
4. **挑一个内置工具读**（推荐 `tools/builtins/task_tool.py` 或 `sandbox/tools.py::bash`），理解一次工具调用是怎么从 LLM 输出 → 中间件 → 实际执行 → 回流的。
5. **读一个 router**（推荐 `app/gateway/routers/thread_runs.py`）—— 理解 HTTP 是怎么进到 runtime 的。
6. **读 `runtime/runs/manager.py` + `worker.py` + `stream_bridge/`** —— 理解 Gateway 怎么内嵌跑 LangGraph 的。
7. **写一个 skill**：`skills/custom/my-skill/SKILL.md`（YAML frontmatter + 正文），重启前端看是否能开启。
8. **写一个 middleware**：在 `agents/middlewares/` 加一个文件，在 `_build_middlewares` 里 append，单测覆盖。
9. **理解 Memory + Tracing + Persistence**：这三块决定生产可观测性。

读 docs/：`backend/docs/ARCHITECTURE.md`、`backend/docs/STREAMING.md`、`backend/docs/CONFIGURATION.md` 是进一步深挖的入口。

---

## 7. 一张图速记

```
┌────────────────────────── 用户 ─────────────────────────────┐
│  Web (Next.js)          IM 频道 (Slack/Feishu/...)          │
└────────────┬──────────────────────┬─────────────────────────┘
             │ SSE / REST           │ langgraph-sdk
             ▼                      ▼
        ┌──────────────────── Nginx :2026 ────────────────────┐
        │  /api/langgraph/* → Gateway runtime                 │
        │  /api/*           → Gateway REST                    │
        │  /                → Frontend (:3000)                │
        └──────────────────────┬──────────────────────────────┘
                               ▼
        ┌──────────── Gateway (FastAPI :8001) ────────────────┐
        │  routers: models / mcp / skills / memory / uploads  │
        │           threads / artifacts / runs / thread_runs  │
        │           feedback / suggestions / channels / auth  │
        │  auth_middleware / csrf_middleware / CORS           │
        │  ┌──────── runtime (LangGraph 兼容) ────────────┐   │
        │  │ RunManager → run_agent() → StreamBridge      │   │
        │  │   ↓                                          │   │
        │  │ make_lead_agent(config)                      │   │
        │  │   ↓                                          │   │
        │  │ create_agent(model, tools, middleware,       │   │
        │  │              system_prompt, ThreadState)     │   │
        │  └──────────────────────────────────────────────┘   │
        └──────────────────────┬──────────────────────────────┘
                               ▼
           ┌──── deerflow harness (packages/harness/) ────────┐
           │  agents/  ── 18 middlewares + lead_agent + memory│
           │  subagents/ ── 线程池调度，task() 工具入口        │
           │  tools/   ── builtins + MCP + community + cfg    │
           │  sandbox/ ── 虚拟路径 → 本地/Docker 沙箱          │
           │  models/  ── thinking / vision / vLLM 工厂        │
           │  skills/  ── SKILL.md 加载 + allowed-tools 过滤   │
           │  mcp/     ── MultiServerMCPClient                │
           │  persistence/ ── SQLite (runs / threads / users) │
           │  config/  ── AppConfig + extensions_config        │
           │  client.py ── DeerFlowClient（嵌入式 = 同一份 API）│
           └──────────────────────────────────────────────────┘
                               ▼
           ┌─────────── 磁盘：每用户/每 thread 隔离 ──────────┐
           │  .deer-flow/users/{uid}/                          │
           │    ├── memory.json / agents/{name}/memory.json    │
           │    └── threads/{tid}/user-data/                   │
           │           ├── workspace / uploads / outputs       │
           │           └── (sandbox files)                     │
           │  skills/{public,custom}/<skill>/SKILL.md           │
           └──────────────────────────────────────────────────┘
```

---

> 本笔记基于阅读 `backend/CLAUDE.md`、`backend/packages/harness/deerflow/agents/lead_agent/agent.py`、`backend/debug.py`、`backend/langgraph.json`、各 middleware/runtime 源码以及 `frontend/CLAUDE.md` 整理。后续如有架构变动，建议优先信任源码与最新的 CLAUDE.md。
