# Agent有哪些架构？做架构选型时要考虑哪些因素？
Agent 架构选型最容易踩的坑,是把"有哪些架构"理解成一个扁平清单(ReAct、multi-agent、RAG……),然后纠结挑哪一个。
但这些东西其实不在同一个维度上——ReAct 是反馈循环的形态,multi-agent 是拓扑的形态,二者可以叠加。
所以正确的心智是:架构 = 几个正交维度的组合,你不是在选一个,而是在每个维度上各定一个值。

下面先给维度框架(这是面试真正想听的),再给拓扑可视化,最后给选择因素和你的 EPC 映射。
一、四个正交维度(决定架构形态的轴)
这四个维度正交,任意组合都是一种合法架构:
Autonomy(自主度):从 Workflow(预定义路径、LLM 只填空)到 Agent(LLM 动态决定下一步)。这是 Anthropic Building Effective Agents 的核心区分——大多数生产系统其实是 workflow,不是全自主 agent。
Topology(拓扑):单 agent / 链式 / supervisor / 分层 / 网状。决定"几个 LLM 角色、谁连谁、信息怎么流"。
Feedback loop(反馈循环):有没有 observe-act 循环(ReAct)、有没有 reflection / self-critique(evaluator-optimizer)。决定 agent 能不能根据执行结果纠偏。
Task decomposition timing(任务分解时机):plan-ahead(先规划全局再执行,可控、可审计)vs interleaved(边做边分解,ReAct 式,灵活但难预测)。
把一个系统在这四轴上各取一个值,就完整描述了它的架构。比如你的 ReAct 子节点 = 高 autonomy + 单 agent topology + 有 feedback loop + interleaved 分解。

二、Topology 维度的几种典型形态
拓扑是四个维度里最"空间化"的,画出来比文字清楚——谁是协调者、信息往哪流:
  ::view-transition-group(*),
  ::view-transition-old(*),
  ::view-transition-new(*) {
    animation-duration: 0.25s;
    animation-timing-function: cubic-bezier(0.19, 1, 0.22, 1);
  }
![img.png](img.png)
VvisualizeVvisualize show_widget几点说明:链式确定、可审计但不灵活;supervisor(= orchestrator-workers)是多 agent 生产系统最常用的形态,协调权集中、好调试;网状(swarm)peer 之间自由通信,灵活但状态共享和可观测性极差,生产环境慎用;分层(图里没画)= supervisor 的 supervisor,适合任务可分解成多层子任务的大系统。

三、常见架构模式 = 维度的具体组合
业界叫得出名字的"架构",本质都是上面维度的固定组合:
Workflow 类(低 autonomy,推荐做主干):Prompt chaining(链式)、Routing(分类后分流)、Parallelization(并行+聚合)、Orchestrator-workers(动态分派但路径受控)、Evaluator-optimizer(生成-评估循环)。
Agent 类(高 autonomy):ReAct(单 agent + reason-act-observe 循环 + interleaved 分解)、Plan-and-execute(先规划后执行 = plan-ahead)、Multi-agent(supervisor / hierarchical / network)。

四、如何根据业务场景选——核心因素
因素倾向确定性 Workflow倾向自主 Agent任务可预测性路径固定、步骤已知开放探索、路径不可预知错误容忍度 / 合规高风险、需审计、要 human-in-the-loop可容错、结果可回滚任务复杂度步骤少、无需动态分解需运行时分解、子任务数量不定延迟 & 成本敏感(workflow 步数可控)可接受 loop 放大的开销可观测 / 可调试要求高(拓扑越简单越好查)团队有能力处理 multi-agent 调试上下文压力单上下文够用子任务需隔离 context → 多 agent / 分层
贯穿一切的第一原则(Anthropic 的核心建议):用能 work 的最简单架构,只在收益明确时才加复杂度。多 agent / 网状不是更高级,而是更贵、更难调、更不可控——能用 routing + 单 agent 解决就别上 supervisor。
五、落到你的 EPC 系统
你的 Orchestrator-Pipeline-ReAct 三层,正是"按节点在四个维度上分别取值"的范本,而不是单选了某一种架构:

