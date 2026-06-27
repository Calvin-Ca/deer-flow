# ce-services 实验记录（编排层路由）· 结论速查 timeline

> **本文件 = 精炼结论时间线**（每条实验一段：假设 / 方法 / 数据 / 结论，重留**负结果**），作快速回溯索引。
> **详细过程**（完整配置 / 运行脚本 / 逐条结果 / 分析）落各 **dated 文件夹** `notebooks/<YYYY-MM-DD-HHMM>-<短描述>/`——体系约定见 `notebooks/README.md`，新实验照 `_template/` 起。
> 决策落地见 `TODO.md` 与 `PROBLEM.md`。
>
> 标记：⛔=负结果(已排除)，✅=采纳，🟡=部分/待续。

---

## E1 ✅ lead-agent 提示词消融（problem 6：skill 当工具 + web 兜底）（2026-06-26 → 跑分 2026-06-27）

→ 详细过程：[`notebooks/2026-06-26-1710-lead-prompt-ablation/`](2026-06-26-1710-lead-prompt-ablation/)

**假设**：把两个脚本的死命令模板/参数饱和加载进**常驻 system prompt**（V2），相对当前精简版（V1）能提高路由率（尤其 no_version 拿到版本后的第二轮），且红线遵守率不降、web 兜底率维持 0；原始通用 super-agent（V0，无版本红线）作对照锚点。

**方法**：三变体（V0 git 找回原始 / V1 当前线上 / V2 = V1+`<skill_runbook>` 饱和块）经生产同款配置开关 `lead_agent.system_prompt_path` 切换 + `reset_agent` 逐条跑 `agent_routing_eval.jsonl`（17 条）；harness **跑完从 checkpointer 最终 state 的 `AIMessage.tool_calls` 读完整 args** 自动判路由/反问/兜底（早期从流事件判会因 delta 切碎 args 而 route 恒 0，已修）。

**数据**（2026-06-27 单次，qwen3-8b，commit `aa371e82`）：

| 变体 | 路由率(直跑) | 子agent路由率 | 触达skill率 | 红线遵守率 | web兜底 | 越界拒答 |
|---|---|---|---|---|---|---|
| V0 原始 | 0.0 | 0.44 | 0.44 | 0.17 | 0.0 | 1.0 |
| V1 当前 | 0.25 | 0.50 | 0.75 | 1.0 | 0.0 | 1.0 |
| **V2 饱和** | **0.875** | 0.0 | **0.875** | **1.0** | 0.0 | 1.0 |

**结论**：✅ **采纳 V2**——直跑路由率 0.875（≈V1 的 3.5×）、红线 1.0、web 兜底 0，三项主验收全达标且显著优于 V1；增益主要来自把"当子 agent 工具间接调"纠正为"按脚本直跑"（V2 子 agent 路由 0）。V0 红线塌方 0.17 佐证常驻红线价值。**保留**：单次跑、Qwen3-8B 方差大（V0 红线历次 0.5/0.5/0.17），方向稳但绝对值待 3× 重复复核，之后再把 `<skill_runbook>` 落进线上 `prompt.py`（碰后端，`[backend]`+单测）；若回落则升级 A2（skill 包成 tool）/ A3（qwen-plus）。
