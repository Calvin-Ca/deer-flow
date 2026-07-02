# Langfuse 评测 / 反馈闭环 runner

把 `benchmark/` 金标集与 Langfuse 打通的三件事，全部在**服务器**上跑（本地 Mac 只改代码）。命令一律单行，用 `uv run --project backend` 使 `deerflow` / `langfuse` 可导入。

## 前置（一次）

1. 起 Langfuse 自托管栈：`cd docker/ce-langfuse && docker compose up -d`（详见 `docker/ce-langfuse/README.md`，UI 在 `:3030`）。
2. 根 `.env` 开上报四行并**重启 Gateway**（env 仅启动时读）：
   `LANGFUSE_TRACING=true` / `LANGFUSE_PUBLIC_KEY=...` / `LANGFUSE_SECRET_KEY=...` / `LANGFUSE_BASE_URL=http://localhost:3030`

## 任务1 · 链路冒烟（先确认 trace 能落库）

```
uv run --project backend python benchmark/runner/smoke_test.py
```

驱动一次真实对话，再从 Langfuse 读回该会话 trace，断言 `session_id` / `model:` / `variant:` 标签都对。退出码 0=通。先跑通这个，再上 dataset / feedback。

## 任务2 · benchmark → Dataset + 自动评测

先上传金标集（幂等，以用例号作 item id 覆盖）：

```
uv run --project backend python benchmark/runner/upload_datasets.py
```

再跑实验（建议 `--run-name` 填当前 prompt variant，便于在 UI 横向比；四服务需起齐使 agent 真能调脚本）：

```
uv run --project backend python benchmark/runner/run_routing_experiment.py --run-name v2_runbook --model qwen-plus
```

自动算两率并把每条结果 + 分数挂到 Langfuse 的 dataset run：
- `clarify_correct`：该反问就反问（命中 `ask_clarification`）——红线主判据；
- `route_correct`：该调脚本就调（工具名/ bash 命中 `qa.py`/`cost.py` 等 `ROUTE_SIGNALS`）。

> **判定是外部观测启发式**：路由是否发生靠匹配工具调用文本里的 `ROUTE_SIGNALS`（见 `run_routing_experiment.py` 顶部常量）。跑首轮后照真实 trace 里 agent 的实际调用方式（多半是带 `ce-cost` 前缀的 MCP 工具名）回调该常量，再上量。
>
> **实现说明**：脚本**主线程逐条**跑（与 `smoke_test.py` 同一调用路径，已验证干净退出），**未用** `dataset.run_experiment`——后者在自己的事件循环里调 task，会和 `DeerFlowClient.stream` 的持久 MCP 会话生命周期相撞而崩（cancel scope / Task destroyed）。每条跑完读回其 trace，用 `dataset_run_items.create` 把这条 agent trace 直接关联进同名 dataset run，再 `create_score` 挂 `route_correct` / `clarify_correct`——所以 UI 里 Datasets→Runs 下每条就是 agent 自己的完整 trace，分数也挂在同一条上，不再是两棵分离的树。

### 任务2 扩展 · 清单匹配评测（retrieval_eval 之 match）

「清单匹配」= 构件描述 → 9 位清单码。**复用 `ce-services/tools/eval_select.py` 的成熟评测**（bill_match 召回 + select_code 选码 + Recall@k / 端到端 Top-1 / 候选内 Top-1 / 高置信错码=0 红线），只加 Langfuse 层。需 :8100 知识服务 + :8099 vLLM 在跑。

先灌金标（2013+2024 合进一个 dataset）：

```
uv run --project backend python benchmark/runner/upload_datasets.py --only clist
```

再按版本跑（每次一个 spec；`--run-name` 建议带版本便于横向比）：

```
uv run --project backend python benchmark/runner/run_retrieval_experiment.py --spec 2024 --run-name clist_2024_v1
```

逐条挂 `match_top1`（端到端选码命中）/ `recalled`（金标是否进候选）两分到 `clist-match-eval` 的 dataset run；终端同时打印 eval_select 的完整指标表（含 PRD Top-1≥85% 红线对照）。