Orchestrator 层:topology = supervisor / routing,autonomy 中等。负责把请求分给组价或规范问答,集中协调、好审计——符合政企造价场景对可控可追溯的硬要求。
Pipeline 层:autonomy 低、plan-ahead、链式。造价流程(清单解析→量价匹配→取费)步骤相对固定,用确定性 workflow 做主干最稳,合规和可调试都占优。
ReAct 层:只在真正需要工具循环和动态推理的子任务(如组价的歧义边缘 case)才上高 autonomy + interleaved。配合你的 confidence-threshold,高置信项走确定性短路,LLM 循环只留给少数难项——这正是"最简架构优先"的体现。

面试时如果被追问"为什么不做成全 multi-agent",最有力的回答就是:造价业务的可审计性和确定性需求,使得 workflow 主干 + 局部 ReAct 比全自主 multi-agent 更合适——把这句话和上面四维框架绑在一起讲,会比罗列架构名词有说服力得多。

# Workflow(预定义路径、LLM 只填空) 对比 Agent(LLM 动态决定下一步)
这句话的本质只有一个区分维度:谁掌握控制流(control flow)——也就是"下一步做什么"是由谁决定的。其他差异都是这一点的衍生。
Workflow 端:开发者把流程图写死在代码里。LLM 只在固定节点上被调用,负责产出一个值——分类一个类别、抽取字段、生成一段文本,这就是"填空"。但"填完空之后走哪条边",是代码里的 if-else / 固定 edge 决定的,LLM 不掌握控制权。
Agent 端:开发者只给 LLM 一组工具和一个目标,不预设路径。LLM 每一步自己决定:调哪个工具、填什么参数、拿到结果要不要再来一轮、什么时候算完成。控制流是 LLM 在运行时生成出来的,而不是开发者提前写好的。
而且这不是二选一,是一条连续谱:越往右越灵活,能处理预料外的情况;但也越不可预测、loop 成本越高、越难调试和审计。中间的 Routing 是个很好的折中点:LLM 决定走哪个分支(让渡一部分控制权),但分支集合是你预定义的(控制权没完全放手)。
![img_1.png](img_1.png)
用你自己的组价场景做对比,这个区别会立刻具体起来——同一个任务,两种做法:
- Workflow 版组价(控制流在代码里):
解析清单项 → 规则/embedding 匹配定额
   → if 置信度高: 直接取费输出
   → else: 调 LLM 兜底节点产出候选定额 → 输出
LLM 只出现在"兜底节点",负责产出候选定额——这是填空。要不要调它、调完走哪条边,全是代码写死的。这恰好就是你的 confidence-threshold 设计:默认把控制权握在代码手里。
- Agent 版组价(控制流由 LLM 生成):
工具集 = [查定额库, 查 SJG 规范, 算工程量, 取费计算]
指令 = "完成这条清单项的组价"
LLM 自己决定:先查定额库 → 发现描述歧义 → 主动去查规范澄清 → 再算量 → 取费 → 收敛输出。每一步选择都是它当场判断的,你没有预设这条路径。
两边做的是同一件事,差别只在那条路径是你写死的、还是 LLM 即时决定的。这就是从 Workflow 到 Agent 的全部含义。
这也直接解释了你上一轮那个三层架构的取舍——造价业务要可审计,所以主干用 workflow 把控制权握在代码里,只在歧义项上把控制权让渡给 ReAct。你不是在"用不用 agent"之间二选一,而是在这条谱上为每个节点选了一个合适的位置。

