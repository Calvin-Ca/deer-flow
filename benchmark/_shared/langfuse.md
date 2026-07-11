# Langfuse UI 看板速读

> 定位：**看板怎么读**——跑完 runner 之后在 UI 里找分、比 variant、归因单条错误的操作指南。与两份既有文档的分工：`../LANGFUSE.md` 是全链路 runbook（前置→灌金标→跑 runner→看分的完整流程与架构决策），`README.md`（本目录）是 runner 命令手册；本文只管「打开 :3030 之后往哪点、看什么」。
>
> 入口：浏览器开 `http://172.19.3.136:3030`（自托管栈，见 `docker/ce-langfuse/README.md`）。

---

## 1. 五个页面各管什么

| 左侧导航 | 干什么用 | 什么时候来 |
|---|---|---|
| **Traces** | 每次 agent 运行一条 trace（含全部节点/LLM/工具子 span） | 归因单条失败用例（§4） |
| **Sessions** | 按 `session_id`（= runner 的 thread_id）聚合 trace | 查某条用例的完整会话（多轮时一会话多 trace） |
| **Datasets** | 金标集（`upload_datasets.py` 灌入）+ 每次评测的 Runs | **看分主入口**（§2）、比 variant（§3） |
| **Scores** | 全部分数流水（含线上 `user_feedback` 点赞踩） | 捞被点踩的真实 case、按分数名过滤 |
| **Dashboard** | 官方聚合图表（trace 量/延迟/token 花费） | 看趋势，评测归因基本用不上 |

## 2. 看一轮评测的分：Datasets → Runs

路径：**Datasets → 选数据集 → Runs 标签页 → 点你的 run_name**。

- 每行 = 一条用例：左边是 dataset item（input/expected_output 即金标），右边挂着**该条 agent 自己的完整 trace** 和逐条分数（runner 用 `dataset_run_items.create` 直接关联，不是分离的两棵树）。
- 各数据集 ↔ 分数名对照（分数由本地判定函数算好、`create_score` 挂上，Langfuse 只当账本）：

| Dataset | 来源 runner | 分数名 | 含义 |
|---|---|---|---|
| `agent-routing-eval` / `bill-match-routing` | L1 routing | `route_correct` | 该调工具就调了（命中 `ROUTE_TOOL_NAMES`）；**正确止步于反问的条目不挂此分**（不计分≠0 分） |
| 同上 | 同上 | `clarify_correct` | 该反问就反问（`ask_clarification`），红线主判据 |
| `clist-match-eval` | L3 retrieval | `match_top1` / `recalled` | 端到端选码命中 / 金标进候选（Recall@k） |
| `agent-toolcall-eval` | L6-B toolcall | `tool_correct` / `call_correct` | 工具名对 / 工具名+args 按 arg_match 都对 |
| `agent-cost-task` | L6-A cost_task | `task_pass` / `redline_ok` | 终态（最终清单码+溯源）过 / 无红线违规（独立计分） |
| `agent-norm-faithful` | L6-C norm_faithful | `faithfulness` / `refusal_ok` | 引用命中检索证据 / 拒答正确性 |

- 分数的 **comment 字段**写了期望 vs 实际（如「期望调脚本=True 实际=False 工具=[...]」）——归因先看它，多数情况不用点进 trace。

### 平均值 Ø 的陷阱

Runs 列表页每列分数只显示**平均值**且不显示是按几条算的：`route_correct` 的分母是「挂了该分的条目」——正确止步于反问的条目记 `None` 不挂分，所以 UI 的 Ø 与 runner 终端打印的「路由率」**分母口径一致但样本数看不见**。逐条矩阵 + 按维度重算平均用终端工具拉回来对账：

```
uv run --project backend python benchmark/_shared/dump_run_scores.py --run-name <run名>
```

## 3. 横向比 variant / 模型

同一数据集反复跑、每次换 `--run-name`（建议命名带上 variant 与模型，如 `lead_v2-qwen3-8b-0712`）：

