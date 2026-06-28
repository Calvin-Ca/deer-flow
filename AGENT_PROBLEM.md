# 造价 Agent 工程化落地 · 问题复盘

> 在 deer-flow(LangGraph super-agent 框架)上落地"智能问答(norm-qa)/智能组价(cost-agent)"两个造价 agent 过程中遇到的真实问题与解决思路。底座模型为本地 **Qwen3-8B**(function-calling/多轮交互能力弱),这一约束贯穿大多数问题。
>
> 记录格式:**现象 → 排查 → 根因 → 候选方案 → 解决 → 收获**。
> 其中「**候选方案**」= 同一根因往往有多种解法,先枚举可行方案并做对比/权衡(能用实验验证的标注实测),再在「解决」里给出最终选定与落地——避免"拿到第一个方案就上"。

---

## 1. Agent 形态选型:subagent 结构上无法交互式反问

**现象**:造价场景有一条安全攸关红线——用户没说国标版本(2013/2024)时**必须先反问**,因为同一 9 位编码在两版含义不同,版本错=串库=给出错误编码与价格。这条红线依赖一次**交互式澄清**。

**排查**:deer-flow 有两条"专项 agent"路径——lead 层自定义 agent(`agent_name` 路由)与 subagent(`task` 工具委派)。逐一读中间件链发现:`ask_clarification` 的"中断并等用户回答"完全依赖 `ClarificationMiddleware`,而该中间件**只在 lead agent 链**装配;subagent 走 `build_subagent_runtime_middlewares`,**没有它**——subagent 在后台 `astream` 里自主跑完,即使配了 `ask_clarification` 也是空操作。且 subagent 仅 ultra 模式可达、lead→subagent 是两跳。

**根因**:框架机制决定了"凡核心流程需要先反问的 agent,必须跑在 lead 链上"。把造价 agent 做成 subagent 会**结构性废掉命根子红线**。

**候选方案**:

| 方案 | 反问可用 | 全 mode | git 冲突 | 能否两独立 agent | 碰核心 |
|---|---|---|---|---|---|
| **0 skill-only(默认 lead 继承 skill)** | ✅ | ✅ | 无 | ❌(合成) | 否 |
| A 独立 lead 层 agent | ✅ | ✅ | 需物化(.deer-flow 被 gitignore) | ✅ | 否(叠加) |
| B 替换默认 agent | ✅ | ✅ | 无 | ❌(1个合成) | **是(分叉)** |
| C 纯 subagent 调度 | ❌ **做不到** | ❌ 仅 ultra | 无 | ✅ | 否 |
| D 混合(lead 反问+subagent 执行) | ✅ | ❌ 仅 ultra | 无 | ✅ | 否 |

对比:C/D 被"subagent 结构上不能反问 + 仅 ultra 可达"直接判死;B 只能产出 1 个合成 agent 且要改核心 prompt=分叉;A 单跳、全 mode、行为一致,但撞 `.deer-flow` gitignore 需"git 源 + 部署物化"两段式。

**解决**:选 **方案 0 skill-only 作基线**(零新增架构、零冲突、反问可用、全 mode 可用),是否升级 A/D 由 **评测数据(路由率/红线遵守率)** 决定,而非预先排期。

**收获**:框架的中间件/执行模型决定了方案的能力边界;遵循"最小复杂度优先",用 evaluation 驱动升级而非过度设计。

> 📎 完整论证(deer-flow 机制事实、候选方案逐一详析)、各方案实施步骤与任务清单、待决项,见**附录 A**——本问题给结论,附录给可执行落地依据。

---

## 2. 弱模型上的系统提示词过载:区分"费 token"与"噪声"

**现象**:默认 lead 的通用 super-agent system prompt 约 **9.5K 字符**,塞满与造价无关的内容(web 引用规范、deep-research 委派范例、文件产物约定等)。担心拖累 Qwen3-8B。

**排查/分析**:把问题拆成两个不同的轴——
- **费 token**:基本不成立。该 prompt 是**静态**的,框架刻意把日期/记忆等动态内容注入到首条 HumanMessage,以保持 system prompt 全静态、命中 **prefix-cache**,边际计算/计费成本被吸收。
- **噪声/遵循率**:这才是真问题,**缓存帮不上**。长且含**冲突指令**的 prompt 会稀释弱模型的指令遵循(lost-in-the-middle;例如通用 `<citations>` 要求"带 URL 引用"与 norm-qa 自身"条文号引用"模型相互打架)。

**根因**:弱模型的指令遵循预算有限,**竞争/冲突指令越多、关键信息位置越靠后,越易被稀释**;通用 prompt 既"过剩(噪声)"又"欠缺(没有造价红线)"。

**候选方案**:① **不动**(靠 prefix-cache 吸收成本);② **裁剪/重写模板**(治噪声,但改核心 prompt = 方案 B 式分叉,担心 upstream merge 痛);③ **换 qwen-plus 基座**(治容量,成本/时延上升);④ **方案 A 的 `tool_groups`**(只收窄工具面,删不掉 prompt 正文)。对比:① 只解决"费 token"、解决不了"遵循率";④ 治工具噪声但治不了 prompt 文本;③ 最直接但要动基座;② 治本,顾虑是分叉——后经核实本仓库 `git pull` 默认只拉自己的 fork(`origin`)、**不会自动同步上游 `bytedance/deer-flow`**,分叉顾虑消除,② 变得可接受。

**解决**:采用 **②**——把默认 lead 重写为面向造价的精简版:通用 super-agent → 造价助手,删除整块 `<citations>`,合并冗长澄清范例,中文化,**新增常驻安全红线**。非 ultra 档渲染 **9534 → 1368 字符(约 -86%)**。

**收获**:对弱模型,**噪声/冲突指令/关键信息的位置**比 token 成本更影响遵循率;优化要对准"遵循率"而非"省钱"。

---

## 3. 安全红线在"渐进披露"下强制力不足

