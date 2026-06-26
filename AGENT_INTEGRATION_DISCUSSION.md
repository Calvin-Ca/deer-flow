# 智能问答 / 智能组价 Agent 集成方案 · 开发讨论记录

> 本文记录"把 **智能问答(norm-qa)** 与 **智能组价(cost-agent)** 以何种形态接入 deer-flow"的方案讨论、关键代码发现与最终倾向。
> 关联文档:`ce-services/PRD.md`(任务层 /norm/qa /cost/compose)、`ce-code/PRD.md`(知识底座 :8100)、`cost_agent_prd.md`(CostAgent 产品)。
> 关联代码:`backend/packages/harness/deerflow/agents/lead_agent/agent.py`(lead agent 工厂)、`backend/packages/harness/deerflow/subagents/executor.py`(subagent 执行)、`.../agents/middlewares/clarification_middleware.py`(澄清中断)。

---

## 1. 目标

在 deer-flow 框架上提供两个面向用户的造价 agent:

- **智能问答(norm-qa)**:造价规范条文检索 + 带引用的结构化回答。
- **智能组价(cost-agent)**:构件描述 → 选清单码 → 取定额工料机含量 + 信息价单价(组价取数)。

两者的**核心安全红线**都依赖一次**交互式反问**:用户没说国标/规范版本(2013/2024)时**必须先反问**——版本错 = 串库 = 给出错误编码与价格。这条红线是后续方案取舍的关键约束。

### 设计原则:最小复杂度优先

**目标是满足用户需求,不是把架构做复杂。** agent 是比 skill 更重的抽象(独立入口、人格、路由、上下文隔离、per-agent 记忆);**只有当 skill 满足不了需求时,才升级到 agent**。因此本文把 **skill-only 作为基线方案(方案 0)**,其余 agent 化方案(A/B/C/D)都是"基线不达标时的升级路径"。是否升级**由 evaluation 数据决定**,而非默认就做 agent(见 §4 方案 0 与 §6 升级触发条件)。

> 补充:skill 是 agent 的**底层能力单元**。做成 agent 时仍复用同一份 `SKILL.md` + 脚本(agent 的 `skills:` 字段指向它)。所以"先 skill-only、日后按需升级 agent"**不是返工**——升级只是给 skill 加一层常驻 SOUL.md 框架与独立入口,skill 资产平滑保留,无沉没成本。

---

## 2. 现状(讨论起点)

骨架其实**已存在**,本次不是从零构建:

| 组成 | 智能问答 | 智能组价 | 状态 |
|---|---|---|---|
| Skill 定义 | `skills/public/norm-qa/SKILL.md` | `skills/public/cost-agent/SKILL.md` | ✅ |
| 沙箱薄客户端脚本 | `norm-qa/qa.py` | `cost-agent/cost.py` | ✅(纯 urllib 零依赖) |
| Skill 启用 | `extensions_config.json` `norm-qa: enabled` | `cost-agent: enabled` | ✅ |
| 子 agent 注册 | `config.yaml` `subagents.custom_agents.norm-qa` | `...cost-agent` | ✅(含 system_prompt/tools/skills) |
| 后端服务 | ce-services :8101 `/norm/qa` → ce-code :8100 `/search` | :8101 `/cost/compose` → :8100 `bill_match`/`price_compose` | ✅ |

**当前形态 = subagent**:两者注册在 `subagents.custom_agents`,只能被 lead agent 经 `task` 工具在 **ultra 模式**委派,不是用户能直接选的独立入口。

**产品决策(已确认)**:
- 暴露方式倾向"**独立专属入口**"——用户能直接选到这两个 agent。
- 模型**保持本地 Qwen3-8B**(注:config.yaml 第 12-13 行已注明本地模型 function-calling/调 skill 不稳,是落地最大风险点)。

---

## 3. deer-flow 关键机制(方案取舍的事实基础)

### 3.1 两条"专项 agent"路径互相独立