- **Datasets → Runs 列表页**本身就是对比表：每个 run 一行，各分数列并排看均值。
- 勾选多个 run → **Compare**：逐条用例横向对比（同一条 A1 在 v1/v2 下分别调了什么、分数各多少），定位「v2 比 v1 好在哪几条、坏在哪几条」。
- 前提纪律：**每次跑都用新 run_name**——重名 run 的分数会并进同一看板混在一起；且旧版 runner 下重名还会续跑旧线程（已修，见 `../README.md` 隔离清单①）。

## 4. 归因单条失败：trace 里看什么

从 run 行点进 trace（或 Traces 页按 session 搜 `exp-{run名}-`前缀），自上而下三件事：

1. **System prompt**（根 span 的 input 里第一条 system 消息）：确认 variant 对不对（看开头是否 CE 提示词）、**有没有 `<memory>` 块**（有=记忆污染复发，见 `../README.md` 隔离清单②——2026-07-11 就是在这里抓到的实锤）、上下文是否异常肥大。
2. **工具调用链**（trace 树里的 tool span）：实际调了什么工具、参数是什么、返回多大——「工具=[]」的条目在这里分辨是模型真没调，还是中途 LLM 400 被打断（会看到 LLM span 报错）。
3. **LLM span 的 input tokens**：单次请求接近/超过 32768（qwen3-8b）说明上下文超载，该条属「环境问题」不是「agent 真错」，剔除后再算两率（归因三分类见 `CLAUDE.md` §4.3）。

## 5. 过滤与标签约定

Trace 上的标签由 tracing 层自动打（`build_langfuse_trace_metadata`），Traces 页可按它们过滤：

| 标签/字段 | 值约定 | 用途 |
|---|---|---|
| `session_id` | `exp-{run_name}-{nonce}-{item_id}`（评测）/ 前端为真实 thread_id | 从 run 名反查该轮全部 trace |
| tag `model:<名>` | 如 `model:qwen3-8b` | 按模型切分 |
| tag `variant:<名>` | 提示词文件名 stem（如 `lead_agent_v1`）；**`default` = 文件没解析到、回退内置模板**——看到它先别看分，尺子不对 | 按提示词版本切分 |
| tag `env:<名>` | `DEER_FLOW_ENV` 环境标 | 区分评测/生产流量 |
| score `user_feedback` | 前端点赞踩自动回写 ±1 | Scores 页过滤 <0 捞真实差评 case |

## 6. trace 树深读：observation 类型与图标的真相

### 6.1 observation type 体系

trace 是树，每个节点（observation）带一个 type，type 决定图标与详情页的专属面板。真正干活的信息只在两种叶子上：

| 类型 | 本质 | 本项目 trace 里的实例 |
|---|---|---|
| **generation** | 一次 LLM 调用（prompt/输出/token/成本面板）| `VllmChatModel` —— **归因主战场** |
| **tool** | 一次工具执行（参数/返回体）| `rag_search_clause`、`cost_workflow_start` 等 |
| span / chain / agent | 编排容器，只贡献瀑布图耗时 | `LangGraph`、`model`、`tools`、各中间件节点 |
| event / retriever / evaluator / embedding / guardrail | 打点/检索/评估/向量化/防护 | 本项目基本见不到（判官在本地 Python、向量化在 ce-rag 内部，均不上报）|

### 6.2 图标怎么定的（别当架构真相读）

type 由 SDK 的 LangChain CallbackHandler 按**机械规则**推断，两层（源码 `langfuse-python` 的 `_get_observation_type_from_serialized`）：

1. **run_type 硬映射**：llm→generation、tool→tool、retriever→retriever、其余 chain 回调→chain；
2. **名字启发式**：chain 回调的名字/类路径**含 "agent" 子串**即升格为 agent 类型。

所以看板上 `UploadsMiddleware.before_agent` 等中间件节点是 agent 图标，纯因**钩子方法名带 "agent" 后缀**撞了子串匹配，不代表它是决策节点；`model`/`tools` 是 chain 图标，因为它们是 LangGraph 的图节点（编排壳），真正的模型调用/工具执行是它们里面的 generation/tool 子节点。**看节点名比看图标可靠。**

### 6.3 中间件为什么会出现在看板（挂接点代码位置）

没有任何代码"专门"上报中间件——链路是三段：