**现象**:版本红线最初写在 `SKILL.md` 里。但 deer-flow 的 skill 是**渐进披露**——system prompt 平时只有 skill 的名字+简介,完整 SKILL.md(含红线)要等模型**主动 `read_file`** 后才进上下文。

**根因**:弱模型要连续做对"识别该用此 skill → 决定去读 SKILL.md → 遵守反问红线 → 带参调用"每一步,任一步掉链子红线即失效。安全攸关约束不能依赖"模型主动加载"。

**候选方案**:① **SKILL.md 内强化红线措辞**(仍受渐进披露——要模型先读才生效);② **补 `allowed-tools` 兜底**(收敛工具面,但不解决"红线不在上下文");③ **提升为 system prompt 常驻块**(第 1 轮就在上下文,方案 0 下零架构成本);④ **方案 A 的 SOUL.md 常驻**(强制力最高,但需先做独立 agent)。对比:①② 治标;④ 需升级架构;③ 在当前方案 0 即可让红线常驻。

**解决**:采用 **③**——把版本红线从 SKILL.md 提升为 system prompt 常驻 `<safety_redline>` 块,并用评测的"红线遵守率"作为是否升级独立 agent 的主判据。(评测中已验证:不带版本时模型确实先 `ask_clarification` 反问 → 该方案生效。)

**收获**:安全攸关的强约束必须**常驻**,不能放在按需加载的层级;可观测指标(红线遵守率)要能直接量化该约束是否被执行。

---

## 4. 前端部署排查:"启动成功 ≠ 功能可用"的分层排除

**现象**:前端 `next dev` 启动成功、页面能打开,但**登录/注册不了**,F12 里看到的请求**全是 200**。

**排查(层层排除假象)**:
1. 怀疑"缺 nginx 反代,`/api` 没转发到 Gateway"→ 实测 `next.config.js` **自带 rewrites**,`curl localhost:2026/api/models` 与 `curl localhost:8001/...` **都返回 401、完全一致** → `/api` 转发其实是通的,**nginx 非必需**。排除。
2. 怀疑 Gateway 没起 → `curl /health` 返回 healthy。排除。
3. 怀疑 `.env` 里 `NEXT_PUBLIC_*` 关掉了 rewrite → `grep` 发现两行都被注释。排除。
4. 怀疑 CSRF/源校验拦截登录 POST → 看 `next dev` 终端日志,发现**根本没有 POST,只有反复的 `GET /login?`** —— 这是 HTML 表单退化成原生 GET 提交的特征,说明**前端 JS 没 hydrate、按钮处理函数没挂上**。

**根因**:Next.js **16.2** 默认**拦截非 localhost 源对 `/_next` dev 资源的访问**(`allowedDevOrigins`)。我用**内网 IP `172.19.3.136` 直连**(因 VSCode 端口转发失败),导致客户端 JS chunk 被挡 → React 不 hydrate → 页面能渲染但完全不可交互。是**依赖升级(Next 16.1.7→16.2.6 收紧跨源拦截)× 访问方式(localhost→LAN IP)** 两个变化叠加触发的回归。

**候选方案**:
- *运行方式*(排查中实测对比):`make dev`(整起,与 debugpy 的 Gateway 撞)/ `next dev` 直占 2026(无 nginx,曾误判 /api 404,实测 rewrite 已转发)/ 前端 3000 + 手起 nginx / 换端口避开 Grafana / 停 Grafana 容器(实测后采用,因 3000 被另一项目 Grafana 永久占用)。
- *hydration 修复*:① `next.config.js` 加 `allowedDevOrigins:['172.19.3.136']`(放行 LAN IP);② 改走 `localhost` 访问(修 VSCode 转发,免改配置)。

**解决**:采用 **①** `allowedDevOrigins` 放行内网 IP(② localhost 作备选)。附带:停掉占用 3000 的 Grafana 容器;`/setup` 一直 loading 实因**管理员早已创建**(`.deer-flow/admin_initial_credentials.txt` 存在),应走 `/login` 用初始凭据。

**收获**:"进程启动成功/页面能打开"不等于"应用可用";**分层排除**(先用 `curl` 把连通性/后端/配置逐层证伪,再看前端);**依赖升级 × 操作变更**叠加是回归的高发区,排查要同时盯这两个变量。

---

## 5. Skill 默认全启用 → prompt 被 22 个无关技能污染

**现象**:精简 prompt 后 dump 出来,`<skill_system>` 里仍列着 **22 个 skill**(podcast/ppt/video/vercel-deploy…),不止造价的两个。

**根因**:读 `is_skill_enabled` 发现——`public`/`custom` 类 skill **未在 `extensions_config.json` 显式禁用时默认启用**;只配 norm-qa/cost-agent 为 enabled 是冗余的,其余 20 个靠默认值全注入了 prompt。对弱模型是路由噪声。

**候选方案**:① 在 `extensions_config.json` 里**逐个显式 `enabled:false`**(方案 0,instance 级一刀切);② **方案 A 的 skills 白名单**(per-agent,工具/技能仍全局保留,只对造价 agent 不开放);③ 不动。对比:② 最干净、且别的 agent 还能用其余 skill,但需先做独立 agent;① 在方案 0 下一步到位、改配置即可。

**解决**:采用 **①**——显式 `enabled:false` 禁用 21 个无关 public skill,只留 norm-qa/cost-agent。skill 列表 22 → 2。(per-agent 级保留待升级方案 A 时用 ②。)

**收获**:接入第三方框架时,**默认值的语义要逐个确认**;"没配置"往往不等于"关闭"。

---

## 6. 评测暴露的路由失败:弱模型把 skill 当"工具"+ 危险兜底

**现象**(一轮真实评测对话):用户问"C30现浇混凝土矩形柱怎么组价?"(未给版本)——
- 第一轮 ✅:模型**主动 `ask_clarification` 反问 2013/2024**(常驻红线生效);
- 第二轮 ❌:拿到"2024版"后,模型思考"cost-agent **工具不可用**…我将用 **web_search** 查找组价信息"——**没调起 skill,反而退回联网搜索**。