| | **lead 层自定义 agent** | **subagent** |
|---|---|---|
| 入口 | `agent_name` 路由(前端选择器) | `task` 工具委派 |
| 配置来源 | `.deer-flow/users/{uid}/agents/{name}/{config.yaml,SOUL.md}` 或 legacy 共享 `.deer-flow/agents/{name}/` | `config.yaml` `subagents.custom_agents` |
| 构建代码 | `agent.py::_make_lead_agent` | `executor.py::SubagentExecutor._create_agent` |
| 中间件链 | `_build_middlewares`(完整,**含 ClarificationMiddleware**) | `build_subagent_runtime_middlewares`(精简,**不含 ClarificationMiddleware**) |
| `agent_config` | 非 None | None(但有自己的 `SubagentConfig`) |
| 筛 skills / tools | `available_skills`(来自 `agent_config.skills`)+ `groups=agent_config.tool_groups` | `_load_skills`(`config.skills`)+ `_filter_tools`(`config.tools`/`disallowed_tools`) |

> 结论:两条路径**完全不交叉**。"agent_config 非 None"只对 lead 层自定义 agent 成立;subagent 也被筛,但走 SubagentExecutor 自己的机制。

### 3.2 决定性发现:**subagent 无法 `ask_clarification` 反问用户**

- `ask_clarification` 的"中断并把问题抛给用户、等回答"完全依赖 `ClarificationMiddleware` 返回 `Command(goto=END)`(`clarification_middleware.py`)。
- 该中间件**只**在 `_build_middlewares`(lead agent 链)末位被加入;`build_subagent_runtime_middlewares` **没有**它。
- subagent 在后台线程 `astream` 里**自主跑完**,调 `ask_clarification` **不会真的反问用户**(内置 general-purpose subagent 因此直接 `disallowed_tools=[ask_clarification]`,prompt 写"Do NOT ask for clarification")。

⚠️ 影响:**凡是核心流程需要"先反问版本"的 agent,跑成 subagent 时这条红线失效**。当前 config.yaml 里 norm-qa/cost-agent 两个 subagent 配了 `ask_clarification` 且 prompt 要求反问——**该配置在 subagent 执行模型下实际不生效**。

### 3.3 `subagent_enabled` = 仅 ultra

`task` 工具由 `subagent_enabled` 决定(`agent.py`),按 mode 表只有 **ultra** 传 True。flash/thinking/pro 下 lead agent **没有 task 工具**,无法调度 subagent → subagent 不可达。常开需改 subagent_enabled 来源(改前端 mode 或改 agent.py 核心)。

### 3.4 `.deer-flow/` 被 git 忽略

`.gitignore:40` 忽略整个 `.deer-flow/`,而 lead 层自定义 agent 只从 `.deer-flow/.../agents/` 读 → 与项目"agent 定义走 git 纳管"红线冲突。要做独立 lead 层 agent,必须"**git 跟踪源 + 部署物化**"两段式。

### 3.5 工具筛选的边界

- `tool_groups` 只筛 **config.yaml `tools:` 段**的工具(web/file/bash);builtin(`present_files`/`ask_clarification`/`view_image`/`task`)**不受 tool_groups 约束**,按各自条件追加。
- skill 的 `allowed-tools` 字段是**条件触发的白名单**:无任何 skill 声明 → 不过滤;一旦有声明 → 取并集做交集裁剪(只减不加)。norm-qa/cost-agent 的 SKILL.md **当前未声明** allowed-tools。
- lead 层自定义 agent **没有** `disallowed_tools`(那是 subagent 专有);要锁工具可在 SKILL.md 补 `allowed-tools` 兜底。

---

## 4. 候选方案逐一分析

> 阅读顺序:先看**方案 0(基线)**;A/B/C/D 是"基线不达标时的升级路径"。

### 方案 0 —— skill-only(基线,不升级 agent)【倾向先采用】

不新增任何 agent,直接靠**默认 lead agent 继承已启用 skill**来满足需求。默认 agent 已具备全部技术前提:

- `agent_config=None` → 继承所有**已启用** skill → prompt 里已能看到 norm-qa + cost-agent。
- 有 `bash` → 直接调 `qa.py`/`cost.py`。
- 跑在 **lead 链** → 有 ClarificationMiddleware → **`ask_clarification` 反问版本红线可用**。
- skill 注入**不依赖 `subagent_enabled`** → **所有 mode 可用**,不卡 ultra。
- **零新增架构、零 git 冲突**(skill 已随 git;enable 在 extensions_config.json)。

