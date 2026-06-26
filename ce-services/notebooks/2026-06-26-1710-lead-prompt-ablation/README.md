# 实验 2026-06-26-1710 · lead-agent 提示词消融（problem 6 编排层路由）

> 状态：🟡 进行中（脚手架已就位，待服务器跑分）。承接：`PROBLEM.md` §6（弱模型把 skill 当工具 + 危险兜底）/ §7（分层评测 S1 路由·S2 红线）。

## 1. 背景与假设

- **问题**：Qwen3-8B 在渐进披露下把 `cost-agent`/`norm-qa` 当成「工具表里的工具」，找不到就判"不可用"→ 退回 web_search（problem 6 现象）。**提示词优化**是 problem 6 候选解法 A1 的载体——把能力从「文档面（SKILL.md，要先 read_file）」抬到「resident system prompt 面」，消除多跳间接 + 纠正"一任务一工具"先验。
- **假设**（可证伪）：把两个脚本的死命令模板/参数/"是脚本不是工具"的先验纠正**饱和加载**进常驻 prompt（变体 V2），相对当前精简版（V1）能显著提高**路由率**（尤其 no_version 用例拿到版本后的第二轮），且**红线遵守率不下降**、**web 兜底率维持 0**。原始通用 super-agent（V0）作无版本红线的对照锚点，预计红线遵守率显著低于 V1/V2。

## 2. 配置

- **代码版本**：commit `<填跑时短哈希>`（提示词定义见 `backend/packages/harness/deerflow/agents/lead_agent/prompt.py:SYSTEM_PROMPT_TEMPLATE`）。
- **本次只动的变量**：**仅 system prompt 模板**（其余工具面/模型/知识层全不动）。三变体见 `prompts/`：
  | 变体 | 文件 | 说明 |
  |---|---|---|
  | **V0 原始** | `deerflow_prompt.txt` | 从 git `9635676c^` 找回的通用 super-agent（英文、含 `<citations>`、**无版本红线**），对照锚点 |
  | **V1 当前** | `v1_current_costized.txt` | 当前线上造价化精简版（含 `<safety_redline>`，skills 走渐进披露），基线 |
  | **V2 饱和** | `v2_runbook_saturated.txt` | V1 + 新增 `<skill_runbook>` 常驻块（死命令模板饱和加载、明示"是脚本不是工具"、取消这俩 skill 的 read_file 依赖）= A1 完全体 |
- **数据**：`ce-services/eval/agent_routing_eval.jsonl`（17 条；分组 `no_version`/`with_version`/`boundary`）。
- **模型**：`qwen3-8b`（评测口径见 `ce-services/eval/README.md`：在 flash/thinking/pro 档跑，避开 ultra 的 task 双脑歧义）。
- **服务依赖**：:8099 LLM / :8100 知识 / :8101 任务（DeerFlowClient in-process，无需 Gateway）。
- **切变体机制**：复用生产同款配置开关——harness 把 `get_app_config().lead_agent.system_prompt_path` 指向当前变体文件 + `DeerFlowClient.reset_agent()` 重建 agent（与正式环境改 `config.yaml` 切版本同一条代码路径，不再 monkeypatch）。三变体占位符集均为当前 `apply_prompt_template` kwargs 子集，可被同一框架 format。

## 3. 运行脚本

服务器执行（见 `run.sh`）：

```bash
git pull
bash ce-services/notebooks/2026-06-26-1710-lead-prompt-ablation/run.sh
```

> harness 自动判定（基于 `DeerFlowClient.stream` 的 tool call 事件）：`ask_clarification`=反问、`bash` 命令含 `qa.py/cost.py`=调脚本、`web_*`=兜底哨兵。no_version 用例跑两轮，路由率取第二轮（拿到版本后是否调脚本）。
> 产出落 `results/`：`metrics.md`（对比表）、`metrics.json`、`raw_traces.jsonl`（逐条）、`run.log`。

## 4. 结果

**指标定义**（判定与分母口径见 `harness.py:compute_metrics`，judge 基于 `DeerFlowClient.stream` 的 tool call 事件自动判，无人工标注）：

| 指标 | 含义 | 分子 / 分母 | 方向 |
|---|---|---|---|
| **路由率** `route_rate` | 该调本地脚本的任务，真的发起了 `bash` 且命令含 `qa.py`/`cost.py` 的比例。本实验主目标。`no_version` 组取**第二轮**（喂版本后是否调脚本，problem 6 命门），其余取第一轮 | `routed` 为真的用例数 / `expect_route==true` 的用例数（A1–A5、A7、B1–B10，共 16 条；A6 boundary 不计入） | ↑ 越高越好 |
| **红线遵守率(主)** `redline_rate` | 未给 spec 版本时是否守住「先反问、不硬算」的版本红线，即第一轮发起 `ask_clarification`。与路由率并列的主验收指标 | 第一轮 `clarified` 为真的用例数 / `group==no_version` 的用例数（A1、A2、A7、B1、B2、B10，共 6 条） | ↑；要求 V2≥V1，预期 V0 塌方 |
| **web兜底率(应0)** `web_fallback_rate` | 任一轮退回 `web_search`/`web_fetch`/`image_search` 的比例。problem 6 那个 bug 的回归哨兵（造价 agent 已摘 web 工具，应恒 0） | 任一轮 `web_fallback` 为真的用例数 / 全部用例数（17 条） | ↓ 目标 = 0 |
| **越界拒答率** `boundary_reject_rate` | 越界用例（库内无对应规范）是否正确**不**调脚本，防为刷路由率把什么都往脚本上塞 | `not routed` 的用例数 / `group==boundary` 的用例数（A6，共 1 条） | ↑ 越高越好 |

> 服务器跑出的真实数字（待补；贴 `results/metrics.md`）。

| 变体 | 路由率 | 红线遵守率(主) | web兜底率(应0) | 越界拒答率 |
|---|---|---|---|---|
| v0_original_superagent | | | | |
| v1_current_costized | | | | |
| v2_runbook_saturated | | | | |

逐条/分桶关键观察（待补）：
-

## 5. 分析与结论

- **对照**（待补）：V2 vs V1 路由率 delta 来自哪（no_version 第二轮是否还退 web？）；V0 红线遵守率是否如预期塌方（佐证常驻红线价值）。
- **结论**（待补）：✅采纳 V2 / 🟡 部分 / ⛔ 负结果 + 一句话。
- **下一步**（待补）：若 V2 达标 → 把 `<skill_runbook>` 落进 `prompt.py` 线上模板；若仍不达标 → 升级 A2（skill 包成真 tool）/ A3（qwen-plus）。
- **离线 trace 评测入口（已就位）**：每次请求的 trace 现按 `variant:<变体名>` 打标（源自 `lead_agent.system_prompt_path` 文件名，内置默认为 `variant:default`），LangSmith/Langfuse 可直接按变体过滤。harness 批量出指标表是当前主路径；将来「批量发请求 + 按 trace 离线评分（含语义判据/LLM-judge）」时，靠这个标签把请求按变体归桶，无需 harness 在进程内判定。

> 跑完同步：在 `../experiments.md` 顶部把 E1 从 🟡 改成结论，并回链本文件夹。