**根因**:
- **A. 弱模型不走渐进披露**:把 `cost-agent` 当成"一个名为 cost-agent 的工具",在工具表里找不到就判"不可用",而不是按指引去 `read_file SKILL.md → bash 跑 cost.py`。深层原因是 8B 模型**多步 agentic 规划弱 + "一任务一工具、直接调"的工具先验 + 不愿去取"看不到的"间接信息**——progressive disclosure 这套抽象是为强模型(Claude)设计的,不向下泛化到 8B。
- **B. web_search 是有害逃生口**:模型卡壳时有联网工具兜底 → 会从网上扒/编造组价,直接违背"不造码、不杜撰、只采信技能返回"红线。**工具面收窄不只是降噪,更是安全问题。**

**候选方案**:
- 针对 A(**提示词优化路线**,把能力从"文档面"抬到"resident prompt 面"):**A1 prompt 常驻"死命令模板"**(把 `cost.py`/`qa.py` 的 bash 命令直接写进 prompt + 明示"不是工具是脚本",消除 read_file 间接 + 纠正工具先验,轻量);**A1′ 完全体「饱和加载」**(更进一步:把两技能的红线+死命令+参数整段抬进常驻 `<skill_runbook>` 块、取消这俩 skill 的渐进披露依赖——渐进披露只在 skill 多时省 token,问题 5 砍到只剩 2 个后已无收益);**A2 把 skill 包成真正的 tool**(在 config.yaml 注册 `cost_compose`/`norm_qa` 工具内部打 :8101,迎合弱模型"找工具就调"的先验,最契合根因,但要写工具封装,且必须兜住"工具诱导填参绕过版本红线"的副作用);**A3 换 qwen-plus**(容量解法,兜底)。
- 针对 B:**B 摘掉 web 工具组**(`web_search/web_fetch/image_search`),堵死逃生口。摘除方式又有候选:删除 vs **注释停用**(`ToolConfig` 无 `enabled` 开关、默认 lead 拿全部工具,故方案 0 下用注释作可逆开关;per-agent 级开关需走方案 A 的 `tool_groups`)。

对比:B 治"走偏"(安全)但治不了"不会调";A1 最轻、对症"间接+先验";A2 最根治但成本高;A3 抬容量但要动基座。

**解决**:**B 已落**(web 工具组**注释停用**,保留配置可一行恢复);**A1/A1′ 进行中**;A2/A3 作为评测仍不达标时的升级路径。

> **实验数据存放位置**:A1/A1′ 提示词优化的 A/B 选型用专门的消融实验验证,存放于 **`ce-services/notebooks/2026-06-26-1710-lead-prompt-ablation/`**——含 `prompts/` 三变体原文(V0 = git `9635676c^` 找回的原始通用 super-agent / V1 = 当前线上造价化精简版 / V2 = 饱和加载 `<skill_runbook>` 完全体)、自动评测 `harness.py`(monkeypatch `SYSTEM_PROMPT_TEMPLATE` + 解析 stream tool call 判路由/反问/兜底)、`run.sh` 与 `results/` 跑分。指标口径(路由率/红线遵守率/web兜底率/越界拒答率)对接 `ce-services/eval/agent_routing_eval.jsonl`;结论时间线见 `ce-services/notebooks/experiments.md` E1。

**收获**:给弱模型的能力调用要**写死、前置**,不能依赖它自行发现多跳工作流;**移除会诱导走偏的工具**与"提供正确工具"同样重要;摘工具优先用**可逆开关**(注释/分组),别直接删。

---

## 7. 评测体系:端到端不分层 → 归因混淆,无法有效优化

**现象**:整个调试过程反复出现"答非所问/结果不对",但**分不清是哪一层的锅**——agent 没调脚本(编排层)?召回到错构件(知识层)?还是服务没起(基础设施)?虽有评测集(`ce-services/eval/agent_routing_eval.jsonl`、`ce-code/data/eval_set/match_gold*.jsonl`),但都是**端到端 / 人工判读**,改一版 prompt 也不知道是涨是跌。

**根因**:① 端到端评测把"编排层失败"和"知识层失败"混在一起,**无法归因**;② 无自动化 harness;③ 无锁定的 **baseline**,优化没有参照系。

**候选方案**:① **维持端到端评测**(简单,但无法归因、容易"修好一个弄坏另一个");② **分层隔离评测**——把链路拆成可独立归因的关键环节,每环单独喂输入、单独量指标。判读方式上又分**人工** vs **自动化 harness**。对比:① 成本低但定位不了问题;② 需建 harness,但能精确归因、可回归。

**解决**:采用 **②**,拆成 **7 个关键环节**:

```
用户query →[编排层 Qwen3-8B]→ bash调脚本 →[服务:8101]→[知识层:8100]→ 结果 →[编排层转达]→ 输出
```

| 环节 | 测什么 | 如何独立测 | 指标 | 失败归属/优化对象 |
|---|---|---|---|---|
| **S1 路由** | 造价/规范意图→是否决定调对应 skill(非瞎答/web_search) | 喂 query,只看是否发起 bash 调脚本 | 路由率 | 编排层:prompt/工具面 |
| **S2 澄清** | 缺版本/缺描述→是否先 `ask_clarification` 反问 | 喂"不带版本"query | 红线遵守率(主) | 编排层:常驻红线 |
| **S3 调用** | bash 调脚本、`--spec/--standard/--description` 参数映射是否正确 | 喂"参数已齐"query,查命令与参数 | 调用/参数正确率 | 编排层:死命令模板 |
| **S4 转达** | 拿到脚本 JSON 后是否忠实透传(need_review/缺价/不算钱),不编造 | 喂固定脚本输出看转达 | 转达忠实率 | 编排层:prompt |
| **K1 召回** | `bill_match` 是否把金标码召回进 top-k | **绕开 agent** 直接打 :8100,用 `match_gold` | recall@k | 知识层:混检/表体注入 |
| **K2 选码** | 候选内 LLM 选 9 位码是否正确 | 直接打 :8101,喂金标描述 | Top-1 准确率 | 知识/任务层:选码 prompt/置信度 |
| **K3 取数** | 选中码→定额工料机+信息价是否正确/完整 | 直接打 price_compose | 命中率/缺口率 | 知识层:定额/信息价数据 |

