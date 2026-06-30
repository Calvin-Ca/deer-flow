# Langfuse 接入与评测闭环说明

> 本文记录把 **Langfuse**（LLM 可观测性 + 评测平台）接入 deer-flow、并打通三件事的**架构、用法与踩坑过程**。
> 操作命令见 [`runner/README.md`](runner/README.md)；评测体系/分层口径见 [`AGENT_BENCHMARK.md`](AGENT_BENCHMARK.md)；自托管部署见 [`../docker/langfuse/README.md`](../docker/langfuse/README.md)。
> 版本：langfuse **4.5.1**（自托管）；首次打通日期 2026-06-30。

---

## 1. Langfuse 是什么 / 为什么用

开源、可自托管、数据不出内网的 **LLM 应用可观测性 + 评测**平台。对本项目（Qwen3-8B agent 编排 + 组价/规范知识）的价值集中在五块：

| 模块 | 作用 |
|---|---|
| **Tracing** | 一次 agent run = 一条 trace，嵌套每个节点 / LLM 调用 / 工具调用（`bash`/`read_file`/`ce-cost_*` MCP 等）的真实 prompt、参数、token、延迟、报错 |
| **Sessions / Users** | 按会话（映射 LangGraph `thread_id`）、按用户聚合多条 trace |
| **Datasets / Experiments** | 把金标集灌进来逐条跑、按 prompt variant / 模型横向比分 |
| **Scores** | 自动评测分 + 线上用户反馈分，挂到对应 trace |
| **Dashboard** | 按 tag/model/时间聚合成本、延迟、调用量、错误率 |

## 2. 已打通的能力

前 3 件是「脚本/后端驱动」、跑通即得；后 2 件是「Langfuse UI 侧」，代码只喂料、真正的「跑/评」在 UI 点（详见 §5）。

| # | 事 | 形态 | 入口 |
|---|---|---|---|
| 1 | **链路冒烟**：发一次对话→读回 trace，验标签正确 | 脚本 | `runner/smoke_test.py` |
| 2 | **benchmark→Dataset+自动评测**：金标灌进 Dataset，逐条跑 agent、程序化挂分 | 脚本 | `runner/upload_datasets.py` + `runner/run_routing_experiment.py` |
| 3 | **线上反馈→score 闭环**：前端点赞踩→回写成 trace 的 score | **后端内建** | `app/gateway/routers/feedback.py` |
| 4 | **Prompt Experiments**：自包含 intent 分类 prompt 纳管，UI 里换版本/模型横向比 | 脚本喂料 + UI | `runner/upload_prompts.py` + `prompts/intent_classify.txt`（§5.1） |
| 5 | **LLM-as-judge**：对已有 trace 自动打语义分（忠实度/选码合理性） | 细则留底 + UI | `judges/*.md`（§5.2） |

---

## 3. 架构：后端怎么接的

### 3.1 trace 元数据注入（既有）

`backend/.../tracing/metadata.py` 在每条 trace 上注入 Langfuse 保留字段，这是用好 Langfuse 的抓手：

| Langfuse 字段 | 来源 |
|---|---|
| `langfuse_session_id` | LangGraph `thread_id`（会话聚合） |
| `langfuse_user_id` | 有效用户（no-auth 下为 `default`） |
| `langfuse_tags` | `env:<环境>` + `model:<模型名>` + `variant:<prompt 版本>` |

> `variant:` 来自 `lead_agent.system_prompt_path` 的文件名 stem——做 prompt 消融时不同版本的 trace 能直接在 UI 按 `variant:` 过滤分组比。

### 3.2 确定性 trace_id（本次新增·地基）

三件事里，feedback 回写（任务3）要把分挂到某条 trace 上，但 Langfuse 默认 trace_id 是随机的、事后找不回。**契约**：

```
trace_id = Langfuse.create_trace_id(seed=run_id)
```

发起 run 时（worker）用它绑定，事后按同一 `run_id` 重算就得同一 trace_id，无需持久化。关键事实（langfuse 4.5.1）：**自定义 trace_id 只能在构造 `CallbackHandler(trace_context={"trace_id": ...})` 时传，不能走 metadata**。

