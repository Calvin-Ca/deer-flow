# 智能问答 / 智能组价 Agent 集成 · 开发实施方案

> 本文是**落地实施文档**(怎么建、分几步、任务拆解)。方案取舍的论证、deer-flow 机制事实、候选方案对比见 `agent_integration_DISCUSSION.md`(讨论记录),本文不重复论证,只给可执行步骤。
> 关联代码:`backend/packages/harness/deerflow/agents/lead_agent/agent.py`(lead 工厂)、`.../subagents/executor.py`(subagent 执行)、`.../config/agents_config.py`(lead 层自定义 agent schema)、`.../config/subagents_config.py`(subagent schema)、`config.yaml`(subagents 注册)、`extensions_config.json`(skill 启用)。
> 关联文档:`ce-services/PRD.md`(任务层 /norm/qa /cost/compose)、`ce-code/PRD.md`(知识底座 :8100)、`cost_agent_prd.md`(CostAgent 产品)。

---

## 0. 总原则与优先级

**最小复杂度优先 + evaluation 驱动升级**:能 skill 解决就不升级 agent;是否升级由评测数据决定,不预先排期。

| 优先级 | 方案 | 何时做 | 一句话 |
|---|---|---|---|
| **P0(现在做)** | **方案 0 skill-only** | 立即 | 默认 lead agent 继承已启用 skill,全 mode 可用,零新增架构 |
| **P1(条件升级)** | **方案 A 独立 lead 层 agent** | 方案 0 评测**红线遵守率不达标**时 | 专属 agent + 常驻 SOUL.md 红线,单跳、全 mode、行为一致 |
| **P2(仅特定场景)** | **方案 D 混合(lead 反问 + subagent 执行)** | 确认瓶颈是**需隔离上下文 / 并行跑重取数**(而非红线)时 | lead 收参 + subagent 当纯执行器,ultra-only、两跳 |

**升级判定门(P0→P1/P2 的唯一依据)**:用一组造价/规范测试问法跑方案 0,量两项指标——

1. **路由率**:造价/规范类问题中,模型真去调 `cost.py`/`qa.py` 的比例。
2. **红线遵守率(主判据,安全攸关)**:**不带版本**的问法中,模型真先 `ask_clarification` 反问版本的比例。

- 两项达标 → **停在方案 0**,不做 A/D。
- 红线遵守率不达标 → **升级方案 A**(常驻 SOUL.md 直接补强红线;方案 D 治不到此病根,见 DISCUSSION §3 建议)。
- 仅当瓶颈是隔离/并行执行(非红线)→ 才考虑方案 D。

---

## 1. 方案 0 —— skill-only(P0,现在做)

### 1.1 架构

```
用户 → 默认 lead agent(agent_name=None,继承全部已启用 skill)
        ├─ system prompt 渐进披露:norm-qa / cost-agent 的名字+简介
        ├─ 跑在 lead 链 → 含 ClarificationMiddleware → ask_clarification 反问版本红线可用
        └─ bash 调脚本
             ├─ /mnt/skills/public/norm-qa/qa.py  → 任务服务 :8101 /norm/qa  → 知识 :8100 /search
             └─ /mnt/skills/public/cost-agent/cost.py → :8101 /cost/compose → :8100 bill_match/price_compose
```

要点:skill 注入不依赖 `subagent_enabled` → **flash/thinking/pro/ultra 四档全可用**;无新增 agent、无 git 冲突(skill 已随 git,启用在 `extensions_config.json`)。

### 1.2 实施步骤

1. **确认 skill 资产就绪**:`skills/public/norm-qa/SKILL.md` + `qa.py`、`skills/public/cost-agent/SKILL.md` + `cost.py`(纯 urllib 零依赖薄客户端)。
2. **确认启用**:`extensions_config.json` 中 `norm-qa: enabled`、`cost-agent: enabled`。
3. **强化 SKILL.md 红线表述**(弱模型关键):在两份 SKILL.md 顶部用极度指令化的语言写明「未指明 2013/2024 版本必须先 `ask_clarification` 反问、不猜默认、不编造编码/条文/价格」,并给死 bash 命令模板。
4. **(可选)补 `allowed-tools` 兜底**:SKILL.md 加 `allowed-tools: [bash, ask_clarification]`,对弱模型收敛工具面(DISCUSSION §6 待决项 4)。
5. **ultra 歧义处理**:方案 0 阶段,ultra 下默认 agent 既能走 skill 又能 `task` 委派现存 subagent(config.yaml 仍注册着 norm-qa/cost-agent)。为评测口径干净,**评测在 flash/thinking/pro 下进行**;是否在方案 0 阶段临时从 `subagents.custom_agents` 摘掉这两个注册以消除 ultra 双脑歧义,见 §4 待决项。