(norm-qa 对应:K1/K2 换成 **R1 条文检索召回 / R2 带引用生成**,指标为忠实引用率、零召回拒答率。)

**关键 = 隔离归因**:测每一环时**把上游输入钉成正确的**——测 K1–K3 直接打 HTTP、根本不经过 agent(确定性、可重复、秒级);测 S1/S2 只看 agent 行为、不管下游对错。落地优先级:① 先做**知识层 harness**(确定性高、复用现成金标、当天出分)→ ② 再做**编排层 harness**(基于 `DeerFlowClient`)→ ③ 锁 **baseline**,之后每次改动出**逐环 delta**。

**收获**:**分层隔离评测是有效优化的前提**。后续优化动作(精简 prompt、摘 web 工具、治召回缺口)分属不同环,不分层就无法验证某次改动的真实效果与副作用——容易"改了 A 修好一个、悄悄弄坏 B"而不自知。

---

## 8. 意图识别与路由:弱模型上"隐式自判"还是"规则前置"

**现象**:lead_agent 入口要判别用户意图(规范条文问答 / 算量组价 / …)并路由到对应脚本(`qa.py` / `cost.py`),但 deer-flow **原生无专门意图识别模块**。当前这套是重写后的单 agent(ReAct)harness,意图识别是**隐式**的:LLM 在 tool-calling 循环里靠 `SYSTEM_PROMPT_TEMPLATE` + 工具/技能描述自己判、自己选 `bash qa.py` 还是 `bash cost.py`。这在 Qwen3-8B 上不稳(承接问题 6 暴露的路由失败)。

**排查**:在源码里搜 `intent/classif/router` 的命中**全是同名不同事**(bash 命令安全分级、异常分级、FastAPI 路由),没有一处是用户意图识别。确认真正要路由的就 **norm(规范条文问答)vs cost(算量组价)** 两条道;两侧触发词在造价领域几乎不重叠,判别属**窄域术语判别**。

**根因**:框架是"prompt 驱动单 agent"范式,意图识别天然隐式、押注 LLM 自由决策——而这恰是 Qwen3-8B 的弱项(function-calling/多步规划不稳)。多让它"先分类"一步等于加注其弱项。
- **关键非对称性**:路由错的代价 **≪** 版本(2013/2024)错的代价。路由分错只是调错脚本、返回空/不相关,模型可重试;版本串库是问题 1/3 标"最高红线"的事(给错编码/条文/价格)。结论:**意图路由可大胆用规则;版本闸门必须保守、继续走 `ask_clarification` 反问,绝不规则猜默认。两件事必须解耦。**

意图类别定为 **5 类**(非二分):`norm`→qa.py、`cost`→cost.py、`clarify`→ask_clarification(信息不足,语义判断规则做不了)、`both`→串行(先 norm 后 cost)、`chat`→直接回答(能力外/元对话)。

**候选方案**:

| 方案 | 机制 | 优点 | 缺点 |
|---|---|---|---|
| **A prompt 内显式两段式路由**(零代码) | `<skill_runbook>` 前插 `<routing>` 决策块,强制"先分类后执行",把隐式决策摆上台面 | 改动最小、与 deer-flow 范式一致、可配独立模板 + Langfuse variant 做 A/B | 仍押注 8B 判别力、弱模型可能分错、不可稳定单测 |
| **B 独立 router LLM** | `before_model` 中间件对首条消息分类一次,输出受约束小 JSON(`{intent,spec_version,missing}`),分类可单用 qwen-plus | 路由可靠性显著高于自由 tool-call、可单测、可据分类注入精简 prompt | 多一次 LLM 调用 + 一道中间件,是框架没有的增量改造 |
| **C 编码优先分层 + 兜底**(推荐主路线) | 纯代码规则(正则/关键词 + 默认值)覆盖高置信大头,只有"无把握/裸歧义"才落到 LLM/澄清兜底 | 零延迟/零 token、可解释、可 TDD(`test_intent_router.py`)、与"能确定就别交给模型"哲学一致、不给弱模型加失败面 | 纯规则覆盖不了裸描述歧义与"信息充分性"判断(故必须留兜底)、关键词表需维护 |

对比:A 治标且押注弱模型;B 最可靠但增量改造重;C 把 LLM 从"每条都判"降级成"只判模糊尾巴",最契合项目"死命令优先"哲学,但需保留兜底、不能纯规则。

**解决**:**方案 A 已落地(2026-06-28)** 作为即时基线——在 `prompt.py` 的 `SYSTEM_PROMPT_TEMPLATE` 的 `<safety_redline>` 与 `<skill_runbook>` 之间新增 `<routing priority="高">` 块(5 类主意图,先分类后执行);**版本澄清下沉**:`<safety_redline>` 不再写"必须先问版本",改为指针,norm/cost 两条能力块各自新增「版本闸门」承载 `ask_clarification`(standard/spec 必填、无默认、绝不猜);`<workflow>` 改为"先分类后澄清"。回归测试 `backend/tests/test_lead_agent_prompt.py`(`test_default_template_embeds_routing_block`、`test_version_clarification_is_pushed_down_to_per_capability_gate`,全文件 25 passed)。
> 实现坑:routing 块内 `{{ norm | cost | both | clarify | chat }}` 必须双花括号转义,否则 `.format()` 抛 `KeyError`。

**当前倾向**:以 **方案 C(编码优先 + LLM/澄清兜底)** 为最终主路线,而非让弱模型每条都判。落地 C 前先用 `ce-services/eval/` + `notebooks/` 评测集跑真实/构造 query,**量化意图歧义率**——若 85%+ 能高置信命中,编码路由明确划算;歧义率高则规则价值打折(该步是 ce-services 侧纯分析脚本,不碰 backend)。版本闸门始终独立、保守,走 `ask_clarification`。B 作为 A 在 8B 上不够稳时的升级路径。