1. **钩子成为图节点**（langchain v1 `agents/factory.py`，三方包）：`create_agent` 把中间件实现的 `before_agent`/`after_agent`/`before_model`/`after_model` 钩子注册成图节点，节点名 = `f"{类名}.{钩子名}"`；**`wrap_model_call` 类钩子不成节点**（在 model 节点内组合执行）——所以 LLMErrorHandling/DeferredToolFilter/LoopDetection 的拦截动作在 trace 上不可见，只能从 generation 的 input 反推。
2. **handler 挂在图调用根部**（本项目代码）：gateway 路径 `backend/packages/harness/deerflow/agents/lead_agent/agent.py:459-464`、嵌入式路径 `client.py:595-598`，都把 `build_tracing_callbacks()`（`tracing/factory.py:74`，内部实例化 `langfuse.langchain.CallbackHandler`）追加进 `config["callbacks"]`。必须挂根部：handler 只在 `on_chain_start(parent_run_id=None)` 时才把 session_id/user_id 提升到 trace（agent.py:452 注释）。
3. **回调继承下发**：LangChain 执行任意子 Runnable 时把父级 callbacks 原样传下去，图上每个节点执行即触发同一个 handler 建 observation。中间件侧零埋点。

### 6.4 observation 详情页字段来源

| 字段 | 来源 |
|---|---|
| **USER / assistant / system / tool 徽章** | input/output 被识别为 ChatML 消息数组时按每条 `role` 渲染；role 由 CallbackHandler 从 LangChain 消息类型转换（HumanMessage→user 等） |
| **Additional Input** | input JSON 里**没被识别成消息的剩余键**：LangGraph 节点 input 是整个 state dict，`messages` 抽走渲染对话后，`artifacts`/`todos` 等落这里 |
| **Corrected Output** | Langfuse 的 Corrections **功能位**（人工修正入口，存成 `dataType: CORRECTION` 的 score，攒微调集用）——默认空，不是我们的数据 |
| **Metadata** | CallbackHandler 上报的元数据：LangGraph 自动带 `langgraph_node/step/thread_id`，trace 根另有 deer-flow 注入的 `langfuse_session_id`/`variant`（`tracing/metadata.py`） |

> 徽章文字 = `role` 字段原样大写。看到不认识的徽章（如 USER_INPUT），把 Input 面板切 **JSON 视图**看那条消息的 `role`/`type` 实际值——是数据造出来的非标角色还是 UI 新标签，一看便知。

### 6.5 版本架构：服务端与 SDK 是两个东西

| 组件 | 是什么 | 版本查法 | 当前（2026-07-12） |
|---|---|---|---|
| **服务端**（Docker 四件套，UI+API+存储） | 收 trace、渲染看板 | `curl -s http://localhost:3030/api/public/health` | 3.202.1（compose 钉浮动 `langfuse/langfuse:3`） |
| **Python SDK**（backend 依赖包） | `CallbackHandler` 上报数据 | `uv run --project backend python -c "import langfuse; print(langfuse.__version__)"` | 4.5.1 |

两者版本序列独立、不必对齐（服务端没有 4.x；兼容靠接口协议，服务端 3.22+ 即支持 v4 SDK 的 OTel 摄入）。分工：**看板渲染归服务端**（查 UI 行为对 `langfuse/langfuse` 仓库 v3.202.1 tag），**type 判定/上报内容归 SDK**（对 `langfuse-python` 仓库 v4.5.1 tag）。注意 SDK 大版本升级有 API 语义变化（v3→v4 改过 trace_id 设置方式，`tracing/factory.py` 的 `trace_context` 写法即 v4 专属）——`uv` 重新解析依赖跳大版本时须核对 `tracing/factory.py`。

## 7. 已知边界

- **LLM-as-judge（UI 内置 Evaluator）不可用**：Langfuse SSRF 防护硬拦内网模型地址，判官一律走本地 Python 判定函数（详见 `../LANGFUSE.md` §5/§6.6）；judge 细则 md 备在各 L6 子集目录。
- 分数只增不改：重跑同 run_name 会追加而非覆盖——又一个「每次换新 run_name」的理由。
- trace 落库是异步的：runner 跑完最后几条的分数可能延迟数秒才可见（`_lf.wait_for_traces` 已在写入侧兜底轮询）。
