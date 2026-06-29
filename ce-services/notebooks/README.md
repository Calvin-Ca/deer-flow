# ce-services 实验体系（编排层）

> 任务层/编排层造价轨的**实验记录规范**。对象是 **lead-agent 的提示词 / 工具面 / 路由行为**（problem 7 的 S1 路由·S2 澄清·S3 调用·S4 转达），与 `ce-code/notebooks/`（知识层 K1 召回·K2 选码·K3 取数）分属两层、各自归因。
>
> 四件套规范（背景/配置+脚本/结果/分析、dated 文件夹命名、experiments.md timeline、五条原则）**完全沿用** `ce-code/notebooks/README.md`，此处只记两层差异，不重复造轮子。

---

## 1. 与 ce-code 实验体系的差异

| 维度 | ce-code（知识层） | ce-services（编排层，本目录） |
|---|---|---|
| 测什么 | 检索召回 / 选码 / 取数（K1–K3） | 路由 / 澄清 / 调用 / 转达（S1–S4） |
| 跑法 | 直接打 :8100/:8101 HTTP，绕开 agent，确定性、秒级 | 经 **lead-agent**（`DeerFlowClient` in-process），看模型行为 |
| 评测集 | `benchmark/retrieval_eval/match_gold*.jsonl` | `benchmark/routing_eval/agent_routing_eval.jsonl` |
| 指标 | recall@k / Top-1 / MRR | 路由率 / 红线遵守率 / web兜底率 / 越界拒答率 |
| 判读 | 自动（金标比对） | 自动 harness（解析 stream tool call）或前端人工 |
| 变量 | 索引/重排/数据 | **system prompt / 工具面 / 模型档** |

## 2. 工作流（同 ce-code：服务器跑 → 本地分析）

1. **本地（开跑前）**：照 `_template/` 起 dated 文件夹，写 README 的「背景/假设 + 配置 + 变量」段 + `run.sh`，commit & push。
2. **服务器（跑）**：`git pull` → `bash ce-services/notebooks/<文件夹>/run.sh`（结果 tee 进 `results/run.log`），产出 commit & push。
3. **本地（跑后）**：`git pull` 取回 `results/`，补 README 的「结果 + 分析 + 结论 + 下一步」，在 `experiments.md` 顶部加精炼结论，commit & push。

> 结果小文本（指标表/日志/逐条 trace）**入 git**（跨机分析必需）。

## 3. 文件夹命名 / 内容

同 ce-code：`<YYYY-MM-DD-HHMM>-<短描述>/`，内含 `README.md` + `run.sh` + `results/`。取时间 `date "+%Y-%m-%d-%H%M"`。照 `_template/` 起新实验。提示词类实验额外放 `prompts/` 存各变体原文（含被 git 找回的历史基线，作对照锚点 + 防丢失）。

## 4. 结论速查

`experiments.md` = 精炼结论时间线（E1、E2… 一条一行，标 `✅采纳 / ⛔负结果 / 🟡待续` + 回链 dated 文件夹）。

## 5. 原则

沿用 ce-code 五条：**一次只动一个变量**（提示词实验只改 prompt，不同时换模型/工具面）、**留负结果**、**真实数字**、**可复现**（`run.sh` + commit 号）。编排层补一条：**判读口径写死在 harness**（problem 6 教训：人工判读"调没调脚本/反没反问"易飘，靠解析 tool call 事件确定化）。