**收获**:**意图路由与版本闸门必须解耦**——前者代价低、可大胆用规则前置;后者安全攸关、必须保守反问。给弱模型做路由,优先"确定性规则覆盖大头 + LLM 只兜模糊尾巴",而非让它每条自判;升级与否用评测集量化的"意图歧义率"判定,而非预先排期。

---

## 贯穿性认知

1. **底座模型能力是第一约束**:Qwen3-8B 的 function-calling/多轮/渐进披露能力弱,几乎每个问题的解法都落在"把关键信息前置、常驻、写死,把会走偏的路径堵死"。
2. **安全攸关 > 能力丰富**:版本红线、不造码/不杜撰是造价场景的命根子,宁可收窄工具面、强制反问,也不追求"乐于助人"。
3. **一题多解,先对比再定**:每个根因都先枚举候选方案、权衡(能实验的用数据),再选落地方案;改动尽量**可逆**(注释开关 > 删除)。
4. **evaluation 驱动 + 分层归因**:方案是否升级、prompt 改动是否有效,都用可量化指标判定;并按"编排层/知识层/基础设施"分层定位,而非端到端拍脑袋。
5. **分层排除**:部署类问题先证伪连通性/后端/配置,再看前端;依赖升级与操作变更叠加是回归高发区。

---

# 附录 A:Agent 集成方案讨论与落地计划

> 本附录并入原 `AGENT_INTEGRATION_DISCUSSION.md`(方案讨论与机制发现)与 `AGENT_INTEGRATION_DEV.md`(落地实施计划),作为**问题 1「Agent 形态选型」**的完整论证与可执行步骤:问题 1 给结论,本附录给事实基础、候选全解、实施步骤与任务清单。
> 关联代码:`backend/packages/harness/deerflow/agents/lead_agent/agent.py`(lead 工厂)、`.../subagents/executor.py`(subagent 执行)、`.../agents/middlewares/clarification_middleware.py`(澄清中断)、`.../config/agents_config.py`(lead 自定义 agent schema)、`.../config/subagents_config.py`(subagent schema)、`config.yaml`(subagents 注册)、`extensions_config.json`(skill 启用)。
> 关联文档:`ce-services/PRD.md`(任务层 /norm/qa /cost/compose)、`ce-code/PRD.md`(知识底座 :8100)、`cost_agent_prd.md`(CostAgent 产品)。

## A.1 目标与设计原则

在 deer-flow 上提供两个面向用户的造价 agent:

- **智能问答(norm-qa)**:造价规范条文检索 + 带引用的结构化回答。
- **智能组价(cost-agent)**:构件描述 → 选清单码 → 取定额工料机含量 + 信息价单价(组价取数)。

两者**核心安全红线**都依赖一次**交互式反问**:用户没说国标版本(2013/2024)时**必须先反问**——版本错=串库=给出错误编码与价格。这条红线是后续方案取舍的关键约束。

**设计原则:最小复杂度优先。** agent 是比 skill 更重的抽象(独立入口、人格、路由、上下文隔离、per-agent 记忆);只有 skill 满足不了时才升级 agent。故 **skill-only(方案 0)为基线**,A/B/C/D 均为"基线不达标时的升级路径",是否升级**由 evaluation 数据决定**。skill 是 agent 的底层能力单元——做成 agent 时仍复用同一份 `SKILL.md` + 脚本(agent 的 `skills:` 字段指向它),故"先 skill-only、按需升级"**不是返工**,升级只叠加一层常驻 SOUL.md 框架与独立入口,skill 资产平滑保留。

## A.2 现状(讨论起点)

骨架已存在,本次非从零构建:

| 组成 | 智能问答 | 智能组价 | 状态 |
|---|---|---|---|
| Skill 定义 | `skills/public/norm-qa/SKILL.md` | `skills/public/cost-agent/SKILL.md` | ✅ |
| 沙箱薄客户端脚本 | `norm-qa/qa.py` | `cost-agent/cost.py` | ✅(纯 urllib 零依赖) |
| Skill 启用 | `extensions_config.json` `norm-qa: enabled` | `cost-agent: enabled` | ✅ |
| 子 agent 注册 | `config.yaml` `subagents.custom_agents.norm-qa` | `...cost-agent` | ✅(含 system_prompt/tools/skills) |
| 后端服务 | ce-services :8101 `/norm/qa` → ce-code :8100 `/search` | :8101 `/cost/compose` → :8100 `bill_match`/`price_compose` | ✅ |

**当前形态 = subagent**:两者注册在 `subagents.custom_agents`,只能被 lead agent 经 `task` 工具在 **ultra 模式**委派,不是用户能直接选的独立入口。

**产品决策(已确认)**:暴露方式倾向"**独立专属入口**"(用户能直接选到这两个 agent);模型**保持本地 Qwen3-8B**(function-calling/调 skill 不稳,是落地最大风险点)。

## A.3 deer-flow 关键机制(方案取舍的事实基础)

### A.3.1 两条"专项 agent"路径互相独立

| | **lead 层自定义 agent** | **subagent** |
|---|---|---|
| 入口 | `agent_name` 路由(前端选择器) | `task` 工具委派 |
| 配置来源 | `.deer-flow/users/{uid}/agents/{name}/` 或 legacy 共享 `.deer-flow/agents/{name}/` | `config.yaml` `subagents.custom_agents` |
| 构建代码 | `agent.py::_make_lead_agent` | `executor.py::SubagentExecutor._create_agent` |
| 中间件链 | `_build_middlewares`(完整,**含 ClarificationMiddleware**) | `build_subagent_runtime_middlewares`(精简,**不含 ClarificationMiddleware**) |
| `agent_config` | 非 None | None(但有自己的 `SubagentConfig`) |
| 筛 skills / tools | `available_skills`(`agent_config.skills`)+ `groups=agent_config.tool_groups` | `_load_skills`(`config.skills`)+ `_filter_tools`(`config.tools`/`disallowed_tools`) |