# 如何根据Agent项目选择相合适的开源模型？要考虑哪些因素？要考虑模型的哪些指标？
因素是你关心的维度,指标是你拿什么去量它。三者的关系是 流程(怎么选)→ 因素(看什么)→ 指标(怎么量)。
一、选型流程(回答"如何选")
开源 Agent 选型不是"挑一个最强模型",而是一套可操作的收敛流程:
拆节点:把系统拆成角色(Orchestrator / 各 sub-agent / triage),逐节点定义能力需求 + SLA(准确率、延迟、成本上限)。Agent 选型的前提是承认异构选型——不同节点用不同模型。
硬约束先过滤:License、私有化/合规、硬件预算这些"过不了就出局"的条件,在比性能之前就把候选集砍小。
强模型立上界:先用阵营里最强的开源模型跑通整条链路,确立质量天花板。
逐节点向下降级:用你自己的 eval set 验证哪些节点降级后质量不掉——这是省 GPU 成本的主战场。
叠工程优化:量化、prompt caching、confidence-threshold 短路、非 LLM 路径。
量化后 + 你的 eval 终判:leaderboard 只圈候选,不定胜负。

二、因素 → 指标 映射(回答"看哪些因素 / 哪些指标")
因素维度具体指标怎么量(benchmark / 测法)License 合规商用许可、权重开放度、蒸馏/再训练限制直接读协议;优先 Apache 2.0,避开带 MAU 上限的自定义协议Tool calling工具选择/参数填充准确率、schema 合法率BFCL、τ-bench;并确认推理引擎有对应 tool parser长上下文有效性effective context(非标称窗口)、中段衰减RULER、大海捞针,而非看 nominal window指令遵循/结构化输出JSON 合法率、字段约束遵循度IFEval;用你 Pipeline 节点间真实传参测推理深度多步规划、ReAct 反思质量只在需要的节点测;注意 reasoning model 的 thinking token 成本幻觉率/grounding忠实度、引用准确性用你的规范问答样本测复述忠实度(对 规范问答 agent 最关键)部署门槛总参数 vs 激活参数、VRAM、KV cacheMoE 看 active params;按 参数×精度 + KV cache 估显存量化友好度量化后关键能力损失FP8/INT4、AWQ/GPTQ;重点测量化后 tool calling 掉不掉Serving 经济性tokens/s/GPU、并发承载vLLM/SGLang 实测吞吐,而非看 token 单价可演进性微调友好度、社区动能是否支持 LoRA/QLoRA;release 节奏、HF 衍生版丰富度中文+领域中文规范文本理解/复述国产模型优先;用你的规范文本自测
这张表的核心逻辑:通用分数(MMLU/GSM8K)对 Agent 几乎没参考价值,真正决定成败的是右侧那几个 Agent 专用指标,而它们大多在模型 README 里不会被强调。
三、开源相对闭源,额外多出来的三件事
如果面试官追问"开源和闭源选型有什么不同",这三点是区分度所在——它们本质都是自托管把推理服务的运维负担接了过来才浮现的:

License 成了第一过滤器(闭源不用关心,开源法务真会看)
真实成本从 token 单价变成 GPU TCO(VRAM + tokens/s/GPU + 并发)
可演进性成了红利(能微调、能改,这是选开源的核心理由,要主动算进价值)

四、落到你的 EPC 系统
三条硬约束(政企合规 + 中文 + Apache 2.0)基本把候选收敛到 Qwen / DeepSeek / GLM 阵营。
Orchestrator 用大 MoE(强推理,省钱不划算);组价 agent 用中端 dense/小 MoE + 强 tool calling,配 confidence-threshold 把高置信项挡在 LLM 外;规范问答 agent 选低幻觉 + 长上下文忠实的;triage 用小模型甚至 embedding。
你已生成的两个 agent 测试数据,正好做成 eval harness,让第 4、6 步从"看榜"变成"看你自己的数"。

# lead_agent 该派 subagent 的四个场景(核心判据)
派不派,就看子任务是否同时满足"上下文重 + 能自洽 + 不需中途问用户 + 值得隔离/并行":
1. 上下文噪音大,会撑爆/带偏父对话
过程产生大量中间输出(读几十文件、翻日志、反复试错),父只要结论。子在隔离 context 里折腾,只回摘要。
2. 可并行的同类独立任务
多个互不依赖的子任务,同时派(≤3)。例:一张清单几十个构件批量取数,各自独立。
3. 任务自洽、能一句话说清、不需中途澄清
因为子被禁了 ask_clarification,发出去就得能独立干完。
4. 探索/广撒网类
"全库找用了 X 的地方""调研某主题"——读得多、结论短。
不该派(反过来记)
- 需要中途问用户(子不能澄清)
- 任务短/便宜(起后台线程+轮询不划算)
- 要紧密来回
- 要给用户展示文件(子被禁 present_files)
- 强依赖父的完整对话历史