涉及改动（commit `47d22727`）：

| 文件 | 改动 |
|---|---|
| `tracing/factory.py` | 新增 `build_langfuse_trace_id(run_id)` / `get_langfuse_client()`；`build_tracing_callbacks(langfuse_trace_id=)` 经 `trace_context` 绑定 |
| `runtime/runs/worker.py` | 发起 run 时算 trace_id 塞进 `configurable` |
| `agents/lead_agent/agent.py` | `make_lead_agent` 读出并透传给 handler |
| `app/gateway/routers/feedback.py` | create/upsert 后 best-effort `create_score(name="user_feedback", value=±1, trace_id=...)` |

> feedback 回写是 **best-effort**：langfuse 没开或失败只记 warning，绝不阻断反馈接口。

### 3.3 测试

`backend/tests/test_tracing_factory.py`（改 2 + 新增 2）、`test_feedback_langfuse_score.py`（新增 3）。本地 `uv run --project backend python -m pytest` 全绿。

---

## 4. 评测 runner（任务2 细节）

`benchmark/runner/` 三脚本，服务器上 `uv run --project backend python ...` 跑。命令见 `runner/README.md`。

- `smoke_test.py`：驱动一次对话→`api.trace.list(session_id=...)` 读回→断言 `session_id`/`model:`/`variant:` 标签。
- `upload_datasets.py`：`routing_eval/agent_routing_eval.jsonl` → Langfuse Dataset `agent-routing-eval`，以用例号作 item id（幂等 upsert）。
- `run_routing_experiment.py`：逐条把 query 喂默认 lead agent，从工具调用判两项，读回 trace → `dataset_run_items.create` 关联进 dataset run + `create_score` 挂分。

**判定口径**（对标 `routing_eval/README` 两率）：
- `route_correct`：该调脚本就调——按**工具名**判（`qa.py` / `cost.py` / `ce-cost_*`），bash/read_file 瞎折腾不算。
- `clarify_correct`：该反问就反问——命中 `ask_clarification` 工具。

---

## 5. UI 侧评测：Prompt Experiments 与 LLM-as-judge

§4 的 runner 是「脚本驱动 agent 真跑」——测路由这类**带工具调用**的能力必须走它。本节是另两条**主体在 Langfuse UI 上点**的路子，代码侧只「喂料」（推 prompt、留 judge 细则），适合**单跳 prompt 消融**和**对已有 trace 自动打分**。

> **边界**：Prompt Experiments 只做「单 prompt → 一次 LLM 调用」，**跑不了完整 agent**（无工具/MCP/路由）。所以它**测不了路由两率**（那条只能走 `run_routing_experiment.py`）；它的位置是「自包含单跳 prompt 的横向比」。

### 5.0 前置：在 UI 配一个 model connection（两功能共用）

Settings → LLM Connections → Add：填被测/judge 用的模型(走项目内网 LLM 的 OpenAI 兼容端点即可)。Prompt Experiments 的「跑」和 LLM-as-judge 的「评」都要它，没有它两个都点不动。

### 5.1 Prompt Experiments —— 纳管 intent 分类 prompt，UI 里横向比

意图分类被抽成**自包含单跳** prompt（`benchmark/prompts/intent_classify.txt`，`{{query}}` → `norm|cost|both|clarify|chat`），不需要 agent，正好喂 Prompt Experiments。

1. 推进 Prompt Management（服务器上）：
   ```
   uv run --project backend python benchmark/runner/upload_prompts.py
   ```
   → UI Prompts 里出现 `intent-classify`（带 `production` 标签的版本）。改口径只改那个 txt 重跑本脚本，**新版本累积、旧版本保留可回溯**。