> 两条路径**完全不交叉**。"agent_config 非 None"只对 lead 层自定义 agent 成立。

### A.3.2 决定性发现:subagent 无法 `ask_clarification` 反问用户

- `ask_clarification` 的"中断并把问题抛给用户、等回答"完全依赖 `ClarificationMiddleware` 返回 `Command(goto=END)`。
- 该中间件**只**在 `_build_middlewares`(lead 链)末位被加入;`build_subagent_runtime_middlewares` **没有**它。
- subagent 在后台线程 `astream` 里**自主跑完**,调 `ask_clarification` **不会真的反问用户**(内置 general-purpose subagent 因此直接 `disallowed_tools=[ask_clarification]`、prompt 写"Do NOT ask for clarification")。

⚠️ **凡核心流程需"先反问版本"的 agent,跑成 subagent 时这条红线失效**。当前 config.yaml 里 norm-qa/cost-agent 两 subagent 配了 `ask_clarification` 且 prompt 要求反问——**该配置在 subagent 执行模型下实际不生效**。

### A.3.3 `subagent_enabled` = 仅 ultra

`task` 工具由 `subagent_enabled` 决定(`agent.py`),按 mode 表只有 **ultra** 传 True。flash/thinking/pro 下 lead 没有 task 工具 → subagent 不可达。常开需改 `subagent_enabled` 来源(改前端 mode 映射 vs 改 agent.py 核心)。

### A.3.4 `.deer-flow/` 被 git 忽略

`.gitignore` 忽略整个 `.deer-flow/`,而 lead 层自定义 agent 只从 `.deer-flow/.../agents/` 读 → 与项目"agent 定义走 git 纳管"红线冲突。要做独立 lead 层 agent,必须"**git 跟踪源 + 部署物化**"两段式。

### A.3.5 工具筛选的边界

- `tool_groups` 只筛 **config.yaml `tools:` 段**的工具(web/file/bash);builtin(`present_files`/`ask_clarification`/`view_image`/`task`)**不受 tool_groups 约束**。
- skill 的 `allowed-tools` 是**条件触发白名单**:无任何 skill 声明 → 不过滤;一旦有声明 → 取并集做交集裁剪(只减不加)。norm-qa/cost-agent 的 SKILL.md **当前未声明**。
- lead 层自定义 agent **没有** `disallowed_tools`(那是 subagent 专有);要锁工具可在 SKILL.md 补 `allowed-tools` 兜底。

## A.4 候选方案逐一分析

> 阅读顺序:先看**方案 0(基线)**;A/B/C/D 是"基线不达标时的升级路径"。

**方案 0 —— skill-only(基线,不升级 agent)【先采用】**
不新增任何 agent,靠**默认 lead agent 继承已启用 skill**满足需求:`agent_config=None` → 继承所有已启用 skill;有 `bash` 直接调 `qa.py`/`cost.py`;跑在 lead 链 → ClarificationMiddleware → **反问红线可用**;skill 注入**不依赖 `subagent_enabled` → 全 mode 可用**;零新增架构、零 git 冲突。
- ⚠️ 唯一实质弱项 = **弱模型上红线强制力**:deer-flow 是**渐进披露**——prompt 平时只有 skill 名字+简介,完整 SKILL.md(含红线)要等模型决定去读后才进上下文。弱模型(Qwen3-8B)要连续做对"识别该用此 skill → 读 SKILL.md → 遵守反问/不编造红线 → 带参调脚本",每步都可能掉链子;agent 的 SOUL.md 从第 1 轮常驻,强制力更高。
- ❌ 无独立入口、无法给问答/组价不同常驻红线(共享通用 prompt)。

**方案 A —— 独立 lead 层自定义 agent(agent_name 路由)**
norm-qa/cost-agent 做成两个用户可选独立 agent,各自 `config.yaml` + `SOUL.md`。
- ✅ 各自干净红线;跑 lead 链 → 反问可用;全 mode;单跳(可靠性较好);纯叠加不碰核心。
- ❌ 撞 `.deer-flow/` gitignore → 需"git 源 + 部署物化"两段式;需前端选择器。

**方案 B —— 替换/改造默认 `_make_lead_agent`**
把唯一默认 agent 硬改成造价 agent。
- ✅ 无 git 冲突;全 mode;反问可用。
- ❌ **只能产出 1 个合成 agent**(问答+组价合一,弱模型要在两 skill 间自选、无法给不同红线);改核心 prompt 模板 = **分叉 harness 核心**,upstream merge 痛。缓解:只改"数据"(prompt 模板文件 + config.yaml tools 列表),别改 agent.py 逻辑。

**方案 C —— 纯 subagent + lead 调度(≈ 现状)**
- ❌ **致命**:subagent 无法 `ask_clarification`(A.3.2)→ 版本反问红线失效;仅 ultra 可达;弱模型两跳更不稳。**不可取**。

**方案 D —— 混合:lead 反问 + subagent 执行**
把"反问"和"执行"分到正确的层:lead 链(含 ClarificationMiddleware)负责 `ask_clarification` 收齐 [版本 + 描述],参数齐后 `task()` 下发"参数已完整"子任务;subagent(纯执行器)拿全参数 → bash 调脚本 → 返回。
- ✅ 反问在 lead 链 → 红线可用;subagent 当"参数已齐的脚本执行器",契合其"自主跑完、不交互"定位;上下文隔离。
- ❌ 仍受 **ultra-only** 限制;仍是**两跳**;需在 lead 层写"路由+收参"prompt。

## A.5 方案对比

