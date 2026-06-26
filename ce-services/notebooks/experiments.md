# ce-services 实验记录（编排层路由）· 结论速查 timeline

> **本文件 = 精炼结论时间线**（每条实验一段：假设 / 方法 / 数据 / 结论，重留**负结果**），作快速回溯索引。
> **详细过程**（完整配置 / 运行脚本 / 逐条结果 / 分析）落各 **dated 文件夹** `notebooks/<YYYY-MM-DD-HHMM>-<短描述>/`——体系约定见 `notebooks/README.md`，新实验照 `_template/` 起。
> 决策落地见 `TODO.md` 与 `PROBLEM.md`。
>
> 标记：⛔=负结果(已排除)，✅=采纳，🟡=部分/待续。

---

## E1 🟡 lead-agent 提示词消融（problem 6：skill 当工具 + web 兜底）（2026-06-26）

→ 详细过程：[`notebooks/2026-06-26-1710-lead-prompt-ablation/`](2026-06-26-1710-lead-prompt-ablation/)

**假设**：把两个脚本的死命令模板/参数饱和加载进**常驻 system prompt**（V2），相对当前精简版（V1）能提高路由率（尤其 no_version 拿到版本后的第二轮），且红线遵守率不降、web 兜底率维持 0；原始通用 super-agent（V0，无版本红线）作对照锚点。

**方法**：三变体（V0 git 找回原始 / V1 当前线上 / V2 = V1+`<skill_runbook>` 饱和块）monkeypatch `SYSTEM_PROMPT_TEMPLATE` + `reset_agent` 逐条跑 `agent_routing_eval.jsonl`（17 条），harness 解析 stream tool call 自动判路由/反问/兜底。

**数据**：待服务器跑（`results/metrics.md`）。

**结论**：🟡 待续——脚手架就位，等跑分决定是否把 `<skill_runbook>` 落进线上 `prompt.py`，或升级 A2（skill 包成 tool）/ A3（qwen-plus）。