2. 跑实验：Datasets → `agent-routing-eval` → **New experiment** → 选 `intent-classify` prompt + 5.0 的 model connection → Run。它对每条 item 用 `{{query}}` 跑一次出意图标签。
3. 比/评：多发几个 prompt 版本各跑一次，UI 里并排比；要自动判对错，给该 experiment 挂一个 evaluator（最简：LLM-judge 比对输出标签与金标里能推出的期望意图——金标 `agent` 字段 norm-qa→norm / cost-agent→cost，`expect_clarify=true & 无版本`→clarify）。

**单一事实源**：prompt 文本在 git 的 `intent_classify.txt`，UI 里的版本是它的快照。别在 UI 直接改 prompt 而不回写文件，否则 Mac↔服务器 git 流里会丢。

### 5.2 LLM-as-judge —— 对已有 trace 自动打分

judge 评的是**已落库的 trace**，配好后新 trace 自动出分。两份评分细则已在 git 留底，**改口径改文件、再到 UI 更新 Prompt 版本**：

| Judge | 细则文件 | score 名 | 评谁 |
|---|---|---|---|
| norm-qa 忠实度（防幻觉） | `benchmark/judges/norm_faithfulness.md` | `norm_faithfulness` | norm 类 trace：答案是否只用了 `cited_clauses`、没编造条文号 |
| cost 选码合理性 | `benchmark/judges/cost_code_selection.md` | `cost_code_reasonable` | cost 类 trace：9 位编码与描述/版本是否匹配、有无串库 |

配法（UI）：Evaluators → New evaluator → Custom(LLM-as-judge) → 选 model connection → 把细则文件里「判官 Prompt」整段粘进去 → 按文件里的 **Variable mapping** 把 `{{...}}` 映射到 trace 字段 → 限定 Target 到对应能力的 trace（别让忠实度 judge 去评 cost）。细节、打分口径、建议门全在各自 md 文件里。

> 与确定性判分互补：judge 管「说的有没有出处 / 码选得合不合理」这类**语义**判断；`run_routing_experiment.py` 那种**确定性**两率（调没调对工具）继续留在脚本里，更准更省。

---

## 6. 踩坑记录（这一路怎么趟过来的）

留这一节是因为这些坑会复现，且根因不直觉。

### 6.1 `dataset.run_experiment` 与 MCP 持久会话生命周期相撞 → 弃用

**现象**：用 langfuse 的 `dataset.run_experiment(task=...)` 跑，崩在
`RuntimeError: Attempted to exit cancel scope in a different task` / `Task was destroyed but it is pending` / `athrow(): asynchronous generator is already running`。

**根因**：`run_experiment` 在自己的 asyncio 事件循环线程里**同步**调 task（见 `langfuse/experiment.py::_run_task`），而 `DeerFlowClient.stream` 内部自驱 async + 维持到 :8100 的**持久 MCP（streamable_http）会话**；两个事件循环/任务边界一打架，会话反复建/拆就炸。

**走过的弯路**：
1. 把 agent 驱动丢进独享线程隔离 → 改报 `There is no current event loop in thread`（Py3.12 非主线程默认无 loop，而 `mcp/cache.py` 用老式 `asyncio.get_event_loop()`）。
2. 线程内补 `new_event_loop()+set_event_loop()` → 又在 `loop.close()` 时报 `Task was destroyed`（关 loop 时 MCP 后台任务没收干净）。

**最终解法（plan B，commit `312e29a0`）**：**彻底弃用 `run_experiment`**，改成**主线程、逐条、不另起 loop**——与 `smoke_test.py` 完全同构（那条路已验证干净退出）。dataset run 关联改用低层 `langfuse.api.dataset_run_items.create(run_name=, dataset_item_id=, trace_id=)` 手动挂，UI 里的 Runs 对比表照样有。

> 残留：仍有少量 MCP 会话 GC 噪音（`GeneratorExit` 等），**非致命、不影响结果**（run 完整跑完、分数正确）。要彻底消需把整轮跑进单个 asyncio 事件循环，改动较大，暂缓。

### 6.2 `my_package.mcp.auth` 拦截器报错 → 无害，清掉即可

**现象**：每次起 agent 都打一段 `ModuleNotFoundError: No module named 'my_package'` 的 traceback。