### 1.3 任务计划

- [ ] T0-1 校对两份 SKILL.md 红线段落 + bash 模板(指令化、给死参数格式)
- [ ] T0-2 确认 `extensions_config.json` 两 skill 已启用
- [ ] T0-3 服务器起 :8100 / :8101 常驻服务,bash 手测 `qa.py`/`cost.py` 连通(先用"已给全参数"样例)
- [ ] T0-4 建评测集:造价/规范问法 N 条,含"带版本"与"不带版本"两组,覆盖路由率 + 红线遵守率
- [ ] T0-5 跑评测,出两项指标 → 决定是否升级(达标即收手)

---

## 2. 方案 A —— 独立 lead 层自定义 agent(P1,红线不达标时升级)

### 2.1 架构

```
前端 agent 选择器 → agent_name 路由 → _make_lead_agent 走自定义 agent 分支
   norm-qa / cost-agent 各为一个独立 agent:
     config.yaml(name/description/model/tool_groups/skills) + SOUL.md(人格 + 常驻红线)
   ├─ 跑在 lead 链(含 ClarificationMiddleware)→ 反问可用
   ├─ SOUL.md 从第 1 轮常驻 system prompt → 红线强制力高于 skill 渐进披露
   ├─ skills 字段指向同一份 SKILL.md + 脚本(零返工,资产复用)
   └─ 单跳、全 mode 可用、行为一致
```

`AgentConfig` 字段(`agents_config.py:38`):`name` / `description` / `model`(None=默认)/ `tool_groups`(筛 config.yaml tools 段)/ `skills`(白名单,指向对应 skill)。人格与红线写在同目录 `SOUL.md`。

### 2.2 实施步骤

1. **git 跟踪源**:新建 `ce-agents/cost-agent/{config.yaml,SOUL.md}`、`ce-agents/norm-qa/{config.yaml,SOUL.md}`(随 git,解决 `.deer-flow/` 被 gitignore 的冲突)。
   - `config.yaml`:`skills: [cost-agent]`(或 norm-qa)、`tool_groups` 收窄到必需、`model` 视可靠性留空或指 `qwen-plus`。
   - `SOUL.md`:把版本红线写成常驻第一性约束(未指明版本必反问、不猜、不编造)。
2. **部署物化**(两段式):服务器 `git pull` 后单行物化到 `base_dir`(物化目标需先确认,见 §4 待决项 3):
   `mkdir -p backend/.deer-flow/agents && cp -rf ce-agents/cost-agent ce-agents/norm-qa backend/.deer-flow/agents/`
3. **前端选择器**:确保用户能在前端选到这两个 agent(`agent_name` 随消息进 `configurable`)。
4. **(可选)工具兜底**:SKILL.md / `tool_groups` 双重锁工具面。
5. **回归**:对两个 agent 各跑 §0 评测两项指标,红线遵守率应显著高于方案 0。

### 2.3 任务计划

- [ ] TA-1 设计 `ce-agents/{cost-agent,norm-qa}/config.yaml`(skills/tool_groups/model)
- [ ] TA-2 撰写 `ce-agents/{cost-agent,norm-qa}/SOUL.md`(常驻红线 + 人格 + bash 模板)
- [ ] TA-3 写部署物化脚本/文档(git pull → cp 到 base_dir),确认 base_dir 落点
- [ ] TA-4 前端选择器接入,验证 agent_name 路由命中正确分支
- [ ] TA-5 复跑评测,对比方案 0 的红线遵守率提升

---

## 3. 方案 D —— 混合(lead 反问 + subagent 执行)(P2,仅隔离/并行场景)

> 仅当确认瓶颈是「需上下文隔离 / 并行跑多个重取数子任务」时才做;**红线问题用方案 A 解,不用 D**(D 的反问仍在通用 lead 层,治不到红线强制力)。受 **ultra-only** 限制。