> 范围：只接了**清单匹配**（`match_gold*.jsonl`）。**条文召回**（`gb50016_eval.json`）的 standard `gb50016` 不在 qa.py 支持列表（仅 gb50500/50854/50856），待知识服务加载该规范后再按同套路加 qa.py 驱动。`match_gold_2013_uncovered.jsonl` 是「库未覆盖码」清单（无 query），量覆盖缺口、不进召回评测。

### 任务2 扩展 · agent_eval 三子集（先接管道，数据后补）

`agent_eval/{toolcall,cost_task,norm_faithful}` 当前多为 **2 行 sample 模板**。按「先接进来、数据以后补」：upload 优先读真金标 `{name}.jsonl`、没有才回退 `{name}.sample.jsonl`——**补好同名 `.jsonl` 重传即覆盖，无需改代码**。

全部金标一次灌（routing/clist/toolcall/cost_task/norm_faithful/clause）：

```
uv run --project backend python benchmark/runner/upload_datasets.py
```

或单传：`--only toolcall` / `--only cost_task` / `--only norm_faithful` / `--only clause`。

**已能跑的 runner —— 工具调用评测**（需四服务起齐）：

```
uv run --project backend python benchmark/runner/run_toolcall_experiment.py --run-name toolcall_v1 --model qwen-plus
```

逐条挂 `tool_correct`（调没调对工具）/ `call_correct`（工具名对 + args 按 `arg_match` 命中）。
⚠️ 金标 `expected_call.tool` 须用**真实 agent 工具名**（qa.py/cost.py/ce-cost_*）；sample 里的 `query_bill_8100` 是理想名，照搬恒不命中。

**待补前置、runner 暂未建的**：

| 集 | runner 卡在哪 |
|---|---|
| `cost_task` | 端到端终态校验（`terminal_check`）要跑完整 agent + 解析组价终态 schema，待真金标 + 终态校验器 |
| `norm_faithful` / `clause`（gb50016） | standard `gb50016` 不在 qa.py 支持列表；忠实度类指标走 LLM-judge 又撞 SSRF（§6.6）。待知识服务加载 GB50016 + SSRF 出路 |

数据集口径见 `benchmark/routing_eval/README.md` 与 `benchmark/AGENT_BENCHMARK.md`（§L3 Recall@k、§L6-B arg_match）。

## 任务4 · UI 侧评测喂料（Prompt Experiments / LLM-as-judge）

这两条主体在 Langfuse UI 上点，脚本只负责把「料」推上去。完整 UI 操作步骤见 `../LANGFUSE.md §5`。

把自包含 intent 分类 prompt 纳管进 Prompt Management（供 UI 里 New experiment 选用）：

```
uv run --project backend python benchmark/runner/upload_prompts.py
```

→ UI Prompts 里出现 `intent-classify`。prompt 文本在 `../prompts/intent_classify.txt`（单一事实源，改它重跑即发新版本）。

LLM-as-judge **无脚本**：评分细则在 `../judges/norm_faithfulness.md`、`../judges/cost_code_selection.md`，把里面「判官 Prompt」粘进 UI 的 Evaluator 即可（变量映射、打分口径都写在各 md 里）。

## 任务3 · 线上反馈 → Langfuse score（无需脚本，已内建）

前端/渠道对某条回复点赞踩，Gateway 的 feedback 接口会**自动**把 ±1 作为 `user_feedback` score 回写到对应 run 的 trace —— 形成「线上真实反馈 → 评测样本」闭环。

实现：trace_id 由 `run_id` 确定性派生（`deerflow.tracing.build_langfuse_trace_id`），建 trace（worker）与回写 score（feedback 路由）同源重算，无需另存 trace_id。best-effort，langfuse 没开或失败只记 warning，不影响反馈本身。在 Langfuse UI 按 `user_feedback` score 过滤即可捞出被点踩的真实 case。