| 方案 | 反问版本(核心红线) | 默认 mode 可用 | git 冲突 | 弱模型可靠性 | 两独立 agent | 碰 harness 核心 |
|---|---|---|---|---|---|---|
| **0 skill-only(基线)** | ✅(lead 链) | ✅ | 无 | 取决于路由率/红线遵守率(**待测**) | ❌(合成,自路由) | 否 |
| A 独立 lead 层 agent | ✅(lead 链) | ✅ | 需物化 | 较好(单跳) | ✅ | 否(叠加) |
| B 替换默认 agent | ✅(lead 链) | ✅ | 无 | 中(单 agent 选两 skill) | ❌(合成 1 个) | **是(分叉)** |
| C 纯 subagent 调度 | ❌ **做不到** | ❌ 仅 ultra | 无 | 差(两跳) | ✅ | 否 |
| D 混合(lead 反问+subagent 执行) | ✅(lead 链) | ❌ 仅 ultra | 无 | 中(两跳) | ✅ | 否 |

## A.6 优先级与升级判定门(evaluation 驱动)

**最小复杂度优先 + evaluation 驱动升级**:能 skill 解决就不升级;是否升级由评测数据决定,不预先排期。

| 优先级 | 方案 | 何时做 | 一句话 |
|---|---|---|---|
| **P0(现在做)** | 方案 0 skill-only | 立即 | 默认 lead 继承已启用 skill,全 mode 可用,零新增架构 |
| **P1(条件升级)** | 方案 A 独立 lead 层 agent | 方案 0 评测**红线遵守率不达标**时 | 专属 agent + 常驻 SOUL.md 红线,单跳、全 mode、行为一致 |
| **P2(仅特定场景)** | 方案 D 混合 | 确认瓶颈是**需隔离上下文 / 并行跑重取数**(非红线)时 | lead 收参 + subagent 当纯执行器,ultra-only、两跳 |

**升级判定门(P0→P1/P2 唯一依据)**——用一组造价/规范测试问法跑方案 0,量两项指标:

1. **路由率**:造价/规范类问题中,模型真去调 `cost.py`/`qa.py` 的比例。
2. **红线遵守率(主判据,安全攸关)**:**不带版本**的问法中,模型真先 `ask_clarification` 反问版本的比例。

- 两项达标 → **停在方案 0**,不做 A/D。
- 红线遵守率不达标 → **升级方案 A**(常驻 SOUL.md 直接补强红线;方案 D 治不到此病根)。
- 仅当瓶颈是隔离/并行执行(非红线)→ 才考虑方案 D。

> 升级**无返工**:A/D 仍复用同一份 SKILL.md + 脚本,只加一层常驻 SOUL.md 框架。

## A.7 各方案实施步骤与任务计划

### A.7.1 方案 0 —— skill-only(P0)

实施步骤:
1. **确认 skill 资产就绪**:两份 `SKILL.md` + `qa.py`/`cost.py`(纯 urllib 零依赖薄客户端)。
2. **确认启用**:`extensions_config.json` 中 norm-qa / cost-agent 均 `enabled`。
3. **强化 SKILL.md 红线表述**(弱模型关键):顶部用极度指令化语言写明"未指明 2013/2024 版本必须先 `ask_clarification` 反问、不猜默认、不编造编码/条文/价格",并给死 bash 命令模板。
4. **(可选)补 `allowed-tools` 兜底**:SKILL.md 加 `allowed-tools: [bash, ask_clarification]`,对弱模型收敛工具面。
5. **ultra 歧义处理**:方案 0 阶段 ultra 下默认 agent 既能走 skill 又能 `task` 委派现存 subagent,为评测口径干净,**评测在 flash/thinking/pro 下进行**。

任务计划:
- [ ] T0-1 校对两份 SKILL.md 红线段落 + bash 模板(指令化、给死参数格式)
- [ ] T0-2 确认 `extensions_config.json` 两 skill 已启用
- [ ] T0-3 服务器起 :8100 / :8101 常驻服务,bash 手测 `qa.py`/`cost.py` 连通(先用"已给全参数"样例)
- [ ] T0-4 建评测集:造价/规范问法 N 条,含"带版本"与"不带版本"两组,覆盖路由率 + 红线遵守率
- [ ] T0-5 跑评测,出两项指标 → 决定是否升级(达标即收手)

### A.7.2 方案 A —— 独立 lead 层自定义 agent(P1)

`AgentConfig` 字段(`agents_config.py`):`name` / `description` / `model`(None=默认)/ `tool_groups`(筛 config.yaml tools 段)/ `skills`(白名单)。人格与红线写在同目录 `SOUL.md`。

实施步骤:
1. **git 跟踪源**:新建 `ce-agents/cost-agent/{config.yaml,SOUL.md}`、`ce-agents/norm-qa/{config.yaml,SOUL.md}`(随 git,解决 `.deer-flow/` 被 gitignore)。`config.yaml`:`skills: [cost-agent]`(或 norm-qa)、`tool_groups` 收窄、`model` 留空或指 `qwen-plus`;`SOUL.md`:版本红线写成常驻第一性约束。
2. **部署物化**(两段式):服务器 `git pull` 后单行物化到 `base_dir`(落点待确认,见 A.9):`mkdir -p backend/.deer-flow/agents && cp -rf ce-agents/cost-agent ce-agents/norm-qa backend/.deer-flow/agents/`
3. **前端选择器**:确保用户能选到这两个 agent(`agent_name` 随消息进 `configurable`)。
4. **(可选)工具兜底**:SKILL.md / `tool_groups` 双重锁工具面。
5. **回归**:对两 agent 各跑两项指标,红线遵守率应显著高于方案 0。

任务计划:
- [ ] TA-1 设计 `ce-agents/{cost-agent,norm-qa}/config.yaml`(skills/tool_groups/model)
- [ ] TA-2 撰写 `ce-agents/{cost-agent,norm-qa}/SOUL.md`(常驻红线 + 人格 + bash 模板)
- [ ] TA-3 写部署物化脚本/文档(git pull → cp 到 base_dir),确认 base_dir 落点
- [ ] TA-4 前端选择器接入,验证 agent_name 路由命中正确分支
- [ ] TA-5 复跑评测,对比方案 0 的红线遵守率提升

### A.7.3 方案 D —— 混合(P2,仅隔离/并行场景)

