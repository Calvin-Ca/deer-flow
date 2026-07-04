# 智能体主线 TODO（根目录总控）

> **定位**：跨层主线（agent 系统整体）的阶段与任务总控——只回答「现在处于什么阶段、下一步做什么、谁先谁后」。
> 层内细节各归其位：知识层 `ce-code/TODO.md`、任务层 `ce-services/TODO.md`、评测规范 `benchmark/AGENT_BENCHMARK.md`、
> 实现/决策记录 `AGENT_DEV.md`、需求权威 `AGENT_PRD.md`。本文件条目只挂结论与验收口径，不重复实现细节。

---

## 当前阶段：benchmark 测试 → 优化 → 迭代（2026-07-03 起）

**前情一句话**：四层骨架（AGENT_DEV §9）+ T9 PRD 对齐批次（§8）已全部落码，Docker 服务端 T9-7 验证全绿；
闭环评估结论——**工程闭环成立**（需求→代码→部署→验证→回归护栏→可观测），**产品闭环差两块**：
① 2013 组价数据未入库（默认口径 happy path 现为诚实 501）；② 真实用户流量 = 0（难例闭环未转起）。

**迭代机制**：改动 → 离线自测（77 例）+ 金标回归（`tools/prerouter_eval` 24 例）→ Langfuse runner 出分
（`benchmark/runner/`，prompt-variant tag 对比）→ 按 backlog 优先级推进。

---

## M0 · 数据前置：深圳·2013 定额/信息价/映射入库 🔴（与 M1 并行，不互相阻塞）

- [ ] 2013 源文档清点入 `ce-code/data/raw/`（深圳 2013 定额 / 对应期信息价 / 费率）——**需人工提供 PDF**
- [ ] 管道实跑：parser → splitter → `cost.*` 抽取 → `load_pg`（全套幂等管道现成，见 ce-code/README 数据轨）
- [ ] `SPEC_REGISTRY` 翻 `supports_compose=True`（一个标志，任务层零改码）
- **为什么是主线**：T9-1 后组价默认口径 = 深圳·2013（PRD C-05），此项决定默认路径何时从「诚实 501 仅选码」
  变为全链可用；同时是 M3 的 L6-A 组价终态评测（`init_context=深圳2013`）的数据前提。
- **归属**：ce-code 数据任务（细节见 `ce-code/TODO.md` 2026-07-03 节）。

## M1 · P0：benchmark 开工三件（零专家、立即可做）

- [ ] **B1 选码置信校准 + 8b/32b 决策**（backlog 唯一「高」，当前体验最痛：实测 96% 转人工）
  - 用 2013 选码 gold（n=91，与默认口径正好对上）跑 `ce-services/tools/benchmark.py`，拉选对/选错两组的
    真实 chosen_score 分布 → 精调 `CE_SELECT_{FLOOR,CEIL,MARGIN}`（`common/config.py`）
  - 同批数据跑 8b vs 32b 选码对比（`tools/models.json` 已备）→ 定选码是否值得走桶 B
  - **验收**：L2 误直配率 ≤1% 且转人工率明显下降（`benchmark/AGENT_BENCHMARK.md` §2-L2 / §9-B1）
- [ ] **τ_high/τ_low 调参曲线**（与 B1 同批实验数据）
  - dev 集扫双阈值，画「误直配率 vs 直配覆盖率」权衡曲线，选点后 test 验证（只许 dev 调参）
  - **验收**：L2 门线 + 曲线选点记录进 AGENT_DEV
- [ ] **评测管线阶段 0/1**（AGENT_BENCHMARK §7 阶段 0+1 / §8 无专家版）
  - 阶段 0：按 §4.2（请求级）/ §4.4（任务级）schema 写判定脚本，进 CI 空跑
  - 阶段 1：L1 路由 + L6-B 工具调用金标**从零合成**（槽位枚举 + 规则推导标签 = 机判金标；
    每落点 ≥50、EH ≥80、总量 ~800–1200、dev/test 切分冻结）
  - ⚠️ 现有 `routing_eval` 24 例**不作种子**（合成非金标），降级为编排层回归护栏继续跑
  - **验收**：L1 总体 ≥95% 且无单行 <90%、危险误分 ≈0；L6-B schema 合法率 ≥99%

## M2 · P1（M1 出数后接续）

- [ ] **L6-C 规范问答忠实度**：条文库反向构造 gold_contexts + 异源模型校验 → 第一个可信 RAGAS 数
  （门线 faithfulness ≥0.9、引用准确率 =100%）
