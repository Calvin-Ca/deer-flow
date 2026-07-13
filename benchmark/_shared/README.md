# Langfuse 评测 / 反馈闭环 runner（`_shared/` = 跨层基建）

把 `benchmark/` 金标集与 Langfuse 打通的三件事，全部在**服务器**上跑（本地 Mac 只改代码）。命令一律单行，用 `uv run --project backend` 使 `deerflow` / `langfuse` 可导入。

> 2026-07-11 重组后：本目录只放**跨层共用**的基建（`_lf.py`/`_paths.py` 引导、`upload_datasets.py` 灌库、`probe_gateway.py` 探针、`smoke_test.py` 冒烟、`dump_run_scores.py` 拉分）；各层的 `run_*_experiment.py` 已搬进所属层目录（L1_routing / L3_retrieval / L6_agent 各子集），与数据、判分器同住。目录 ↔ 层映射见 [`../README.md`](../README.md)。本文档保留全链路 runbook（下述命令路径已更新）。
>
> **跑完去哪看分**：Langfuse UI 看板速读（页面导航 / Runs 找分 / variant 对比 / 单条归因 / 标签约定）见 [`langfuse.md`](langfuse.md)。

## 前置（一次）

1. 起 Langfuse 自托管栈：`cd docker/ce-langfuse && docker compose up -d`（详见 `docker/ce-langfuse/README.md`，UI 在 `:3030`）。
2. 根 `.env` 开上报四行并**重启 Gateway**（env 仅启动时读）：
   `LANGFUSE_TRACING=true` / `LANGFUSE_PUBLIC_KEY=...` / `LANGFUSE_SECRET_KEY=...` / `LANGFUSE_BASE_URL=http://localhost:3030`

## 任务1 · 链路冒烟（先确认 trace 能落库）

```
uv run --project backend python benchmark/_shared/smoke_test.py
```

驱动一次真实对话，再从 Langfuse 读回该会话 trace，断言 `session_id` / `model:` / `variant:` 标签都对。退出码 0=通。先跑通这个，再上 dataset / feedback。

## 任务2 · benchmark → Dataset + 自动评测

先上传金标集（幂等，以用例号作 item id 覆盖）：

```
uv run --project backend python benchmark/_shared/upload_datasets.py
```

再跑实验（建议 `--run-name` 填当前 prompt variant，便于在 UI 横向比；四服务需起齐使 agent 真能调脚本）：

```
uv run --project backend python benchmark/L1_routing/run_routing_experiment.py --run-name v2_runbook --model qwen-plus
```

自动算两率并把每条结果 + 分数挂到 Langfuse 的 dataset run：
- `clarify_correct`：该反问就反问（命中 `ask_clarification`）——红线主判据；
- `route_correct`：该调脚本就调（工具名命中 `ROUTE_TOOL_NAMES`：`cost_workflow_*` / `bill_match` / `quota_recommend` / `price_query` / `cost_calc` / `task` /（tool_search 关闭态）lead 直调 `ce-rag_search_clause`）。

> **判定是外部观测启发式**：路由是否发生靠匹配 agent 实际调的工具名是否在 `ROUTE_TOOL_NAMES`（**精确名集合、不用前缀**；见 `run_routing_experiment.py` 顶部常量）。跑首轮后照真实 trace 里 agent 的实际工具名回校该常量，再上量。
>
> **实现说明**：脚本**主线程逐条**跑（与 `smoke_test.py` 同一调用路径，已验证干净退出），**未用** `dataset.run_experiment`——后者在自己的事件循环里调 task，会和 `DeerFlowClient.stream` 的持久 MCP 会话生命周期相撞而崩（cancel scope / Task destroyed）。每条跑完读回其 trace，用 `dataset_run_items.create` 把这条 agent trace 直接关联进同名 dataset run，再 `create_score` 挂 `route_correct` / `clarify_correct`——所以 UI 里 Datasets→Runs 下每条就是 agent 自己的完整 trace，分数也挂在同一条上，不再是两棵分离的树。

### 任务2 扩展 · 清单匹配评测（L3_retrieval 之 match）

「清单匹配」= 构件描述 → 9 位清单码。**复用 `benchmark/L2_gating/select_eval/tools/eval_select.py` 的成熟评测**（bill_match 召回 + select_code 选码 + Recall@k / 端到端 Top-1 / 候选内 Top-1 / 高置信错码=0 红线；原属已退役的 ce-services，随其退役迁入本仓 `benchmark/L2_gating/select_eval/`），只加 Langfuse 层。需 :8100 知识服务 + :8099 vLLM 在跑。

先灌金标（2013+2024 合进一个 dataset）：

```
uv run --project backend python benchmark/_shared/upload_datasets.py --only clist
```

再按版本跑（每次一个 spec；`--run-name` 建议带版本便于横向比）：

```
uv run --project backend python benchmark/L3_retrieval/run_retrieval_experiment.py --spec 2024 --run-name clist_2024_v1
```

逐条挂 `match_top1`（端到端选码命中）/ `recalled`（金标是否进候选）两分到 `clist-match-eval` 的 dataset run；终端同时打印 eval_select 的完整指标表（含 PRD Top-1≥85% 红线对照）。

> 范围：只接了**清单匹配**（`match_gold*.jsonl`）。**条文召回**（`gb50016_eval.json`）的 standard `gb50016` 不在 agent 面允许清单（默认仅 gb50500/50854-2013）——跑该评测时以 ce-code env `CE_RAG_AGENT_STANDARDS` 放开 gb50016（其索引已建），再按同套路加驱动。`match_gold_2013_uncovered.jsonl` 是「库未覆盖码」清单（无 query），量覆盖缺口、不进召回评测。

