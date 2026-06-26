# 造价 Agent 工程化落地 · 问题复盘

> 在 deer-flow(LangGraph super-agent 框架)上落地"智能问答(norm-qa)/智能组价(cost-agent)"两个造价 agent 过程中遇到的真实问题与解决思路。底座模型为本地 **Qwen3-8B**(function-calling/多轮交互能力弱),这一约束贯穿大多数问题。
>
> 记录格式:**现象 → 排查 → 根因 → 解决 → 收获**。

---

## 1. Agent 形态选型:subagent 结构上无法交互式反问

**背景**:造价场景有一条安全攸关红线——用户没说国标版本(2013/2024)时**必须先反问**,因为同一 9 位编码在两版含义不同,版本错=串库=给出错误编码与价格。这条红线依赖一次**交互式澄清**。

**排查**:deer-flow 有两条"专项 agent"路径——lead 层自定义 agent(`agent_name` 路由)与 subagent(`task` 工具委派)。逐一读中间件链发现:`ask_clarification` 的"中断并等用户回答"完全依赖 `ClarificationMiddleware`,而该中间件**只在 lead agent 链**装配;subagent 走 `build_subagent_runtime_middlewares`,**没有它**——subagent 在后台 `astream` 里自主跑完,即使配了 `ask_clarification` 也是空操作。且 subagent 仅 ultra 模式可达、lead→subagent 是两跳。

**根因**:框架机制决定了"凡核心流程需要先反问的 agent,必须跑在 lead 链上"。把造价 agent 做成 subagent 会**结构性废掉命根子红线**。

**解决**:确立 **skill-only 为基线方案**(默认 lead agent 继承已启用 skill,跑在 lead 链 → 反问可用、全 mode 可用、零新增架构),是否升级独立 agent 由 **评测数据(路由率/红线遵守率)** 决定,而非预先排期。

**收获**:框架的中间件/执行模型决定了方案的能力边界;遵循"最小复杂度优先",用 evaluation 驱动升级而非过度设计。

---

## 2. 弱模型上的系统提示词过载:区分"费 token"与"噪声"

**现象**:默认 lead 的通用 super-agent system prompt 约 **9.5K 字符**,塞满与造价无关的内容(web 引用规范、deep-research 委派范例、文件产物约定等)。担心拖累 Qwen3-8B。

**排查/分析**:把问题拆成两个不同的轴——
- **费 token**:基本不成立。该 prompt 是**静态**的,框架刻意把日期/记忆等动态内容注入到首条 HumanMessage,以保持 system prompt 全静态、命中 **prefix-cache**,边际计算/计费成本被吸收。
- **噪声/遵循率**:这才是真问题,**缓存帮不上**。长且含**冲突指令**的 prompt 会稀释弱模型的指令遵循(lost-in-the-middle;例如通用 `<citations>` 要求"带 URL 引用"与 norm-qa 自身"条文号引用"模型相互打架)。

**解决**:把默认 lead 重写为面向造价的精简版——通用 super-agent → 造价助手,删除整块 `<citations>`,合并冗长澄清范例,中文化,**新增常驻安全红线**。非 ultra 档渲染 **9534 → 1368 字符(约 -86%)**。

**收获**:对弱模型,**噪声/冲突指令/关键信息的位置**比 token 成本更影响遵循率;优化要对准"遵循率"而非"省钱"。

---

## 3. 安全红线在"渐进披露"下强制力不足

**现象**:版本红线最初写在 `SKILL.md` 里。但 deer-flow 的 skill 是**渐进披露**——system prompt 平时只有 skill 的名字+简介,完整 SKILL.md(含红线)要等模型**主动 `read_file`** 后才进上下文。

**根因**:弱模型要连续做对"识别该用此 skill → 决定去读 SKILL.md → 遵守反问红线 → 带参调用"每一步,任一步掉链子红线即失效。安全攸关约束不能依赖"模型主动加载"。

