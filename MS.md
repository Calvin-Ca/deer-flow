# CE Agent · 面试向能力路线（Milestones）

> 目标不是落地价值，而是**在 agent 开发岗面试里能拿出硬东西**。本场景的三个天然优势要 lean into：
> ① 有可验证硬 ground truth（清单码/定额子目可判对错）→ 能拿真实评测数字；② 有事故级红线 + 弱模型(8B)
> → 可靠性/安全工程有真问题可解；③ LLM 与确定性边界干净 → 能讲清"LLM 该放在哪、不放在哪"。
>
> 面试官真正在意的是"**你怎么度量它、怎么让不可靠的模型可靠、怎么判断边界**"，下面按信号密度排。

## Tier 1 — 优先做

### ① 评测体系（= `benchmark/`，做对的那种）— 单项最值钱【实施中】
`benchmark/` 已相当成熟：`routing_eval`/`retrieval_eval` 有 runner；`agent_eval/{cost_task,toolcall,norm_faithful,
adversarial,trajectory}` 有数据集 + `judges/*.md` 裁判 rubric。**缺口：`cost_task`（端到端组价·τ-bench 式终态 +
pass^k）等四个数据集没 runner。**
- **[✓ 已实现] cost_task runner**：逐条跑 agent × `pass_k` 次，程序化判**终态**（`expected_bill_code` 落没落对、
  `must_cite`/`must_ask`/`must_refuse`/`must_declare_caliber`）+ **红线 policy 独立计分**（违规即 fail、门线 0）；
  报**任务成功率 + pass^k（连跑全过）+ 红线违规率 + evaluable 覆盖率**。判定器 `benchmark/scoring/cost_task_score.py`
  纯函数、`test_cost_task_score.py` 22 例单测通过；runner `benchmark/runner/run_cost_task_experiment.py`。
  **诚实原则**：外部判不了的红线标 not_evaluable、不假装通过（可写进简历/面试的取信细节）。
- **[后续] norm_faithful runner**：RAGAS 忠实度，走 `judges/norm_faithfulness.md` LLM 裁判，**裁判须先在小批
  人标样本上校准一致率**再上量。
- **[后续] 轨迹/对抗 runner**：`trajectory`（多轮闭环）、`adversarial`（红线鲁棒性）。
- **[后续] CI 门禁**：金标回归挂 CI，PR 掉点即红。
> 面试话术：轨迹+终态双层、LLM 裁判与人工一致率、pass^k、回归进 CI —— 一句话和背 prompt 的候选人分开。

### ② 复核 / 批判 agent（adversarial critic）— 多智能体深度
独立复核 agent 对组价结果做对抗检查（定额↔清单是否匹配 / 单价是否在合理区间 / 有无漏项错套），命中打回。
展示 reflection / verifier 模式，比单纯并行 fan-out 深一档。deer-flow subagent + 结构化输出直接支撑。

### ③ 弱模型可靠性层 — 最好的 war story
围绕 8B 加固（任选组合）：结构化输出 schema 校验 + 修复回环；选码 self-consistency 采样投票（叠加现有置信门）；
确定性 prerouter 兜底（项目本有 prerouter 却旁路了——"发现并修正架构错配"本身可讲）。
> 话术："function-calling 不稳的 8B，用 schema-repair + 自一致投票 + 确定性兜底把可用率从 X 拉到 Y。"

## Tier 2 — 挑 1 个做深
- **④ 模型路由/升桶**：默认 8B，低置信/高风险升 32B，报成本/质量权衡曲线（90% 走 8B，准确率持平，成本降 X%）。
- **⑤ Agentic RAG 升级**：查询分解 + **引用忠实性校验**（生成的条文号回查是否真在检索结果里，不在则拒答）。
- **⑥ 主动学习闭环**：HITL 人工纠正回流成 few-shot / 选码器微调数据。

## Tier 3 — 锦上添花
真正的 planner agent（当前是启发式拆）；项目级 memory；延迟优化（并行工具调用 / 缓存）。

## 面试上别花时间的
再接 IM channel / 加一堆 skill / UI 打磨（广度≠深度）；再写一个确定性端点（重复劳动无新信号）。

## 若只做两个
**① 评测体系 + ②复核 agent**，或 **① + ③可靠性层**。有了度量话语权 + 可靠性话语权，手里每个设计决策
（为什么 workflow 不用自由 agent、为什么选码置信门、为什么知识层拆 rag/db）都能用数字+权衡讲出来。
