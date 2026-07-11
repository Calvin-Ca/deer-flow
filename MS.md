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

### ② 复核 / 批判 agent（adversarial critic）— 多智能体深度【实施中】

**核心洞察**：不是"再叫个 LLM 问'对不对'"（8B 判 8B 会互相橡皮图章）。强设计是 **generator–verifier 模式**：
大部分校验用**确定性规则**扛，LLM 只判规则判不了的语义，且**对抗性提问（"找茬"而非"确认"）**。

**它查什么（拆两半）**：
- **A. 确定性校验器（规则，无 LLM）— 占大头、是地基**：码真实存在（`ce-db_bill_get` 能取到）；码在候选内
  （∈ `bill_match` 候选，不是编的）；清单↔定额映射合法；**算术独立重算**（复核端从头重算综合单价 = Σ(含量×单价)
  ×(1+费率)，须和组价给的数逐分一致——抓弱模型在"工具结果→最终答案"间的转写/幻觉误差）；单位一致；人材机完整；
  价格在信息价±容差；地域/版本隔离；引用真实。
- **B. LLM 语义复核 — 只干规则干不了的**：构件↔码语义匹配（描述"多孔砖墙"却选了实心砖墙 `010401003`——正是
  cost_task-0004 的坑）；漏项/错套；特征落实。

**怎么接**：generator–verifier，组价出结果后、定稿前插一道。子智能体版（`cost-critic`）= 只拿"结果+检索证据"、
被对抗性提示的独立 agent，语义判断背后压着确定性预检——展示多智能体。查出问题：确定性硬错（算术不符/编码/越界）
→ 拒绝打回或转 HITL；语义存疑 → 降置信转 HITL 并摆出具体异议。**必须有界**（复核→重选 最多 N 轮，不下转人工）。

**让复核自身可靠**：活更窄（只验不生成）；证据接地（从证据引出为什么反对）；返回结构化 `{verdict, findings:[{type,
evidence,severity}]}`；多视角（单位镜/语义镜/漏项镜各查一遍）。**价值用 eval①量**：在已知错样本（`adversarial.jsonl`/
多孔砖陷阱）的**命中率** + 已知对样本的**误拒率** + 端到端 pass^k 提升。
> 面试话术："generator–verifier，复核在已知错样本召回 X%、误拒 Y%，端到端 pass^5 提了 Z 点。"

**实现**：确定性校验器 `backend/app/ce/cost/verify.py`（纯函数，独立重算综合单价 + 完整性/格式/地域校验，
`test_cost_verify.py` 单测）→ 暴露为 `verify_cost` 工具；`cost-critic` 子智能体（config.yaml）调 `verify_cost`
+ `ce-db_bill_get`（码存在）+ `ce-rag_match_bill_item`（候选/语义），对抗性提示、结构化 verdict。

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
- **⑤ Agentic RAG 升级（norm-qa）——先立传统 RAG 基线再对比**

  **方法论（先做基线，别跳）**：引入 agentic RAG 前**必须先评传统 RAG 立基线**，否则"提升"没说服力。
  做法：把两态做成**可配置开关**、用**同一评测集**（`benchmark/agent_eval/norm_faithful`，含 expect_refuse
  测误拒）分别跑，Langfuse 按 variant 横向比 → 才有"忠实度 X→Y、误拒 Z、召回 A→B"的可信数字。**标准 ablation。**

  **两个正交部件**（各一个开关，才能单变量归因）：
  - **查询分解**（提召回）：复合规范问题先拆子问题、逐个检索再综合（agentic = LLM 规划检索，非固定检索一次）。
    造价规范问题天然复合（计量+清单口径+取费），单句检索捞不准。norm-qa 本能多次检索，升级基本是 prompt。
  - **引用忠实性校验**（提诚实，招牌）：生成后**回查答案引的每个条文号是否真在检索证据里**，不在则拒答/剥引用/
    降级"无库内依据"——把红线"不编条文"从 prompt 祈祷变**确定性 enforced check**，治 RAG 头号幻觉。两层：
    ① 存在性回查（确定性、可单测，`verify_norm`，与 cost-critic 的 verify.py 同套路）；② 论断落地（RAGAS 式，
    走 `judges/norm_faithfulness.md` LLM 裁判，重、需先在小批人标上校准）。招牌是①（便宜且强）。

  **可配置切换（传统 ↔ agentic，deer-flow 现成机制）**：
  - 忠实校验 → `CE_NORM_FAITHFULNESS_CHECK` env flag（config 支持 `$ENV` + 热重载，确定性 gate，on/off 干净）。
  - 查询分解 → **prompt variant**（deer-flow 已有 `system_prompt_path` 变体 + `resolve_active_prompt_variant`
    给 trace 打 `variant:` 标签，本就是为 A/B 设计；agentic 提示词要求分解、传统单轮，切两份）。
  - 评测 runner 参数化 `--mode traditional|agentic`，同一 `norm_faithful` 数据集跑两轮 → Langfuse dataset run
    横向比。**顺序**：先跑传统基线 → 加开关 → 切两态对比，让归因诚实（是分解带来的还是忠实校验带来的）。

  **deer-flow 好做吗**：分解=prompt；存在性回查=纯函数可单测；toggle=env flag + prompt variant（现成）；
  度量=norm_faithful 现成。RAGAS 论断落地是重的那半（要裁判校准）。

  **[✓ 已落地 agentic 半]**（`test_norm_faithfulness.py` 10 例单测通过）：
  - `backend/app/ce/norm/faithfulness.py`：`check_faithfulness`（抠答案条款号→回查是否在检索证据里，
    verdict=faithful/unfaithful/no_citation + faithful_rate；条款号正则只吞带小数点的、不误吞年份/标准号）
    + `verify_norm` 工具（原名 `norm_verify`，2026-07-11 统一 verify 前置命名）+ `faithfulness_enabled()` 读 `CE_NORM_FAITHFULNESS_CHECK` 开关。
  - norm-qa 提示词升级为 agentic：**①复合问题先分解逐个检索 ②定稿前调 `verify_norm` 回查引用**、
    unfaithful→剥引用/降级"无库内依据"；`verify_norm` 工具已加进 norm-qa。
  - **[✓ 已实现] baseline 对比 runner**：`run_norm_faithful_experiment.py --mode traditional|agentic`，同一
    `norm_faithful` 集跑两轮，`--mode` 控 `CE_NORM_FAITHFULNESS_CHECK` + variant 标签；判定器
    `benchmark/scoring/norm_faithful_score.py`（纯函数、10 例单测）出**忠实率/幻觉引用率/答案要点覆盖/std 级
    上下文召回 + 误拒率/漏拒率**——runner 从 trace 拿{答案, ce-rag 检索证据}直接 `check_faithfulness` 可靠度量、
    不依赖弱模型自觉。**先跑 traditional 立基线再开 agentic，比忠实率↑/幻觉率↓**。
  > 面试话术："先立传统 RAG 基线，再加可配置的分解+引用回查同集横向比——忠实度 X→Y、误拒 Z、召回 A→B。"