**解决**:把版本红线从 SKILL.md **提升为 system prompt 常驻 `<safety_redline>` 块**(第 1 轮就在上下文里),并用评测的"红线遵守率"作为是否升级独立 agent 的主判据。

**收获**:安全攸关的强约束必须**常驻**,不能放在按需加载的层级;可观测指标(红线遵守率)要能直接量化该约束是否被执行。

---

## 4. 前端部署排查:"启动成功 ≠ 功能可用"的分层排除

**现象**:前端 `next dev` 启动成功、页面能打开,但**登录/注册不了**,F12 里看到的请求**全是 200**。

**排查(层层排除假象)**:
1. 怀疑"缺 nginx 反代,`/api` 没转发到 Gateway"→ 实测 `next.config.js` **自带 rewrites**,`curl localhost:2026/api/models` 与 `curl localhost:8001/...` **都返回 401、完全一致** → `/api` 转发其实是通的,**nginx 非必需**。排除。
2. 怀疑 Gateway 没起 → `curl /health` 返回 healthy。排除。
3. 怀疑 `.env` 里 `NEXT_PUBLIC_*` 关掉了 rewrite → `grep` 发现两行都被注释。排除。
4. 怀疑 CSRF/源校验拦截登录 POST → 看 `next dev` 终端日志,发现**根本没有 POST,只有反复的 `GET /login?`** —— 这是 HTML 表单退化成原生 GET 提交的特征,说明**前端 JS 没 hydrate、按钮处理函数没挂上**。
5. **真因**:Next.js **16.2** 默认**拦截非 localhost 源对 `/_next` dev 资源的访问**(`allowedDevOrigins`)。我用**内网 IP `172.19.3.136` 直连**(因 VSCode 端口转发失败),导致客户端 JS chunk 被挡 → React 不 hydrate → 页面能渲染但完全不可交互。

**解决**:`next.config.js` 增加 `allowedDevOrigins: ['172.19.3.136']`。

**叠加副线问题**:
- 端口 **3000 被另一项目的 Grafana 容器(`docker-proxy`,已跑 5 周)永久占用**,导致 `make dev` 在前端步骤因端口占用直接中止;
- `/setup` 一直 loading,实因**管理员早已创建**(`.deer-flow/admin_initial_credentials.txt` 存在),应走 `/login` 用初始凭据,而非 setup。

**收获**:
- "进程启动成功/页面能打开"不等于"应用可用";**分层排除**,先用 `curl` 把"连通性/后端/配置"逐层证伪,再看前端。
- 这次回归是**依赖升级(Next 16.1.7→16.2.6 收紧跨源拦截)× 访问方式(localhost→LAN IP)** 两个变化叠加触发的——排查时要同时盯"环境/依赖变了什么"和"我的操作变了什么"。

---

## 5. Skill 默认全启用 → prompt 被 22 个无关技能污染

**现象**:精简 prompt 后 dump 出来,`<skill_system>` 里仍列着 **22 个 skill**(podcast/ppt/video/vercel-deploy…),不止造价的两个。

**根因**:读 `is_skill_enabled` 发现——`public`/`custom` 类 skill **未在 `extensions_config.json` 显式禁用时默认启用**;只配 norm-qa/cost-agent 为 enabled 是冗余的,其余 20 个靠默认值全注入了 prompt。对弱模型是路由噪声。

**解决**:在 `extensions_config.json` 里**显式 `enabled: false` 禁用 21 个无关 public skill**,只留 norm-qa/cost-agent。skill 列表 22 → 2。

**收获**:接入第三方框架时,**默认值的语义要逐个确认**;"没配置"往往不等于"关闭"。

---

## 6. 评测暴露的路由失败:弱模型把 skill 当"工具"+ 危险兜底