### 任务2 扩展 · L6_agent 三子集（先接管道，数据后补）

`L6_agent/{toolcall,cost_task,norm_faithful}` 当前多为 **2 行 sample 模板**。按「先接进来、数据以后补」：upload 优先读真金标 `{name}.jsonl`、没有才回退 `{name}.sample.jsonl`——**补好同名 `.jsonl` 重传即覆盖，无需改代码**。

全部金标一次灌（routing/clist/toolcall/cost_task/norm_faithful/clause）：

```
uv run --project backend python benchmark/_shared/upload_datasets.py
```

或单传：`--only toolcall` / `--only cost_task` / `--only norm_faithful` / `--only clause`。

**已能跑的 runner —— 工具调用评测**（需四服务起齐）：

```
uv run --project backend python benchmark/L6_agent/toolcall/run_toolcall_experiment.py --run-name toolcall_v1 --model qwen-plus
```

逐条挂 `tool_correct`（调没调对工具）/ `call_correct`（工具名对 + args 按 `arg_match` 命中）。
⚠️ 金标 `expected_call.tool` 须用**真实 agent 工具名**（`cost_workflow_*` / `ce-rag_*` / `ce-db_*` / `task` 等）；sample 里的 `query_bill_8100` 是理想名，照搬恒不命中。

**已能跑的 runner —— cost_task 端到端组价评测**（τ-bench 式终态 + pass^k，需 :8100/:8102/:8099 起齐）：

```
uv run --project backend python benchmark/L6_agent/cost_task/run_cost_task_experiment.py --run-name cost_v1 --split test [--limit N] [--no-langfuse]
```

比 **agent 落定的最终清单码**（不比工具名/答案文本）+ 溯源 + 红线行为，逐条挂 `task_pass` / `redline_ok`；
聚合出**任务成功率 pass^k（连跑全过）+ 红线违规率(独立，门=0) + 逐 difficulty + evaluable 覆盖率**。
判定器 `benchmark/L6_agent/cost_task/cost_task_score.py` 是**纯函数、已单测**（`test_cost_task_score.py`，22 例），
外部判不了的红线（`no_rag_calc`/`no_fabricate_code` 等）诚实标 not_evaluable、不假装通过。
> 终态码抽取是启发式（正则捞 9 位码，final=答案里最后一个）；首轮实跑后可按真实工具结果结构收紧（同 routing 的常量回调思路）。

**已能跑的 runner —— norm_faithful 规范问答忠实度（traditional↔agentic 对比）**（需 :8100 + :8099）：

```
uv run --project backend python benchmark/L6_agent/norm_faithful/run_norm_faithful_experiment.py --mode traditional --run-name norm_base
uv run --project backend python benchmark/L6_agent/norm_faithful/run_norm_faithful_experiment.py --mode agentic   --run-name norm_agentic
```

`--mode` 设 `CE_NORM_FAITHFULNESS_CHECK`（agentic=开引用回查/traditional=关基线）+ 打 variant 标签。判定器
`benchmark/L6_agent/norm_faithful/norm_faithful_score.py`（纯函数、10 例单测）：**忠实率**（引用条款号∈检索证据，复用
`app.ce.norm.faithfulness`，即 agentic RAG 招牌）+ **误拒/漏拒率** + **答案要点覆盖** + **std 级上下文召回**；
拒答用例不算忠实率。**先跑 traditional 立基线再跑 agentic**，比忠实率↑/幻觉率↓（MS.md ⑤ ablation）。
> 忠实度的 RAGAS**论断落地**那半（每个论断是否真从条文推出）走 LLM-judge（`L6_agent/norm_faithful/norm_faithfulness.md`），
> 撞 SSRF（§6.6）待出路；本 runner 做的是**可程序化判定**的忠实率/拒答/覆盖/召回四项。

**待补前置、runner 暂未建的**：

| 集 | runner 卡在哪 |
|---|---|
| `clause`（gb50016） | standard `gb50016` 不在 qa.py 支持列表；待知识服务加载 GB50016 |
| `adversarial` / `trajectory` | 对抗红线鲁棒性 / 多轮轨迹，待建对应 runner（判定器可复用 cost_task_score / norm_faithful_score 思路） |

数据集口径见 `benchmark/L1_routing/README.md` 与 `benchmark/AGENT_BENCHMARK.md`（§L3 Recall@k、§L6-B arg_match）。

## 任务4 · UI 侧 LLM-as-judge 喂料（无脚本）

主体在 Langfuse UI 上点，代码只留评分细则。完整 UI 操作步骤见 `../LANGFUSE.md §5`。

评分细则在 `../L6_agent/norm_faithful/norm_faithfulness.md`、`../L6_agent/cost_task/cost_code_selection.md`，把里面「判官 Prompt」粘进 UI 的 Evaluator 即可（变量映射、打分口径都写在各 md 里）。

## 任务3 · 线上反馈 → Langfuse score（无需脚本，已内建）

前端/渠道对某条回复点赞踩，Gateway 的 feedback 接口会**自动**把 ±1 作为 `user_feedback` score 回写到对应 run 的 trace —— 形成「线上真实反馈 → 评测样本」闭环。

实现：trace_id 由 `run_id` 确定性派生（`deerflow.tracing.build_langfuse_trace_id`），建 trace（worker）与回写 score（feedback 路由）同源重算，无需另存 trace_id。best-effort，langfuse 没开或失败只记 warning，不影响反馈本身。在 Langfuse UI 按 `user_feedback` score 过滤即可捞出被点踩的真实 case。