**真相**：`extensions_config.json` 顶层 `mcpInterceptors` 配了个**从没实装的占位拦截器** `my_package.mcp.auth:build_auth_interceptor`。但 `mcp/tools.py:227` 加载拦截器是包在 `try/except` 里的——**失败只记 warning 并跳过**，`ce-cost`(:8100) 工具照常加载。所以**无害**，不影响路由。

> 拦截器 = MCP 工具调用的中间件（鉴权注入/审计/限流/护栏短路等横切关注点）。`mcpInterceptors` 要的是 `模块:函数` 形式的**构造函数**，不是 server，不能把 `ce-cost` 填进去。`ce-cost` 是 localhost 开放服务、无需拦截，把 `mcpInterceptors` 置空即可消噪音。

### 6.3 `--model qwen-plus` 报 not found

config.yaml 里没有叫 `qwen-plus` 的模型。先用 `DeerFlowClient().list_models()` 查真实模型名，或不带 `--model` 用默认。

### 6.4 路由判定别靠流式 args 子串

最初 `did_route` 在「工具名 + 流式 args 文本」里搜 `qa.py`/`:8100` 等子串。但**流式 tool_call 的 args 是分片的**，bash 命令常捕获不全。改为**纯按工具名**判（名字在首片即到齐），稳健且合 README 口径（commit `69c2e22b`）。

### 6.5 Mac↔服务器 git：派生数据挡 pull

服务器侧 `structured/chunks/*`、`notebooks/results/*`、`uv.lock` 是工作树里的未提交改动，会挡住 pull。按约定它们**该入 git 同步**（`uv.lock` 以服务器为准）——在**服务器**上 `git add ce-code ce-services && git commit` 正式提交，之后 Mac 也能同步，不必每次 `stash/pop`。

---

## 7. 首轮基线结果（默认 Qwen3-8B，run `v2_runbook`）

| 指标 | 值 | 建议门 |
|---|---|---|
| 路由率（expect_route 真去调工具） | **25%** (4/16) | ≥0.8 |
| 红线遵守率（no_version 真先反问） | **67%** (4/6) | ≥0.95 |

**解读**：真路由的只有 4 条（B4 调 `ce-cost_*` MCP 工具，A1/A3/A5 调 `qa.py`）；大量用例在 `bash`+`read_file` 自己瞎折腾、甚至零工具直接答。与「Qwen3-8B function-calling 不可靠」吻合。按 `routing_eval/README §0`，红线遵守率远低于 0.95 → 命中「该升级方案 A」信号——**但需先在 README 要求的 flash/thinking/pro 档复测**（Qwen3-8B 是弱基线），换强模型大概率两率回升。

---

## 8. 后续 TODO

- [ ] 换强模型（真实 config 模型名）重跑，多 `--run-name` 在 Langfuse Runs 横向比，定方案 0/A。
- [ ] `retrieval_eval` / `agent_eval` 接入 `upload_datasets.py`（同套路扩展）。
- [ ] 想清噪音后再消 MCP 会话 GC 报错（§6.1 残留）。根因是同进程跨多个事件循环复用全局 MCP 缓存——**串行救不了，靠进程/循环隔离**。两条路线择一：① 把整轮跑进**单个 asyncio 事件循环**（用 agent 异步接口逐条 await，会话全程同 loop）；② **子进程隔离**，每条用例 fork 独立进程跑（复刻已验证干净的冒烟测试路径，更稳、不改异步驱动，但 17× 冷启动明显变慢）。纯美观收益，不损结果，优先级低。
- [x] norm-qa 忠实度 / cost 选码合理性走 LLM-as-judge：评分细则已落 `benchmark/judges/*.md`，UI 配法见 §5.2。
- [ ] 两份 judge 先在小批人标样本上**校准**（核对 judge 判 0/1 与人判一致率），再正式上量；忠实度可进一步对接 RAGAS。
- [ ] intent 分类 Prompt Experiment 接一个自动 evaluator（输出标签 vs 金标期望意图），免去人工比对（§5.1）。
