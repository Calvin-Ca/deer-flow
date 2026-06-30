# Langfuse 评测 / 反馈闭环 runner

把 `benchmark/` 金标集与 Langfuse 打通的三件事，全部在**服务器**上跑（本地 Mac 只改代码）。命令一律单行，用 `uv run --project backend` 使 `deerflow` / `langfuse` 可导入。

## 前置（一次）

1. 起 Langfuse 自托管栈：`cd docker/langfuse && docker compose up -d`（详见 `docker/langfuse/README.md`，UI 在 `:3030`）。
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

数据集口径见 `benchmark/routing_eval/README.md` 与 `benchmark/AGENT_BENCHMARK.md`。`retrieval_eval` / `agent_eval` 暂未接入 uploader，按相同套路扩 `upload_datasets.py` 即可。

## 任务3 · 线上反馈 → Langfuse score（无需脚本，已内建）

前端/渠道对某条回复点赞踩，Gateway 的 feedback 接口会**自动**把 ±1 作为 `user_feedback` score 回写到对应 run 的 trace —— 形成「线上真实反馈 → 评测样本」闭环。

实现：trace_id 由 `run_id` 确定性派生（`deerflow.tracing.build_langfuse_trace_id`），建 trace（worker）与回写 score（feedback 路由）同源重算，无需另存 trace_id。best-effort，langfuse 没开或失败只记 warning，不影响反馈本身。在 Langfuse UI 按 `user_feedback` score 过滤即可捞出被点踩的真实 case。