- ✅ 最简单、当前已基本就绪;反问红线可用;全 mode 可用;无 git 冲突;不碰核心。
- ⚠️ **唯一实质弱项 = 弱模型上红线的强制力**:deer-flow 是**渐进披露**——prompt 平时只有 skill 的名字+简介,完整 SKILL.md(含红线)要等 agent **决定去读**后才进上下文。于是弱模型(Qwen3-8B)要连续做对:① 从简介识别该用此 skill → ② 读完整 SKILL.md → ③ 遵守反问/不编造红线 → ④ 带参调脚本;每步都可能掉链子,且通用 agent 自身"乐于助人"的 prompt 可能盖过"无版本就反问"。对比之下,agent 的 SOUL.md **从第 1 轮就常驻** system prompt,红线强制力更高。
- ❌ 无独立入口(用户对通用助手提问,模型自路由);无法给问答/组价**不同的常驻红线**(共享通用 prompt)。
- 判断:**作为基线先上并验证**;版本红线是安全攸关(版本错=错价),容错率低,故"够不够用"的判据要盯**红线遵守率**(见 §6)。

### 方案 A —— 独立 lead 层自定义 agent(agent_name 路由)

把 norm-qa/cost-agent 做成两个用户可选的独立 agent,各自 `config.yaml`(name/description/model/tool_groups/skills)+ `SOUL.md`(人格/红线)。

- ✅ 两个独立 agent,各自干净红线;跑在 lead 链 → **反问可用**;所有 mode 可用;单跳(可靠性较好);纯叠加不碰 deer-flow 核心。
- ❌ 撞 `.deer-flow/` gitignore → 需"git 源 + 部署物化"两段式;需前端选择器。
- 解决 git 冲突:定义写进 git 跟踪目录(如 `ce-agents/{cost-agent,norm-qa}/`),服务器 `git pull` 后单行物化:
  `mkdir -p backend/.deer-flow/agents && cp -rf ce-agents/cost-agent ce-agents/norm-qa backend/.deer-flow/agents/`

### 方案 B —— 替换/改造默认 `_make_lead_agent`

把唯一的默认 agent 直接硬改成造价 agent(替换 system prompt + 收窄 skills/tools)。

- ✅ **无 git 冲突**(改的全是 git 跟踪代码/config);所有 mode 可用;不碰 lead 层自定义 agent 机制;反问可用(走 lead 链)。
- ❌ **只能产出 1 个 agent**:问答+组价合成一个,弱模型要在两 skill 间自选(退回可靠性老问题),且无法给两者不同红线;直接改 `_make_lead_agent`/核心 prompt 模板 = **分叉 harness 核心**,upstream merge 有痛。
- 缓解:若一定走此路,只改"数据"(prompt 模板文件 + config.yaml tools 列表),别改 agent.py 逻辑,分叉面最小。

### 方案 C —— 纯 subagent + lead 调度(≈ 现状)

保持两者为 subagent,由 lead agent 经 `task` 调度。

- ✅ config.yaml 一处搞定,**无 git 冲突**;每个 subagent 上下文隔离、单 skill 筛得干净。
- ❌ **致命**:subagent 无法 `ask_clarification`(见 3.2)→ **版本反问红线失效**;仅 ultra 可达(3.3);弱模型**两跳串联**(lead 发 task + subagent 执行)更不稳。
- 判断:**不可取**——废掉命根子红线,且卡 ultra-only。

### 方案 D —— 混合版:lead 反问 + subagent 执行(**倾向采用**)

把"反问"和"执行"分到正确的层:

```
lead 层(含 ClarificationMiddleware)
  └─ 负责 ask_clarification 收齐 [版本 + 构件/问题描述]
         ↓ 参数收齐后,用 task() 下发"参数已完整"的子任务
subagent(纯执行器,无需 clarification)
  └─ 拿完整参数 → bash 调 cost.py / qa.py → 返回结果
```

- ✅ 反问发生在 lead 链 → **红线可用**;subagent 只当"参数已齐的脚本执行器",完美契合 subagent"自主跑完、不与用户交互"的定位;subagent 单 skill 筛得干净、上下文隔离。
- ❌ 仍受 **ultra-only** 限制(除非解决 subagent_enabled);仍是**两跳**(lead 收参 + subagent 执行);需在 lead 层写"路由 + 收参"prompt。
- 适配性:把交互留给能交互的层、把自主执行留给自主层,是对 deer-flow 两类 agent 定位的正确利用。