- [ ] **L3 检索评测**：Recall@k/MRR 回归基线 + 版本对齐召回 + 地域泄漏率 =0；顺带接
  ce-code 既有召回缺口靶子（「矩形梁 010502011 未进 top-10」→ sparse 混检，见 ce-code/TODO 三）
- [ ] **B2 剩余：FR-X02 比选价差**：compound 路径两个 cost 子任务结果做确定性价差对比
  （FR-I01/02 取数腿已于 T9 接入；依赖 M0 或用 2024 数据先行）

## M3 · P2（依赖 M0/M1 产出）

- [ ] **L6-A 组价终态 pass^5**：反向构造主干（定额库反推 terminal_check）+ 容差 silver 占位；
  红线违规率独立计分 =0（依赖 M0 的 2013 数据）
- [ ] **L5 复合拆解**：子任务集合 P/R/F1（compound 金标用例先补入 M1 管线）
- [ ] **L7 NFR 压测**：P95 延迟分桶（FR-K ≤3s / FR-P01 ≤2s / ReAct 单轮 ≤5s——PRD 硬指标，从未量过）+
  多租户隔离 + 可观测追溯

## M4 · 组价步骤前端可视化（新线，独立于 benchmark，可并行）

> 用户输入「实体名称 + 做法特征」触发组价后，**清单匹配 → 套定额 → 取价 → 单位工程汇总 → 单项工程汇总 → 总造价**
> 各步骤在前端逐步显示。完整方案（文件清单/数据契约/红线自检）见 `ce-services/COST_STEP_DISPLAY_PLAN.md`。
> 现状：后端每节点已发 `events`（`cost/state.py` 累积）、前端已有 HITL 内嵌卡，**唯缺时间线渲染**——数据管道全通。

- [x] **阶段 0 · 前端步骤时间线**：新增 `step-timeline.tsx` + `core/cost/step-format.ts` 渲染 `events`，
  `cost-hitl-inline.tsx` 的 `Snapshot` 接入 `events`。**已验收**（服务器 pnpm check + vitest 95/95 绿，2026-07-04）
- [x] **阶段 1 · SSE 逐节点流式**：`session.py` 抽 `_initial_state` + 加 `_run_stream`/`stream_start`/`stream_resume`
  （`.stream(stream_mode="updates")`）+ `router.py` 两个 `text/event-stream` 端点（`X-Accel-Buffering:no`）+
  代理 `route.ts` 改 body 透传（运行时 `CE_SERVICES_BASE_URL`）+ `client.ts` `consumeSSE`/`streamResume` +
  `cost-hitl-inline` decide 流式增量 + docker-compose frontend 加 extra_hosts/CE_SERVICES_BASE_URL 打通容器→:8101。
  **前端 check+vitest 绿 + 后端 SSE curl 逐帧 + 全链路代理实测通过**（2026-07-04：`curl :2026/ce-cost/session/start`
  穿 nginx→前端新 route.ts→:8101 回真实会话，证前端新代码 + extra_hosts/CE_SERVICES_BASE_URL 生效）。
  部署坑见记忆 docker-daemon-split（app/ce-services 在 rootful sudo，改码须 `--no-cache`+`--force-recreate`）。
  **浏览器 e2e 已过**（2026-07-04 生产栈：时间线逐条 + 层级树，见下方 Option B 收官行）
- [x] **阶段 2 · 两级汇总（单位工程/单项工程）**：item 加 `unit_work/single_work` 分组标签 + `pricing.py` 新增
  `rollup_hierarchy` 原语 + `graph.py` 拆 `rollup_compute` 逐层发 `unit_rollup/single_rollup/rollup` 事件 + `price_gate_node` 补 `price` auto_pass 事件 + 前端 `HierarchyTree` 层级树渲染。
  **v1 范围**：措施/规费保持项目级（单位工程级费用留 v2，已在方案标注）。**已验收**（后端 `tools/test_rollup_hierarchy.py` 4/4 + 前端 check 绿，2026-07-04）；⚠️ 端到端真跑（多构件带分组）待补
- [x] **阶段 3 · 点火型 MCP `start_cost_session`**：mcp_server 加只点火不编排的起会话工具（懒加载 session.start，
  返回 task_id + `cost-hitl` marker + first_gate），extensions_config + cost-agent SKILL 同步；py_compile 过。
  编排仍在图里，MCP 不当编排器（红线 HITL_DESIGN §10）。