### 3.1 架构

```
lead 层(含 ClarificationMiddleware,负责交互)
  └─ ask_clarification 收齐 [版本 + 构件/问题描述]
        ↓ 参数齐后 task() 下发"参数已完整"子任务
subagent(纯执行器,无 ClarificationMiddleware)
  └─ 拿全参数 → bash 调 cost.py / qa.py → 返回结果
```

硬约束(DISCUSSION §3.2/§3.3):subagent **不能反问**(配了 `ask_clarification` 也是空操作)→ 必须做成参数已齐的执行器;**仅 ultra** 可达;lead 发 task + subagent 执行 = **两跳**。

### 3.2 实施步骤

1. **lead 层收参**:在 lead 层写"路由 + 收参"prompt——识别造价/规范意图 → 用 `ask_clarification` 收齐版本+描述 → 参数齐后才 `task` 下发(注入方式见 §4 待决项 2,避免分叉 harness 核心)。
2. **subagent 注册**(`config.yaml` `subagents.custom_agents.{name}`,随 git 无物化坑,照 DISCUSSION §7.3 字段表):
   - `description`:lead 路由依据(何时委派)。
   - `system_prompt`:**假定参数已齐**的纯执行指令 + 给死 bash 模板;**不要**再让它反问。
   - `tools: [bash]`、`disallowed_tools: [task, present_files]`、`skills: [{name}]`、`model: inherit`(或 `qwen-plus`)、`max_turns: 10`、`timeout_seconds: 600`。
3. **解 ultra-only**(若产品要求非 ultra 也能用):改 `subagent_enabled` 来源(动前端 mode 映射 vs 改 agent.py 核心),见 §4 待决项 1。
4. **验证**:① "参数已齐"样例 → lead 经 task 委派、subagent 带对参数;② 需澄清样例 → lead 先 `ask_clarification` 收齐再 task。

### 3.3 任务计划

- [ ] TD-1 确认进入 P2 的前置:评测证明瓶颈是隔离/并行(非红线)
- [ ] TD-2 解决待决项 1(ultra-only 是否接受 / 如何常开 subagent_enabled)
- [ ] TD-3 设计 lead 层"路由+收参"prompt 注入点(不分叉核心)
- [ ] TD-4 在 config.yaml 注册/调整 norm-qa/cost-agent subagent(纯执行器化,去掉无效 ask_clarification)
- [ ] TD-5 端到端验证两跳链路(已齐参数 + 需澄清两类样例)

---

## 4. 待决项(升级前需拍板)

1. **ultra-only(影响 D)**:方案 D 仅 ultra 可用是否接受?否则需解决 `subagent_enabled` 常开(改前端 mode 映射 vs 改 agent.py 核心)。
2. **lead 层 prompt 注入方式(影响 D)**:"路由+收参"prompt 怎么进默认 lead 而不分叉 harness 核心(skill 渐进披露 vs 改 prompt 模板)。
3. **物化落点(影响 A)**:`base_dir` 取 `backend/.deer-flow` 还是项目根 `.deer-flow`?per-user(`users/{uid}/agents/`)还是 legacy 共享(`agents/`)?需在服务器确认。
4. **工具兜底**:是否给 SKILL.md 补 `allowed-tools: [bash, ask_clarification]`(对弱模型尤其值)。
5. **模型切换**:本地 Qwen3-8B function-calling/多轮反问不稳是贯穿风险;是否对造价 agent 单独指 `model: qwen-plus`(不动默认助手)。

---

> 决策记录(本文新增,补充 DISCUSSION 的结论):
> - 优先级定为 **P0 方案 0 → P1 方案 A → P2 方案 D**;升级 evaluation 驱动,达标即收手。
> - 升级首选 **A 而非 D**:红线遵守率是主判据,A 的常驻 SOUL.md 直接补强红线、单跳、全 mode 行为一致;D 的反问仍在通用 lead 层,治不到红线病根。
> - **D 仅用于"需隔离上下文 / 并行跑重取数"场景**,不拿它补红线;受 ultra-only + 两跳限制。
> - 三方案复用同一份 SKILL.md + 脚本,升级无返工。