---

## 5. 方案对比

| 方案 | 反问版本(核心红线) | 默认 mode 可用 | git 冲突 | 弱模型可靠性 | 能否两个独立 agent | 是否碰 harness 核心 |
|---|---|---|---|---|---|---|
| **0 skill-only(基线)** | ✅(lead 链) | ✅ | ✅ 无 | 取决于路由率/红线遵守率(**待测**) | ❌(合成,自路由) | 否 |
| A 独立 lead 层 agent | ✅(lead 链) | ✅ | 需物化 | 较好(单跳) | ✅ | 否(叠加) |
| B 替换默认 agent | ✅(lead 链) | ✅ | ✅ 无 | 中(单 agent 选两 skill) | ❌(合成 1 个) | **是(分叉)** |
| C 纯 subagent 调度 | ❌ **做不到** | ❌ 仅 ultra | ✅ 无 | 差(两跳) | ✅ | 否 |
| D 混合(lead 反问+subagent 执行) | ✅(lead 链) | ❌ 仅 ultra | ✅ 无 | 中(两跳) | ✅ | 否 |

---

## 6. 倾向与待决项

**倾向(按最小复杂度原则修正)**:**先采用方案 0(skill-only)作基线并验证**——它最简单、当前已基本就绪、红线可用、全 mode 可用、无 git 冲突。**只有当基线验证不达标时才升级**:升级首选 **方案 A(独立 lead 层 agent)** 或 **方案 D(混合版)**(都把红线提到常驻 SOUL.md);**方案 C 排除**(subagent 反问失效);**方案 B** 仅在"接受单一合成助手"时考虑。

### 升级触发条件(evaluation 驱动,不拍脑袋)

用一组造价/规范测试问法跑**默认 agent(skill-only)**,量两个指标:

1. **路由率**:造价/规范类问题中,模型真正去调 `cost.py`/`qa.py` 的比例(而非自己瞎答)。
2. **红线遵守率**:**不带版本**的问法中,模型**真的先 `ask_clarification` 反问版本**的比例(而非替用户猜默认版本)。

- 两项**达标 → 停在 skill-only**,不升级(符合最小复杂度原则)。
- **不达标(尤其红线遵守率低)→ 升级**到 A / D。**红线遵守率是安全攸关判据**(版本错=错价,容错率低),其权重高于路由率。

> 升级是平滑的:A/D 仍复用同一份 SKILL.md + 脚本,只是加一层常驻 SOUL.md 框架,无返工。

**待决项**:
1. **mode 可用性**:方案 D 受 ultra-only 限制——是否接受?或要解决 `subagent_enabled` 常开(改前端 mode 映射 vs 改 agent.py)?这直接决定 D 与 A 的取舍。
2. **是否要严格两个独立红线** vs 接受一个合成造价助手(决定 是否排除 B)。
3. 若走 A:接受"git 源(`ce-agents/`)+ 部署物化"两段式吗?物化目标 `base_dir` 需在服务器确认(`backend/.deer-flow` vs 项目根 `.deer-flow`)。
4. 是否给 SKILL.md 补 `allowed-tools: [bash, ask_clarification]` 作工具兜底(对弱模型尤其值)。

**风险(贯穿所有方案)**:本地 Qwen3-8B 的 function-calling/多轮反问/调脚本不稳。缓解:prompt 极度指令化 + 给死 bash 命令模板;每个 agent 只挂一个 skill;验证先用"已给全参数"样例跳过反问、再单测反问;实在不稳再单独给造价 agent 指定 `model: qwen-plus`(不动默认助手)。

---

## 7. 新增 subagent 的开发步骤(ultra 模式)

> 适用于"要把某个**参数已齐、自主跑完、不与用户交互**的子任务做成 subagent"。subagent 仅在 **ultra** 可达(`subagent_enabled`,§3.3),由 lead agent 经 `task` 委派。
> 关联代码:`config.yaml` `subagents.custom_agents`(定义来源)、`subagents/config.py::SubagentConfig`(字段 schema)、`subagents/executor.py::SubagentExecutor._create_agent`(执行)、现有样例 `cost-agent`/`norm-qa`(config.yaml:143 起)。

### 7.1 三条硬约束(设计前先记住)

