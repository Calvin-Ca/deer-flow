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

## 6. 已知边界

- **LLM-as-judge（UI 内置 Evaluator）不可用**：Langfuse SSRF 防护硬拦内网模型地址，判官一律走本地 Python 判定函数（详见 `../LANGFUSE.md` §5/§6.6）；judge 细则 md 备在各 L6 子集目录。
- 分数只增不改：重跑同 run_name 会追加而非覆盖——又一个「每次换新 run_name」的理由。
- trace 落库是异步的：runner 跑完最后几条的分数可能延迟数秒才可见（`_lf.wait_for_traces` 已在写入侧兜底轮询）。
