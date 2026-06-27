# 实验 2026-06-26-1710 · lead-agent 提示词消融（problem 6 编排层路由）

> 状态：✅ 已跑分（2026-06-27，单次，commit `aa371e82`）——结论采纳 V2（直跑路由率 0.875 / 红线 1.0 / web 兜底 0），落库前待 3× 重复复核。承接：`PROBLEM.md` §6（弱模型把 skill 当工具 + 危险兜底）/ §7（分层评测 S1 路由·S2 红线）。

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

**指标定义**（判定与分母口径见 `harness.py:compute_metrics`，judge **跑完后从 checkpointer 最终 state 的 `AIMessage.tool_calls` 读完整 args 自动判**，无人工标注。判据为何不取流事件：`DeerFlowClient.stream` 用 `messages` 模式逐 token 发 delta，tool_call 的 name/args 被切成残缺碎片，且 `values` 模式的完整 message 对已 streamed 的 id 被跳过，故流事件里拼不回完整命令——前两版据此判 route 恒 0，是检测失真而非模型真没调脚本）：

| 指标 | 含义 | 分子 / 分母 | 方向 |
|---|---|---|---|
| **路由率(直跑)** `route_rate` | 该调本地脚本的任务，真的发起 `bash` 且命令含 `qa.py`/`cost.py` 的比例。本实验主目标。`no_version` 组取**第二轮**（喂版本后是否调脚本，problem 6 命门，靠 `since_idx` 把第二轮新增消息切出来单判），其余取第一轮 | `routed` 为真的用例数 / `expect_route==true` 的用例数（A1–A5、A7、B1–B10，共 16 条；A6 boundary 不计入） | ↑ 越高越好 |
| **子agent路由率** `subagent_route_rate` | 走 `norm-qa`/`cost-agent` **子 agent 工具**（而非 bash 直跑脚本）触达 skill 的比例——problem 6「把 skill 当工具表里的工具」反模式。单列不并入 route_rate，用来看变体把"间接调"转成"直跑"的程度 | `routed_subagent` 为真的用例数 / 同上 16 条 | 越低越好（间接路径应被直跑替代） |
| **触达skill率** `reach_skill_rate` | 直跑 ∪ 子 agent，任一方式触达本地 skill 即「未退 web」。与 route_rate 之差 = 还困在子 agent 间接路径的占比 | `routed or routed_subagent` 为真的用例数 / 同上 16 条 | ↑ |
| **红线遵守率(主)** `redline_rate` | 未给 spec 版本时是否守住「先反问、不硬算」的版本红线，即第一轮发起 `ask_clarification`。与路由率并列的主验收指标 | 第一轮 `clarified` 为真的用例数 / `group==no_version` 的用例数（A1、A2、A7、B1、B2、B10，共 6 条） | ↑；要求 V2≥V1，预期 V0 塌方 |
| **web兜底率(应0)** `web_fallback_rate` | 任一轮退回 `web_search`/`web_fetch`/`image_search` 的比例。problem 6 那个 bug 的回归哨兵（造价 agent 已摘 web 工具，应恒 0） | 任一轮 `web_fallback` 为真的用例数 / 全部用例数（17 条） | ↓ 目标 = 0 |
| **越界拒答率** `boundary_reject_rate` | 越界用例（库内无对应规范）是否正确**不**触达脚本/子 agent，防为刷路由率把什么都往脚本上塞 | `not (routed or routed_subagent)` 的用例数 / `group==boundary` 的用例数（A6，共 1 条） | ↑ 越高越好 |

**服务器实跑（2026-06-27，qwen3-8b，单次，commit `aa371e82`）**：

| 变体 | 路由率(直跑) | 子agent路由率 | 触达skill率 | 红线遵守率(主) | web兜底率(应0) | 越界拒答率 |
|---|---|---|---|---|---|---|
| v0_original_superagent (`deerflow_prompt`) | **0.0** | 0.44 | 0.44 | **0.17** | 0.0 | 1.0 |
| v1_current_costized | 0.25 | 0.50 | 0.75 | **1.0** | 0.0 | 1.0 |
| **v2_runbook_saturated** | **0.875** | **0.0** | **0.875** | **1.0** | 0.0 | 1.0 |

逐条/分桶关键观察：
- **V2 直跑路由率 0.875 ≈ V1(0.25) 的 3.5×、V0(0) 从无到有**：`<skill_runbook>` 饱和加载让模型直奔 `bash` 跑脚本。V2 唯二未直跑的 expect_route 用例是 **A5**（2013/2024 抹灰对比）、**B8**（自定义复合保温墙体，库内无对应）——均为 `clarified` 而非退 web，属"宁可多问一句"的过度谨慎，可接受。
- **V2 子 agent 路由率 = 0**：完全不再退走 `norm-qa`/`cost-agent` 那个不透明工具；V1 仍有一半（0.50）靠子 agent 兜（progressive disclosure 的 read_file→bash 与子 agent 混用），V0 则把 skill 全当子 agent 工具（0.44）、自己从不 `bash`。
- **红线：V0 塌到 0.17**（6 条 no_version 仅 B10 反问，1/6），且 V0 反而在不该问的 with_version（B3–B6/B8/B9）乱反问——反问行为与"是否给了版本"几乎脱钩。V1/V2 红线均 1.0。无常驻 `<safety_redline>` → 版本反问基本失效。
- **web 兜底恒 0、越界拒答恒 1.0**：两条哨兵在三变体均稳；造价 agent 摘 web 工具后 problem 6 那个"退回 web"的 bug 未复现。

## 5. 分析与结论

- **对照**：V2 vs V1 路由率 delta（+0.625）几乎全部来自「子 agent 间接路径 → 直跑 bash」的转化——V2 子 agent 0、直跑 0.875，V1 子 agent 0.50、直跑 0.25，二者触达 skill 率（0.875 vs 0.75）差距远小于直跑率差距，说明 runbook 的增益主要是**把已能触达的能力从"当工具间接调"纠正成"按脚本直跑"**，而非凭空多触达。V0 红线如预期塌方（0.17），佐证常驻红线价值。
- **结论**：✅ **采纳 V2**。三项主验收（直跑路由率 0.875 / 红线 1.0 / web 兜底 0）全部达标且显著优于 V1，越界与回归哨兵无退化。
- **保留（重要）**：**单次跑、Qwen3-8B 采样方差大**——同一 V0 红线遵守率在历次跑中为 0.5 / 0.5 / 0.17，数值不稳；结论的**方向**（V2≫V1≫V0 直跑、V2=V1≫V0 红线）在各次均成立，但**绝对值不可单次定论**。落库前应每条用例重复 ≥3 次取均值复核。
- **下一步**：先做 3× 重复跑锁定数值 → 达标后把 `<skill_runbook>` 落进 `backend/.../lead_agent/prompt.py` 线上模板（碰后端，commit 加 `[backend]` + 补单测）；若重复跑后 V2 路由率回落则升级 A2（skill 包成真 tool）/ A3（qwen-plus）。
- **离线 trace 评测入口（已就位）**：每次请求的 trace 现按 `variant:<变体名>` 打标（源自 `lead_agent.system_prompt_path` 文件名，内置默认为 `variant:default`），LangSmith/Langfuse 可直接按变体过滤。harness 批量出指标表是当前主路径；将来「批量发请求 + 按 trace 离线评分（含语义判据/LLM-judge）」时，靠这个标签把请求按变体归桶，无需 harness 在进程内判定。

> 跑完同步：在 `../experiments.md` 顶部把 E1 从 🟡 改成结论，并回链本文件夹。