**现象**(一轮真实评测对话):用户问"C30现浇混凝土矩形柱怎么组价?"(未给版本)——
- 第一轮 ✅:模型**主动 `ask_clarification` 反问 2013/2024**(常驻红线生效);
- 第二轮 ❌:拿到"2024版"后,模型思考"cost-agent **工具不可用**…我将用 **web_search** 查找组价信息"——**没调起 skill,反而退回联网搜索**。

**根因**:
- **A. 弱模型不走渐进披露**:把 `cost-agent` 当成"一个名为 cost-agent 的工具",在工具表里找不到就判"不可用",而不是按 `<skill_system>` 指引去 `read_file SKILL.md → bash 跑 cost.py`(真正的调用命令藏在需 read_file 才可见的 SKILL.md 里)。
- **B. web_search 是有害逃生口**:模型卡壳时有联网工具兜底 → 会从网上扒/编造组价,直接违背"不造码、不杜撰、只采信技能返回"的红线。**工具面收窄不只是降噪,更是安全问题。**

**解决方向**:
- 把"如何调用技能"做成 **system prompt 常驻死命令模板**(写死 `python3 .../cost.py --description ... --spec ...`,并明示"这不是工具名,是用 bash 跑脚本"),不依赖模型自己去 read_file;
- **移除 web 工具**(web_search/web_fetch/image_search),堵死"联网编答案"的捷径。

**收获**:给弱模型的能力调用要**写死、前置**,不能依赖它自行发现多跳工作流;**移除会诱导走偏的工具**与"提供正确工具"同样重要。

---

## 7. 评测体系:端到端不分层 → 归因混淆,无法有效优化

**现象/背景**:整个调试过程反复出现"答非所问/结果不对",但**分不清是哪一层的锅**——agent 没调脚本(编排层)?召回到错构件(知识层)?还是服务没起(基础设施)?虽有评测集(`ce-services/eval/agent_routing_eval.jsonl`、`ce-code/data/eval_set/match_gold*.jsonl`),但都是**端到端 / 人工判读**,改一版 prompt 也不知道是涨是跌。

**根因**:① 端到端评测把"编排层失败"和"知识层失败"混在一起,**无法归因**;② 无自动化 harness;③ 无锁定的 **baseline**,优化没有参照系。

**解决(把链路拆成 7 个可独立归因的关键环节)**:

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

**关键 = 隔离归因**:测每一环时**把上游输入钉成正确的**——测 K1–K3 直接打 HTTP、根本不经过 agent(确定性、可重复、秒级);测 S1/S2 只看 agent 行为、不管下游对错。这样某环掉链子不会被上游问题掩盖,涨跌能精确定位到环。

**落地优先级**:① 先做**知识层 harness**(确定性高、复用现成金标、当天出分)→ ② 再做**编排层 harness**(基于 `DeerFlowClient` 跑 agent,自动判定调没调/反没反问/参数对不对/转达忠不忠实)→ ③ 锁 **baseline**,之后每次改动出**逐环 delta**。

**收获**:**分层隔离评测是有效优化的前提**。后续优化动作(精简 prompt、摘 web 工具、治召回缺口)分属不同环,不分层就无法验证某次改动的真实效果与副作用——容易"改了 A 修好一个、悄悄弄坏 B"而不自知。

---

## 贯穿性认知

1. **底座模型能力是第一约束**:Qwen3-8B 的 function-calling/多轮/渐进披露能力弱,几乎每个问题的解法都落在"把关键信息前置、常驻、写死,把会走偏的路径堵死"。
2. **安全攸关 > 能力丰富**:版本红线、不造码/不杜撰是造价场景的命根子,宁可收窄工具面、强制反问,也不追求"乐于助人"。
3. **evaluation 驱动**:方案是否升级、prompt 改动是否有效,都用"路由率/红线遵守率"两个可量化指标判定,而非拍脑袋。
4. **分层排除**:部署类问题先证伪连通性/后端/配置,再看前端;依赖升级与操作变更叠加是回归的高发区。