# 简述提示词工程及项目中的调优方法

# skill、tool、mcp、acp、subagent,解释它们的概念、联系和区别
最好从agent的发展来讲

# 哪些工具适合用 tool_calling，哪些适合封装在skill中用bash，谈谈你的看法
deer-flow 用 tool_calling 真正暴露的工具（全是原语）：bash / ls / glob / grep / read_file / write_file / str_replace（文件&shell 原语）+ ask_clarification / present_file / view_image / task(subagent) / setup_agent / update_agent（harness 控制原语）+ MCP 工具 + tool_search（延迟加载）。
skills/public 里 23 个 skill：全部是「SKILL.md 说明书 + 脚本/HTTP 调用」，靠 bash 一条命令拉起。

我的核心判据

决定一个能力放 tool_calling 还是封进 skill，我看 6 个轴：

┌────────────────────────────┬────────────────────────┬────────────────────────────────────────┐
│             轴             │   倾向 tool_calling    │            倾向 skill+bash             │
├────────────────────────────┼────────────────────────┼────────────────────────────────────────┤
│ 通用性/频率                │ 几乎每个任务都用的原语 │ 特定领域、偶发                         │
├────────────────────────────┼────────────────────────┼────────────────────────────────────────┤
│ 参数复杂度                 │ 少、扁平、schema 稳定  │ 多参数 / 需读参考文档才能填对          │
├────────────────────────────┼────────────────────────┼────────────────────────────────────────┤
│ 同类变体数量               │ 单一动作               │ 一引擎 N 变体（如 26 种图表）          │
├────────────────────────────┼────────────────────────┼────────────────────────────────────────┤
│ 是否含「怎么用」的流程知识 │ 单步原子动作           │ 多步 playbook（inspect→query→summary） │
├────────────────────────────┼────────────────────────┼────────────────────────────────────────┤
│ 状态/编排                  │ 无状态                 │ 自带状态、跨调用缓存、内部编排         │
├────────────────────────────┼────────────────────────┼────────────────────────────────────────┤
│ 上下文预算                 │ 常驻也不贵             │ 渐进披露、按需读 references 才划算     │
└────────────────────────────┴────────────────────────┴────────────────────────────────────────┘

对照 skills/public 的归类

这些就该是 tool_calling（现状正确）：bash、文件读写、grep/glob——原子、通用、schema 稳定、天天用；ask_clarification / present_file / task 是 harness 控制流原语，也对。

这些天然该是 skill（封 bash），理由各不同：
- chart-visualization：26 种图表各自一套 schema。若每种都做成 tool_call，工具列表直接爆炸、每轮都烧 token。收成 1 个 skill + references 按需读，是教科书级的正确选择。
- data-analysis：多步 + 有状态（DuckDB 缓存跨调用复用）。脚本自己 own 编排和 .cache/ 目录，比让模型连发 N 个工具调用稳。
- ppt / podcast / video / image-generation：重型外部流水线 + 大量参数，封脚本合适。
- deep-research / github-deep-research / systematic-literature-review / consulting-analysis / newsletter / academic-paper-review / code-documentation：本质是长流程方法论，知识写进 prose 比塞进 JSON schema 自然。
- frontend-design / web-design-guidelines：几乎是纯知识，没有真正的"调用"，skill 就是一份会被触发加载的指南。
- norm-qa / cost-agent（你们自己的）：包了对 :8100/:8101 的 HTTP 调用——这是最值得讨论的边界情况，下面单说。

关键 insight：skill 没有逃离 function-calling，只是把它"降级"了

skill 最终还是靠 bash 这个 tool_call 拉起的。所以它的真正价值不是"绕开工具机制"，而是把一次"复杂嵌套 JSON args 的 tool_call"换成一次"bash + 命令字符串的 tool_call"。