> 仅当确认瓶颈是"需上下文隔离 / 并行跑多个重取数子任务"时才做;**红线问题用方案 A 解,不用 D**。受 **ultra-only** 限制。硬约束:subagent **不能反问**(配了 `ask_clarification` 也是空操作)→ 必须做成参数已齐的执行器;**仅 ultra** 可达;lead 发 task + subagent 执行 = **两跳**。

实施步骤:
1. **lead 层收参**:写"路由+收参"prompt——识别造价/规范意图 → `ask_clarification` 收齐版本+描述 → 参数齐后才 `task` 下发(注入方式见 A.9,避免分叉核心)。
2. **subagent 注册**(`config.yaml` `subagents.custom_agents.{name}`,随 git 无物化坑,字段见 A.8):`description`(lead 路由依据)、`system_prompt`(**假定参数已齐**的纯执行指令 + 死 bash 模板,**不要**再反问)、`tools: [bash]`、`disallowed_tools: [task, present_files]`、`skills: [{name}]`、`model: inherit`(或 `qwen-plus`)、`max_turns: 10`、`timeout_seconds: 600`。
3. **解 ultra-only**(若产品要求非 ultra 也能用):改 `subagent_enabled` 来源。
4. **验证**:①"参数已齐"样例 → lead 经 task 委派、subagent 带对参数;② 需澄清样例 → lead 先 `ask_clarification` 收齐再 task。

任务计划:
- [ ] TD-1 确认进入 P2 的前置:评测证明瓶颈是隔离/并行(非红线)
- [ ] TD-2 解决待决项(ultra-only 是否接受 / 如何常开 subagent_enabled)
- [ ] TD-3 设计 lead 层"路由+收参"prompt 注入点(不分叉核心)
- [ ] TD-4 在 config.yaml 注册/调整 norm-qa/cost-agent subagent(纯执行器化,去掉无效 ask_clarification)
- [ ] TD-5 端到端验证两跳链路(已齐参数 + 需澄清两类样例)

## A.8 新增 subagent 开发步骤(ultra 模式)

> 适用于"把某个**参数已齐、自主跑完、不与用户交互**的子任务做成 subagent"。subagent 仅 **ultra** 可达,由 lead 经 `task` 委派。

**三条硬约束**:① subagent 不能反问用户(A.3.2,即使配了 `ask_clarification` 也是空操作)→ 必须做成参数已齐的纯执行器,澄清提到 lead 层先做完;② 仅 ultra 可达;③ 两跳 + 弱模型更不稳。

**开发步骤**:① 先做 skill(`skills/public/{name}/SKILL.md` + 薄客户端脚本,随 git,`extensions_config.json` 置 enabled);② `config.yaml` 注册 `subagents.custom_agents.{name}`,`skills: [{name}]` 指向 skill;③ commit/push → 服务器 `git pull`,**无需物化拷贝**(config.yaml 随 git,不撞 `.deer-flow/` gitignore——这是 subagent 相对方案 A 的开发优势);④ ultra 下验证 task 委派 + 参数。

**`SubagentConfig` 字段(照抄)**:

| 字段 | 作用 | 给弱模型的建议 |
|---|---|---|
| `description` | **lead 路由依据**(何时委派) | 写清触发场景,别写人格 |
| `system_prompt` | 执行指令(**第 1 轮常驻**,无渐进披露) | 极度指令化 + 死 bash 模板 |
| `tools` | 工具白名单(如 `[bash]`) | 只给必需 |
| `disallowed_tools` | 黑名单 | 至少禁 `task` 防递归 |
| `skills` | skill 白名单 | **一个 subagent 只挂一个 skill** |
| `model` | `inherit` 或具体模型 | 不稳就单独指 `qwen-plus` |
| `max_turns` / `timeout_seconds` | 轮数 / 超时(默认 900s) | 取数类 10 轮 / 600s 够用 |

并发上限 3、默认 15 分钟超时(见 `backend/CLAUDE.md` Subagent System)。

## A.9 待决项(升级前需拍板)

1. **ultra-only(影响 D)**:方案 D 仅 ultra 可用是否接受?否则需解决 `subagent_enabled` 常开(改前端 mode 映射 vs 改 agent.py 核心)。
2. **lead 层 prompt 注入方式(影响 D)**:"路由+收参"prompt 怎么进默认 lead 而不分叉核心(skill 渐进披露 vs 改 prompt 模板)。
3. **物化落点(影响 A)**:`base_dir` 取 `backend/.deer-flow` 还是项目根 `.deer-flow`?per-user(`users/{uid}/agents/`)还是 legacy 共享(`agents/`)?需在服务器确认。
4. **工具兜底**:是否给 SKILL.md 补 `allowed-tools: [bash, ask_clarification]`(对弱模型尤其值)。
5. **是否要严格两个独立红线** vs 接受一个合成造价助手(决定是否排除 B)。
6. **模型切换**:本地 Qwen3-8B function-calling/多轮反问不稳是贯穿风险;是否对造价 agent 单独指 `model: qwen-plus`(不动默认助手)。

> **决策记录**:
> - 优先级 **P0 方案 0 → P1 方案 A → P2 方案 D**;升级 evaluation 驱动,达标即收手。
> - 升级首选 **A 而非 D**:红线遵守率是主判据,A 的常驻 SOUL.md 直接补强红线、单跳、全 mode 行为一致;D 的反问仍在通用 lead 层,治不到红线病根。
> - **D 仅用于"需隔离上下文 / 并行跑重取数"场景**,不拿它补红线;受 ultra-only + 两跳限制。
> - subagent **结构上无法**与用户做交互式澄清(ClarificationMiddleware 仅在 lead 链)——这是排除"纯 subagent 调度"(方案 C)的决定性依据。
> - 凡核心流程需"先反问"的 agent,**必须跑在 lead 链上**。
> - 三方案复用同一份 SKILL.md + 脚本,升级无返工;本地 Qwen3-8B 可靠性为独立风险项。