1. **subagent 不能反问用户**(§3.2):`ClarificationMiddleware` 仅在 lead 链;subagent 后台 `astream` 自主跑完,**即使配了 `ask_clarification` 也是空操作**。→ 必须做成「参数已齐的纯执行器」,所有澄清提到 lead 层先做完(方案 D)。
2. **仅 ultra 可达**(§3.3):flash/thinking/pro 下 lead 没有 `task` 工具,subagent 不可达。要降档常开得改 `subagent_enabled` 来源(动前端 mode 映射或 agent.py)。
3. **两跳 + 弱模型**:lead 发 `task` + subagent 执行串联,Qwen3-8B 上更不稳。缓解见 7.4。

### 7.2 开发步骤

1. **先做 skill(底层能力单元)**:`skills/public/{name}/SKILL.md` + 薄客户端脚本(纯 urllib 零依赖,沙箱内 `bash` 直接 `python3` 调,转发给常驻服务)。随 git;`extensions_config.json` 里置 `enabled`。
2. **在 `config.yaml` 注册** `subagents.custom_agents.{name}`(照 cost-agent/norm-qa 抄),字段见 7.3。`skills: [{name}]` 指向第 1 步的 skill。
3. **commit/push → 服务器 `git pull`**。**无需物化拷贝**——config.yaml 本身随 git,不撞 `.deer-flow/` gitignore(这是 subagent 相对方案 A 独立 lead agent 的开发优势,§3.4)。
4. **验证**(ultra 模式):① 先用「参数已齐」样例,看 lead 是否经 `task` 委派、subagent 是否带对参数调脚本;② 若该任务需澄清,单测 lead 层先 `ask_clarification` 收齐参数、再 `task` 下发(方案 D 链路)。

### 7.3 `SubagentConfig` 字段(照抄即可)

| 字段 | 作用 | 给弱模型的建议 |
|---|---|---|
| `description` | **lead 路由依据**——"什么时候该委派给我" | 写清触发场景,别写人格 |
| `system_prompt` | subagent 执行指令(**第 1 轮就常驻**,无渐进披露) | 极度指令化 + 给死 bash 命令模板 |
| `tools` | 工具白名单(如 `[bash]`) | 只给必需的 |
| `disallowed_tools` | 黑名单(现有都禁 `task`/`present_files`) | 至少禁 `task` 防递归 |
| `skills` | skill 白名单 | **一个 subagent 只挂一个 skill** |
| `model` | `inherit` 或具体模型名 | 不稳就单独指 `qwen-plus` |
| `max_turns` / `timeout_seconds` | 轮数 / 超时(默认 900s) | 取数类 10 轮 / 600s 够用 |

### 7.4 可靠性要点

- subagent 只当「参数已齐的脚本执行器」,完美契合其"自主跑完、不交互"定位;凡需反问的逻辑一律提到 lead 层(方案 D)。
- 单 skill、锁死工具(白名单 + `disallowed_tools`)、bash 命令给模板;实在不稳给该 subagent 单独 `model: qwen-plus`(不动默认助手)。
- 并发上限 3、默认 15 分钟超时(见 `backend/CLAUDE.md` Subagent System);取数类任务按 7.3 收紧 `max_turns`/`timeout_seconds`。

---

> 决策记录:
> - **最小复杂度优先**:agent 比 skill 重,能 skill 解决就不升级 agent。**skill-only(方案 0)定为基线**,A/B/C/D 均为"基线不达标时的升级路径";是否升级由 evaluation(路由率 + 红线遵守率)决定,不默认做 agent。
> - skill-only 在弱模型上的唯一实质短板是"红线强制力"(渐进披露 vs agent 常驻 SOUL.md);版本红线安全攸关,故以**红线遵守率**为主判据。
> - 升级**无返工**:agent 复用同一份 SKILL.md + 脚本,仅叠加 SOUL.md 与独立入口。
> - subagent **结构上无法**与用户做交互式澄清(ClarificationMiddleware 仅在 lead 链)——这是排除"纯 subagent 调度"的决定性依据。
> - 凡核心流程需"先反问"的 agent,**必须跑在 lead 链上**(作默认 agent、lead 层自定义 agent,或混合版里由 lead 层负责反问)。
> - 本地 Qwen3-8B 可靠性为独立风险项,后续单独评估是否对造价 agent 切 qwen-plus。