这对你们尤其重要：你 memory 里记着 Qwen3-8B function-calling 不可靠。回忆上轮拆的 _parse_input 那道 pydantic 闸门——模型生成一条 shell 字符串，比生成一个深层嵌套、会被 schema 严格校验的 args 对象，成功率高得多。所以在弱模型上，「能力越复杂 → 越该封进 skill」不只是工程整洁，是直接提调用成功率。

但 skill 的代价：丢了那道闸门

反过来，skill 走 bash 也付出了 tool_calling 的两个好处：
1. 没有 args_schema 校验——错误以 bash 失败的形式暴露，模型可能拼出畸形命令；
2. 没有结构化 guardrail——安全面变成了 shell。

所以凡是需要强校验/强护栏的能力（破坏性操作、写生产、严格枚举参数），我更倾向做成带 args_schema + 你自己 guardrail 的真工具，而不是 skill。

给你们 ce-* 的具体建议

norm-qa / cost-agent 现在是 skill，我认为这个选择对，但理由要清楚：它对，是因为它们除了"调一下 :8100"还携带大量领域流程知识（选哪个 spec 版本、怎么组织带引用的回答、9 位编码怎么在候选内选），这些是 prose 才装得下的；而且弱模型下少绑工具更稳。

但底层那次纯检索（:8100 的召回）其实参数扁平、schema 稳定、未来审图/FM/算量都要复用——这正好命中 tool_calling 的甜区。我的建议是分层：

- 把 :8100 检索做成一个薄的、schema 清晰的 MCP 工具（带校验、跨 agent 复用），这是 BIM 那条"横切共享底座"思路在工具层的延续；
- norm-qa / cost-agent 这层 skill 保留方法论与编排，内部可以调那个 MCP 工具，也可以继续 curl——但领域知识留在 skill。

一句话收尾我的看法：原子、通用、需要校验/护栏的 → tool_calling；多变体、多步、富流程知识、重型外部流水线的 → skill。 在 Qwen-8B 这种弱 function-calling 模型上，这条线还要再往 skill 那边挪一点，因为"一条 bash 字符串"比"一个合 schema 的嵌套 args"好生成得多。

# 在项目中如何对用户的意图进行有效的识别，考虑哪些因素？
规则优先、LLM兜底，原因：
1. 意图空间极小、判别词高度领域化。不是通用闲聊分类（那种才必须上模型），是窄域术语判别，正则/关键词命中率会很高。真正要路由的就 norm vs cost 两条道，而且两边的触发词在造价领域几乎不重叠：
  - norm 侧：计量规则、工程量计算规则、项目特征、按什么计算、综合单价包含哪些费用、条文、规范怎么规定……
  - cost 侧：组价、套定额、套什么清单码、9 位编码、工料机含量、信息价……
2. 确定性 + 可测试，正好踩中你们的工程约定。后端是强制 TDD 的。规则路由可以写 test_intent_router.py，喂一批 query 断言路由结果——而 LLM 路由你没法稳定测（同样输入可能飘）。你们 prompt 里那一堆"死命令模板/红线"本身就是"能确定就别交给模型"的哲学，路由用规则是一致的。
3. 不给弱模型增加新的失败面。你自己记忆里就记着 Qwen3-8B function-calling 不可靠。让它多做一步"先分类"，等于又押注它的弱项。规则路由是把这个决策从模型手里拿走，反而更稳。
4. 零延迟、零 token 成本、可解释。分错了能直接看是哪条规则命中的，改一行就行；模型分错你只能调 prompt 再赌。

编码会在哪卡住（所以不能纯规则）
1. 裸描述 / 无动词的输入。用户直接贴 "C30 现浇矩形柱"，没有任何疑问词——他是想查计量规则还是想组价？规则只能靠"默认值"猜（合理默认：裸构件描述→cost）。这能兜住大部分，但总有歧义残留。
2. "信息是否充分"这一步规则做不了。判断"构件描述够不够细以致要不要 ask_clarification"是语义判断，关键词命中不了。不过——这一步本来就不该规则化，交给 ask_clarification 的现有流程即可，和意图路由解耦。