- [x] **Option B · 前门确定性点火 HITL**（治「弱模型不调 start / 漏贴 marker → 卡不出」）：`prerouter` 加
  确定性 `compose_full` 形态（cost + 「到总造价/逐步确认」信号）+ `orchestrator` 单一意图 `_ignite_hitl` 点火
  session.start 返回 `{mode:hitl,task_id}`（去 first_gate 抑制弱模型转述）+ 前端 message-group 泛化扫任意
  `ce-task_*` 结果里的 task_id 渲染卡（不靠模型贴 marker）。**Docker 生产栈 e2e 全绿**（2026-07-04：卡片 +
  11 步时间线 + 两级层级树 + 算到真总价 45314.8）。prerouter 27 / orchestrator 11 自测过。
- [x] **M4 阶段 0/1/2 浏览器 e2e 收官**（2026-07-04 生产栈实跑通过，阶段 1 不再仅 headless）。

### M4 backlog（已论证、未排期）

- [ ] **UI 抛光**：note 已改（去照抄话术/方向词，治重复+方向矛盾）；可选 ①「引导在上、卡在下」布局
  （前端 message-list 组合顺序）②深修默认 lead prompt「mode:hitl 只产极简回合」（要先定位 lead prompt，
  DEV §9 说「默认 lead + 改后 prompt」，不在 config.yaml subagents）。**先验 note 效果，大概率够。**
- [ ] **B2 · Claude 原生「停-答-续」HITL（可选深修）**：让 agent 回合真暂停在每个闸、人答完再续、done 后
  agent 收尾。详细改造清单 + 工作量 + 前置调研见 `ce-services/COST_STEP_DISPLAY_PLAN.md §7`。要点：**非阻塞**
  （阻塞会死锁）——走 deer-flow agent 图内自定义节点 + **节点内嵌套 interrupt**（LLM 只调 2 次、不撞 §10 红线）；
  **代价大一个量级**（动 `backend/` 上游 harness 核心 + 前端 interrupt 渲染）。**前置调研先行**（deer-flow 图是否
  支持节点内循环 interrupt，0.5–1 人日）。仅当「agent 必须在场」被确认为刚需才启动；**未排期**。

## 意图混合路由（确定性 + LLM 兜底，归 benchmark M1 路由线，未排期）

- [ ] **prerouter 加 LLM 兜底分类**（治关键词穷举疲劳 + 口语变体漏判，不把红线交给弱模型）：确定性命中
  **强信号**（COST/PRICE_STRONG/COMPOUND/显式GB/`_COMPARE_RE`）→ 直接走（多数流量、零延迟、金标可回归）；
  未命中 / 只命中泛词（`区别/构成/包含哪些`）→ **32b 兜底分类器**（temp=0、enum JSON schema、校验 + fail-safe
  跌回确定性默认）。**红线 guard（出界 EH-03 / 口径 C-02）两条路都确定性**，LLM 只补 capability+form 分类、
  不碰安全闸、不覆盖。改动：`prerouter` 加「命中强信号 vs 兜底默认」置信信号 + 一个 LLM 兜底函数；
  orchestrator/router 低置信时调兜底。测：确定性层金标（`routing_eval`）不动 + 兜底层建「难例/含糊」集，
  量**升级率 + LLM 路由准确率 + 延迟**（对齐 FR-K ≤3s NFR）。量：中（后端 + 新 eval）。详见对话 2026-07-04 讨论。

## 小尾巴（不阻塞主线，择机清）

- [ ] 前端三条人工判读（B1 口径声明行 / A1→A9 会话粘性 / B11 出界话术转述）——gateway 热加载随时可测；
  若弱模型转述不守约归 backlog B4（结果卡已兜底）
- [ ] `ce-services/TODO.md` 内网试用 checklist 的 P1/P2 余项（常驻化等，部分已被 Docker 化覆盖，待对账勾销）

## 挂起（决策在案，勿重复排查）

- **联网兜底重开**：服务器有公网但 DDG/Jina 被墙、无代理 → 保持 `CE_NORM_WEB_FALLBACK=0`；
  重开路线 A（代理，零代码）/ B（博查等国内搜索 API，半天）见 `AGENT_DEV.md` §8.4 决策 1
- **silver→gold 升级**：费率容差 / 组价合理性 / 复合拆解合理性等判断类金标，等造价专家到位
  （AGENT_BENCHMARK §8.4 硬边界）
- **真实流量难例闭环**：等内网试用起量后，用户日志歧义/缺特征/复合请求回灌评测集（§4.1 难例来源）