- **⑥ 主动学习闭环（few-shot 版已实现）**：HITL 人工纠正回流成 few-shot（不训练）。

  **核心洞察**：置信门本就是 active-learning 采样器（把低置信选码路由给人）。闭环 = 把人工在闸上给的
  正确码 log 下来，下次遇相似构件时**检索最相似的历史纠正当 few-shot 示例**注入选码——同样的错不再犯，
  **不重训、不要 GPU**。四段全在 deer-flow 现成件上接线：采集（HITL 已产 override）→ 存储 → 检索（ce-rag
  思路）→ 注入（选码 prompt）→ 度量（benchmark eval①）。
  > 两种沉淀形态：**few-shot / 检索增强示例**（轻，deer-flow 好做，本次实现）；**微调选码器**（重，训练是
  > 框架外离线 ML，deer-flow 只让"换上练好的模型"trivial）。面试选前者：是系统/agent 工程、能真跑能 demo。
  > **依赖关系**：没有 eval① 不能安全做闭环——任何回灌须过 benchmark 门禁防退化，故 ① 是 ⑥ 的前置。

  **[✓ 已实现] few-shot 完整闭环**（纯函数 + 现成件接线，`test_cost_exemplars.py` 13 例单测通过）：
  - `backend/app/ce/cost/exemplars.py`：`CorrectionStore`(append-only JSONL) + `similarity`(字符二元组
    Jaccard，中文友好无依赖) + `retrieve_exemplars`(**按 spec 版本隔离** top-k，相似度过下限防噪) +
    `format_fewshot` + `record_bill_correction`(采集) + `cost_recall_exemplars` 工具；
  - **采集端**：`workflow.py` 的 select_bill 闸 resume 处接 `record_bill_correction`（try/except 兜底，
    采集失败不拖垮组价）；
  - **注入端**：`cost_recall_exemplars` 工具注册 + 加进 cost-agent 工具，提示词要求**选码前先检索历史纠正示例**
    （仅参考、仍只在当前候选内选，示例码不在候选不采用）；生产可把检索换成 ce-rag/embedding，相似度接口不变。
  > 风险（面试会问）：纠正都是难例→few-shot 池偏边缘要平衡；人工 override 不一定对→**接 ②复核 agent，
  > 复核过的纠正才高权**（`Correction.verified` 已留字段、检索 verified 优先）；检索到不相关反而伤→用 eval 量。

## Tier 3 — 锦上添花
真正的 planner agent（当前是启发式拆）；项目级 memory；延迟优化（并行工具调用 / 缓存）。

## 面试上别花时间的
再接 IM channel / 加一堆 skill / UI 打磨（广度≠深度）；再写一个确定性端点（重复劳动无新信号）。

## 若只做两个
**① 评测体系 + ②复核 agent**，或 **① + ③可靠性层**。有了度量话语权 + 可靠性话语权，手里每个设计决策
（为什么 workflow 不用自由 agent、为什么选码置信门、为什么知识层拆 rag/db）都能用数字+权衡讲出来。
