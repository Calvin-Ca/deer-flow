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

**它是什么**：不是一个功能，而是**夹在弱模型和系统输出之间、把不可靠的 8B 变成生产可用的一组工程手段**。
核心命题：**模型是给定的（不换更强的），可靠性是你在它周围"工程化"出来的属性，不是等模型变好。**

**先看 8B 在本场景的具体翻车方式**（都可复现，正是要各个击破的对象）：

| 翻车方式 | 具体表现 |
|---|---|
| 结构化输出坏 | 要 JSON 给半段 + 一堆废话 / 字段错 / 编字段 |
| 决策不一致 | 同一构件这次选 A 下次选 B（这就是为什么要 pass^k） |
| 越界/破红线 | 编候选里没有的码 / 该确定性算的地方自己口算 / 口径漂移串库 |
| 路由错 | 该派 cost 派了 norm、该反问不反问 |
| 被带偏 | 用户一质疑就改口（sycophancy） |

**对每种翻车叠一层护栏**（本项目里的具体做法 + 杀哪种 + 权衡）：

| # | 手段 | 杀哪种 | 项目里怎么接 | 权衡 |
|---|---|---|---|---|
| 1 | **结构化输出强约束 + 修复回环**：强制 schema 合法，校验失败不崩、把错误喂回重出（有界重试→再不行转 HITL） | 输出坏 | deer-flow 有 structured output 原语，直接接 | 重试加延迟，须设上限别无限 loop |
| 2 | **选码 self-consistency 投票**：高风险决策采样 N 次取多数票，票分散=低置信→转人工 | 不一致 | **叠在现有置信门上**（门现只用检索分，这加"模型自一致"第二信号） | N× 成本/延迟，只对高风险/首过低置信开 |
| 3 | **确定性优先路由 + LLM 兜底**：意图先走规则/分类器，LLM 只兜真歧义 | 路由错 | 项目本有 `routing/prerouter.py` 却被旁路——**"发现并修正架构错配"本身可讲** | 规则有覆盖尾巴，保留 LLM 兜底 |
| 4 | **候选内约束生成 + 校验**：只能从检索候选里选码，选后校验 `码∈候选集`，不在→拒绝/转人工 | 编码 | 红线现靠 prompt 求，这层变成**代码硬约束** | 几乎无（纯增强）|
| 5 | **护栏中间件(pre/post tool)**：工具调用前拦破坏地域/版本隔离的调用；输出后拦没走确定性算价链的总价 | 越界/串库/RAG算数 | deer-flow 有 `GuardrailMiddleware`，把红线从"prompt 祈祷"变"enforced code" | 规则维护成本 |
| 6 | **反思重试 / 自检**：校验失败/低置信时带着错误原因反思后重试 | 兜底 1/4 | 与 1/2/4 的失败分支复用 | 延迟 |

**为什么和评测体系(①)是一对**：每个手段都是**拿延迟/成本换可靠性**，值不值、调多少要用评测数字说话——
> "加了 self-consistency 投票后，选码 pass^5 从 62%→89%，红线违规 8%→0，代价高风险 case 延迟 +1.5×。"

这句同时证明你会**度量**(①)+会**加固**(③)，是面试杀器；也是"若只做两个 = ①+③"的理由。

**已有 vs 要加**：已有 = 置信门(检索分驱动) + 红线 prompt + deer-flow 的 GuardrailMiddleware/structured output 原语 +
一个被旁路的 prerouter；要做 = **有意识地把它们接起来** + 补缺的(自一致投票 / schema-repair 回环 / 重接 prerouter /
输出后护栏)。很多是"接线"而非从零写，但**接的方式和权衡**就是 senior 信号。

**一句话**：把"红线靠 prompt 求"升级成"红线靠代码拦"，把"模型一次说了算"升级成"投票/校验/修复/兜底"，
让 function-calling 不稳的 8B 达到能上生产的稳定度——每一步都有 pass^k 前后对比撑着讲。

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
