# Agent 任务级评测集（outcome 层）

> 框架定义见 [`../AGENT_BENCHMARK.md`](../AGENT_BENCHMARK.md)：层定义见 **§2-L6**，任务级 schema 见 **§4.4**。本目录放「整件事做没做成」的任务级数据（L6 层），与 `../routing_eval`（请求级路由）、`../retrieval_eval`（检索/匹配金标）互补，靠 `id` 串接。

## 三个子层

| 目录 | 子层 | 对标范式 | 测谁 | schema 见 |
|---|---|---|---|---|
| `cost_task/` | L6-A 端到端组价 | τ-bench | cost agent 整体多轮跑 | §4.4 |
| `toolcall/` | L6-B 工具调用 | BFCL | 工具选择 + 参数填充 | §4.4 |
| `norm_faithful/` | L6-C 规范问答忠实度 | RAGAS | norm-qa（本质 RAG） | §4.4 |

每个子目录各有一份 `*.sample.jsonl`，是**照 §4.4 schema 填好的一条样例**，构建数据集时复制字段、删掉 `.sample` 后缀按 `split` 累积即可。

## 三条铁律（建数据时务必守住，来由见 §2-L6）

1. **pass^k 而非 pass@k**：每条 case 标 `pass_k`（建议 5），连跑全过才算过——测 Qwen3-8B 一致性、不漂移。
2. **红线违规率独立计分**：`policy` 字段单独判，不糅进任务成功率；违规即 case fail，门线 = 0。
3. **规范问答别套 agent 框架**：L6-C 用 RAGAS 忠实度指标，不要塞进 L6-A 的终态判定。

## 判定方式（见 §2-L6）

- `cost_task` / `toolcall`：程序化判定，比**最终状态/工具调用**，不比答案文本，可进 CI。
- `norm_faithful`：RAGAS LLM-judge，**judge 须先在小批人标样本上校准一致性**再上量。

## 切分约定

- `split`: `dev`（调阈值/权重/judge prompt）/ `test`（冻结，只跑一次出验收数）。门控/judge 调参只许碰 `dev`。
