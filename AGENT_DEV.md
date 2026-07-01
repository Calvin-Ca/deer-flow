# 智能体开发文档

> 文档定位：从 `AGENT_PRD.md`（需求/路由权威）落到实现态的开发记录。**§1–7 为 HITL（人介入）方案**——现状盘点、与 PRD 的契合/冲突评估、改造方案；**§8 为「深圳·2013 口径收窄 + 规范问答联网兜底」落地方案**（对应 AGENT_PROBLEM 问题 9「智能体能力有界」、AGENT_PRD commit 52ac6008）。
> 关联：需求见 `AGENT_PRD.md`；HITL 图实现见 `ce-services/cost/`（`graph.py` / `session.py` / `gates.py`）；HITL 设计细节见 `ce-services/HITL_DESIGN.md`；问题复盘见 `AGENT_PROBLEM.md`。

---

## 1. HITL 交互模型（结论先行）

**当前实现 = 逐闸 stop-and-go**：图跑到一个闸就 `interrupt()` 暂停、返回这一个闸的 payload、等一次 `resume` 注入决策、再继续跑到下个闸。与 Claude Code「要执行某条 bash 就停下等确认、确认后继续」同一个模型；**不是**「开局把所有问题列出来、用户填完一次性 submit」。

- **跨流程**：逐闸 stop-and-go（一次 `resume` 只喂当前闸的决策，`session.py:97`）。
- **单闸内**：可能是带多字段的小表单（如 setup 闸一次填 `spec_version/region/period/price_source`，`graph.py:62-69`）；缺价闸是**循环 interrupt**（N 个缺价材料逐个回填，`graph.py:197`）。
- **两种闸型**：confirm 闸（编码/定额，给候选让「采纳/选备选/手填/否决」）、input 闸（setup/费率/参数/缺价，一组字段一起填）。

### 1.1 单向不可回退（现状约束）

当前走到下一步**无法回改上一步**，是设计如此的单向前进，不是 bug：

- `session.py` 只暴露 `start/resume/get_state` 三动作，`resume` 只做 `Command(resume=decision)`——只能往前推当前挂起的闸，无 rewind/undo 入口。
- `graph.py:403-413` 是严格线性单向边、无回边无环（唯一分支 `_has_code` 只是「有码继续 / 没码收尾」）。
- 闸一旦 resume 即通过 `lock_value(...)` 钉死（`locked=True`），下游节点不重读/改写上游已钉值。
- **底层能力其实在**：`SqliteSaver` 逐节点 checkpoint，历史快照都在 `.hitl_checkpoints.db`；理论上用 langgraph `get_state_history()` + `update_state(checkpoint_id=...)` 做时间旅行可回退，只是 `session.py` 没封装。

---

## 2. 与 AGENT_PRD 的契合度评估

判断：**交互粒度（逐闸确认）对、且必须保留；但整体形态（固定全流程线性 DAG、单向不可回退、一路算到总造价）与 PRD 架构有数处实打实的冲突。**

### 2.1 契合（保留，别推翻）

| 点 | 依据 |
|---|---|
| 逐闸确认 = §4.4 置信度门控的落地 | `list_gate`/`quota_gate` 走 `should_pause_*(env, τ)`：高置信自动过、低置信才停，与「高置信直配绕 LLM / 低置信澄清」一致 |
| 逐闸 + provenance 事件 = C-01 全量溯源 / §8.2 可观测 | 每闸一条审计、每数字带信封，stop-and-go 比「批量一次性 submit」更能逐项留痕——**故不应改成「开局列全部问题一次提交」**，那会弱化审计 |

### 2.2 冲突（关键，需改）

1. **PRD 的 HITL 本质是「ReAct 特征澄清循环」，不是固定全流程闸机。** **（改 1 已落地解决）** §8.2 明确「澄清结果回填后**重走 §4.4 门控**」——clarify→回填→重新门控的**回环**；原图是无回边 DAG、单向不可回退。现加 `feature_gate→list_match` 条件回边实现该回环。
2. **PRD 头号澄清场景——特征澄清——当前图没实现。** **（改 1 已落地解决）** FR-P02/EH-04（「只写'砌筑'，缺砌块/砂浆强度」）要求先反问补关键特征再匹配；现 `feature_gate` 以 LLM 抽取缺口（`clarify.py`）+ 回环重匹配落地。
3. **缺「工程量 Q 录入闸」，当前静默按 Q=1 算出错误总价。** Q 由**用户录入**（非 BIM 自动算——PRD §1.2 范围外的是 Q 的几何/扣减**计算**，用户手填一个已知 Q 是正常 HITL 输入，不越界）。但当前图**没有任何闸收 Q**：`_unit_price_for` 构造 `UnitPriceInput` 时不传 `quantity`，而 `pricing.py:80` `quantity` 默认 1.0，故 `total_price = 综合单价 × 1`。`_compute_rollup` 把一堆「Q=1 的综合单价」当分部分项合价相加——**不报错、直接定稿一个名不副实的总造价**。这是「静默用错误默认值」，比报错更危险。
   - 注：综合单价/总造价段本身**不违 C-04**——`compute_unit_price`/`rollup_cost` 是确定性「计算工具能力」，HITL 只负责收 Q/rates/params 再喂给它。问题不在「该不该算总造价」，而在「缺一个把 Q 喂进去的闸」。
4. **缺 Orchestrator/意图路由层。** PRD 骨架是 Orchestrator 按 §4.3 在「两 Agent + 两能力」间路由；当前 HITL 是单体组价管线，无意图分类、无规范问答介入、无复合拆解（EH-01）。它更像「组价 happy-path 脚手架」，不是 PRD 描述的 agent 系统。

---

## 3. 改造方案（保闸粒度，改闸机形态）

不是推倒重来，而是「逐闸 stop-and-go 的粒度留下、固定全流程 DAG 的形态要改」。

| 改动 | 优先级 | 内容 | 解决的冲突 |
|---|---|---|---|
| 改 1 · 特征澄清闸 | 必须 · **已落地** | `list_match` 后加 `feature_gate`：缺关键特征→反问补全→回填进 `feature`→**回环重跑 `list_match`+门控**（条件边 `feature_gate→list_match`）。缺口判定走 LLM 抽取（`clarify.extract_missing_features`，降级安全：不可靠则不澄清、交 list_gate 兜底）；回环由 `MAX_CLARIFY_ROUNDS=2` 截断，无死循环。代码见 `cost/clarify.py` + `cost/graph.py`(`list_match_node`/`feature_gate_node`/`_after_feature`) | §2.2 之 1+2（不可回退 & 头号用例缺失一并解决） |
| 改 2 · 补 Q 录入闸 | 高 · **已落地** | 在 `price_gate` 之后、`rates_gate` 之前新增 `quantity_gate` input 闸收工程量 Q，透传 `_unit_price_for(..., quantity=Q)` → `compute_unit_price`，修掉「静默 Q=1 出错误总价」。无基价时跳闸、有基价缺 Q 标 `missing_quantity`/blocked、不静默按 1 计。可经 `SessionStartRequest.quantity` 预供则自动过。总造价段保留（合规、不违 C-04）。代码见 `cost/{graph,gates,session,router,state}.py` | §2.2 之 3 |
| 改 3 · 架构归位 | 中 | 这条线性图应是 Orchestrator 路由后**组价 Agent 内部**的一种 pipeline 形态，意图路由（§4.3）置于其上 | §2.2 之 4 |

### 3.1 回退能力（与改 1 配套）· **已落地**

`session.py::rewind(task_id, to_node)` —— langgraph 时间旅行：`get_state_history` 定位「目标闸即将执行」的最近 checkpoint（`snapshot.next` 含 to_node），从它重 `invoke(None)`。上游 compute（含 LLM 选码/取数）不重跑（checkpoint 已存），目标闸**之后**的锁值（编码/定额/价/Q/费率/参数）随回退作废、由用户重答。`to_node` 仅限闸节点（`REWINDABLE_GATES`，compute 节点拦截为 error）。端点 `POST /cost/session/{task_id}/rewind {to_node}`。验证：回退作废后续锁值、重答生效、非法目标拦截均通过。

---

## 4. 当前 HITL 路由（参考）

ce-services 任务层 `:8101`，挂在 `cost_router`（`main.py:46`，无额外 prefix）：

| 方法 & 路由 | 作用 | 关键入/出参 |
|---|---|---|
| `POST /cost/session/start` | 起可中断会话，跑到首个闸或 done | 入 `SessionStartRequest(feature, spec, region, period, price_source, rates)`；出 `{task_id, status, interrupt, events, items, overrides, audit_log}` |
| `POST /cost/session/{task_id}/resume` | 用决策续跑到下个闸或 done | 入 `req.decision`；出 下个 interrupt 或终态 |
| `GET /cost/session/{task_id}/state` | 只读当前持久化状态、不推进图 | 出 `{task_id, status, next, values}` |

- 与 `POST /cost/compose`（端到端「简单场景」旧路）并存，判据=是否需 HITL/可审计。
- 前端调试页：`/workspace/cost`（`frontend/src/app/workspace/cost/page.tsx`，渲染 `CostHitlPanel`），经同源代理 `/ce-cost/*` 转发到 `:8101`。
- 图链路：`setup → list_match → feature_gate →(缺特征?回环 list_match)→ list_gate →(有码?)→ compose → quota_gate → price_gate → quantity_gate → rates_gate → params_gate → rollup → done`。

---

## 5. 决策依据透明化（前端展示 · 已落地）

「为什么问你 / 为什么没问你」都进前端**依据时间线**（`CostHitlPanel` 的 `EventTimeline`），数据同源于 provenance 信封：

- **停闸的依据**：confirm 闸 payload 自带 `evidence{source_type, source_ref, confidence}` + `alternatives`；input 闸（含改 1 的特征澄清）用 `context` 字段承载「缺什么特征 / 为什么问」。
- **自动过（高置信）的依据**：自动过不打断，但每个门控节点经 `state.gate_event(...)` 往 `events` 写一条决策事件——`auto_pass=true`，编码闸再带 `confidence`/`tau`，前端渲染「自动采纳」徽标 + 「置信 X ≥ 阈值 τ，故未打断」。
- **覆盖的门控**：`list_gate`（带 τ）/ `quota_gate` / `quantity_gate` / `params_gate` / `rates_gate` 全部发 `gate_event`，所有闸的「停/过」决策在时间线可见、可审计（呼应 §8.2 可观测）。
- 代码：后端 `cost/state.py::gate_event` + `cost/graph.py` 各门控节点；前端 `core/cost/types.ts::CostEvent`（加 `auto_pass/tau/confidence`）+ `cost-hitl-panel.tsx::EventTimeline`。

---

## 6. P2 健壮性（配套 · 已落地）

| 项 | 内容 | 代码 |
|---|---|---|
| 前端 `context.why` 渲染 | 特征澄清闸的 `context.why`（数组）专门渲染成「为什么要补这些：· 砂浆等级 —— 缺砂浆等级无法定子目」；`CONTEXT_LABELS` 把 feature/code/unit… 显示成中文 | `frontend/.../cost/gates.tsx::InputGate` |
| rewind 回退 | 见 §3.1 | `cost/session.py` + 端点 |
| **多构件**（PRD §5.2 FR-P05） | **外层循环**：`current_item` 标当前在办件，per-item 闸（list_match→…→quantity_gate）只动 `items[current_item]`（`_put_item` 写回保留其余件），`advance` 推进到下一件；办完所有件才进**项目级**收尾——`rates_gate` 一套费率给每件算综合单价、`rollup` 汇总 Σ 各件。单个坏件（选不出码/缺 Q）`skip` 跳过、不拖死整单，`done_node` 仅当全无综合合价才整单 blocked。入口 `start(features=[...])` / `SessionStartRequest.features`。`clarify_rounds` 移至 item（各件独立计澄清预算） | `cost/graph.py`（`_put_item`/`advance_node`/`_after_advance`/`rates_gate` 项目级化）+ `cost/state.py`（`current_item`）+ `cost/session.py`/`router.py`（`features` 入口） |

验证（本地 mock，全过）：双构件各自 Q（×3/×5）、各算综合合价、`subtotal=Σ`、项目级费率一套管全单、rollup approve→done；单构件回归（特征澄清回环 + rewind）不受影响。

---

## 7. P0 服务器端到端验证（真 LLM Qwen3-8B + 知识服务 :8100，2026-06-29）

环境：:8100 知识 + :8101 任务 + :8099 LLM，经 curl 走真实 `start/resume/rewind/state`。结论：**HITL 主线五项全绿**。

| 场景 | 命令链 | 结果 |
|---|---|---|
| 充分描述单构件（实心砖墙） | start→list_coding→quota→price_item×9→quantity→rates(自动)→params→rollup | ✅ `total=1278139.45`，精确 = 综合单价 `11726.05`×Q`100`×(1+税率`9%`)，`missing:0`、`done` |
| rewind 回退 | 对上单 `rewind→params_gate` | ✅ 回到 params 闸（字段正确）、丢弃其后垃圾值、重答后正常定稿 |
| 特征澄清 + 回环（改 1 核心） | `feature="砌筑"` → feature 闸 | ✅ 真 LLM 触发，抽出 `masonry_type`/`mortar_grade`，`why` **基于召回候选**给理由；补全后 feat 追加、`clarify_rounds` 累加、回 list_match 重匹配；重匹配仍低置信→第 2 轮再问，`MAX_CLARIFY_ROUNDS=2` 截断放行 |
| 多构件（FR-P05） | `features=[C30现浇柱, 实心砖墙]` | ✅ `items:2` 逐件走闸；砖墙算出 `1172605`（与单跑一致）；柱 `null` 计入 `missing:1`、`subtotal` 只算好件、整单 `done`（坏件不拖死） |

**真跑暴露的数据层问题（非 HITL 编排 bug，归 ce-code）**：
1. **现浇柱定额映射缺口**：`010502006`（C30 现浇矩形柱）选到码但 `price_compose` 取不到定额子目 → `quota_basis` 缺 → 综合单价 `missing_base`。即 commit `c1e0a5e8` 标注的 HITL demo 已知阻塞。
2. **砂浆口径/材料缺信息价**：砖墙定额子目用「干混砌筑砂浆 M7.5」而描述是「M5 水泥砂浆」口径不齐；同名材料（铁钉/干混砂浆）在多子目重复且均无命中信息价（9 项缺价逐项停 price 闸）。

**HITL 侧无改动需求**；上述两项作为 ce-code 定额映射/信息价补全的输入。

**用法备忘（resume 各闸 decision 格式）**：confirm/review（list_coding/quota/rollup）→ `{"action":"approve"}`；input：feature→`{<fields[].key>:值}`、quantity→`{"quantity":N}`、price_item→`{"value":N}`、params→`{"measure_fee","other_fee","fee_levy","tax_rate"}`。

---

## 8. 深圳·2013 口径收窄 + 规范问答联网兜底（落地方案，待开工）

> 对应 AGENT_PROBLEM 问题 9「智能体能力有界」、AGENT_PRD（commit 52ac6008）C-02 分侧 / C-05 版本固定 / §4.0 口径归一 / FR-K07 联网兜底 / EH-05 会话粘性反问。
> 核心思想：**边界内权威作答、边界处按"答案是否唯一"决定要不要反问、边界外诚实告知或非权威降级兜底**。落地 = 在既有 norm-qa / cost-agent / `:8101` 骨架上做 6 处定向改造,不是重写。

### 8.1 现状 vs PRD 差距

| PRD 要求 | 现状（代码位置） | 差距 |
|---|---|---|
| 组价缺版本**不反问**,默认深圳2013（场景 A） | cost-agent **强制 `ask_clarification` 反问版本**（`config.yaml` cost-agent system_prompt「版本红线」） | ⚠️ **行为反转**:去掉版本反问,默认 2013 |
| 规范问答缺口径 **B1 会话粘性反问** | norm-qa **每次都反问**（`config.yaml` norm-qa system_prompt） | 改为会话内仅首次 |
| FR-K07 规范问答**联网兜底**（三道闸） | `/norm/qa` 零召回**直接拒答**（`ce-services/norm/router.py:65` `if not clauses:`） | **全新构建** |
| C-05 版本固定 2013 默认 | `spec`/`standard` 必填无默认（`norm/router.py` `NormQARequest`、`cost.py --spec`） | 默认值改 2013 |
| 口径声明 / 拒答给出路 / 降级标注 | 无 | 输出模板新增 |
| web_search 工具 | `config.yaml` 注释未启用（`# deerflow.community.ddg_search` / `# jina web_fetch`,约第 69/73 行） | 启用 |

### 8.2 六块改造方案

| 块 | 优先级 | 内容 | 文件 | 对应 PRD |
|---|---|---|---|---|
| **块1 · 默认口径=深圳2013** | 高（含行为反转） | cost-agent system_prompt 删「缺版本必反问」→「`spec` 缺省 2013、`region` 缺省深圳,**不就版本反问**」;`ask_clarification` **只留给特征缺失**;`cost.py --spec` 默认 `2013`;输出加**口径声明行**「口径:深圳·2013」（会话内首次） | `config.yaml`(cost-agent)、`skills/public/cost-agent/cost.py` | 场景A·C-05·§4.0 |
| **块2 · 规范问答联网兜底主体** | 高（核心工作量） | `norm/router.py:65` 零召回分支改为调新模块 `norm/web_fallback.py`;**三道闸**(详 §8.3)。仅 `/norm/qa` 开,`/cost/*` 不碰 | 新建 `ce-services/norm/web_fallback.py`、改 `norm/router.py`、`norm/generation.py`(加 web 降级变体) | FR-K07·C-02·C-03 |
| **块3 · B1 会话粘性反问** | 中 | norm-qa system_prompt 改:「本会话已说明地区+版本则沿用、不再问;仅会话内**首次**缺口径才 `ask_clarification` 问"哪个地区+哪个版本"」。**软实现**——靠 agent 多轮上下文(规范问答信息性、不动钱,软度可接受;要更硬再加 thread 级口径缓存) | `config.yaml`(norm-qa) | EH-05·§4.0 |
| **块4 · 输出模板** | 中 | 组价跨地域(北京)→ 体面告知「仅深圳2013,建议用 XX」(不联网);规范问答跨地域→ B1 确认后走块2;拒答统一带「已查范围 + 建议渠道」 | `cost.py`/`qa.py` 输出层 + 两 agent prompt | EH-03·C-03 拒答给出路 |
| **块5 · 启用 web 基础设施** | 中（块2 前置） | 取消注释 ddg_search + jina web_fetch（或 ce-services 直连搜索 API,见 §8.5 决策） | `config.yaml` tools 段 | FR-K07 |
| **块6 · 评测** | 低（收尾） | 路由评测集补 EH-05 / FR-K07 用例;新增指标 | `ce-services/eval/agent_routing_eval.jsonl`、`ce-code/data/eval_set/` | §8 验收 |

### 8.3 块2 联网兜底三道闸（详设）

**关键架构选择:三道闸放服务端 `ce-services`,不交给弱模型 agent** —— 同 HITL「红线不经弱模型」原则(§5/§6)。让 agent 自由 web_search = 把「查询口径约束 / 可信度筛查」交给 qwen-plus 即兴发挥 = 污染源(联网正是把 2024/他省口径捞回来的最大入口)。故 `web_fallback.py` 内确定性执行,LLM 只做带降级标注的总结。

`norm/web_fallback.py` 流程（接 `router.py:65` 零召回分支）:
1. **查询带口径约束**:口径内注入 `深圳 2013`/`GB 50500-2013`;口径外(EH-05 已确认北京/2024)按问题自身口径。
2. 调 search backend(§8.5 决策)→ 候选 URL。
3. **域名白名单分级**:住建部 / 深圳住建局·造价站 / 省标官网 > 行业站 > 博客·文库(最低层或排除)。
4. **结果筛查**:口径内筛掉 2024/他省杂质(挡污染);口径外整段打降级标注。
5. 喂 `generation`(新 web 变体)→ 强制 Tier-2 头部 `⚠️ 非本系统深圳·2013权威口径,联网检索结果,请人工核验` + URL + 访问日期溯源。
6. 仍无可信源 → **C-03 拒答给出路**(说明已查范围 + 建议渠道)。

来源分级（PRD FR-K07 表，呈现层契约）:Tier-1 本地权威(标准号+版本+条款号,直接答) / Tier-2 联网(URL+访问日期,降级标注) / Tier-3 无可信命中(拒答给出路)。

### 8.4 需拍板的决策点（开工前）

1. **搜索后端**:① DDG 社区工具(repo 已有、免费、质量一般) ② Bing/Serper API(要 key、质量好) ③ Jina(repo 已有,擅长 fetch 原文)。**倾向 DDG 召回 + Jina 取原文**(都在 repo、零额外 key)。
2. **会话粘性强度**:块3 软实现(agent 上下文,够用) vs 硬实现(thread 级口径缓存,工程量大)。**倾向先软**。
3. **块1 组价默认 2013 不反问 = 行为反转**,确认按场景 A 决定执行?(silent 默认 2013 的错版风险由口径声明显著化兜底)

### 8.5 落地顺序与任务清单

顺序:块1(配置) → 块3(prompt) → 块5(启用工具) → 块2(联网兜底主体,核心) → 块4(模板) → 块6(评测)。

- [ ] T9-1 块1:`config.yaml` cost-agent 去版本反问 + 默认深圳2013;`cost.py --spec` 默认 2013;口径声明行
- [ ] T9-2 块3:`config.yaml` norm-qa 改 B1 会话粘性反问措辞
- [ ] T9-3 块5:`config.yaml` 启用 ddg_search + jina web_fetch（按 §8.5 决策）
- [ ] T9-4 块2:新建 `norm/web_fallback.py`(三道闸)+ 改 `norm/router.py:65` 零召回分支 + `norm/generation.py` web 降级变体
- [ ] T9-5 块4:`cost.py`/`qa.py` + 两 agent prompt 输出模板(跨地域告知 / 拒答给出路 / 降级标注)
- [ ] T9-6 块6:评测集补 EH-05 / FR-K07;指标=Tier-2 降级标注覆盖率、域名白名单分级正确率、会话粘性「仅首次反问」达成率、**组价/价格联网调用=0**
- [ ] T9-7 服务器端到端验证(参照 §7 真 LLM + :8100/:8101)

### 8.6 红线（落地必守）

- **组价/价格(FR-P/FR-I)不开联网**:块1 的 cost-agent `tools` 不加 web_search;验收「组价/价格联网调用=0」。
- **联网结果一律 Tier-2 降级标注**:绝不冒充深圳2013 权威口径(块2 强制,不靠 LLM 自觉)。
- **联网兜底 ≠ 替代拒答**:联网无可信源仍走 C-03,不把「不知道」包装成「有链接的答案」。
- **三道闸在服务端确定性执行**,弱模型不碰查询口径/可信度筛查(同 HITL)。

---

## 9. 整体 Agent 骨架编排（架构基线，待落代码）

> 定位：§1–8 各自聚焦「组价 HITL 管线」与「规范问答兜底」两条具体线；本节给**统摄两者的整体 agent 骨架**——从 AGENT_PRD 的纯需求（C-01~05 红线 + 路由≥95% + §8.2 延迟 NFR + 功能需求的双峰难度）反推得到，并对模型选型（仅 Qwen3-8B / Qwen3-32B 之间）给出结论。
>
> **与 PRD 三层 Orchestrator–Pipeline–ReAct 同源**——因为那套骨架本就被红线**强制蕴含**（C-01 100%溯源 + C-03 零幻觉 + C-04 精确算钱 ⟹ 检索接地 + 确定性计算 + 校验闸 + 前置确定性路由，纯对话 agent 数学上满足不了），不是设计偏好。本节真正的**增量**只有四点：① 模型分层 8b/32b（PRD 未提模型）；② 复合推理桶上 32b（PRD 默认弱模型贯穿，此处分歧）；③ 「问题类型→规范代号」选择确定化（PRD §4.0 口径归一只管地域+版本，未管选哪部 GB——标准漂移 bug 即此缺口）；④ 校验闸显式提成独立一层（PRD 散在红线里）。

### 9.1 四层骨架

```
用户请求
  │
  ▼
① 前置路由（确定性 / 轻分类器，无 LLM）          ← 保 ≥95% 分流 + 低延迟
   · 能力分流：组价(FR-P) / 规范(FR-K) / 价格(FR-I)
   · 形态判定：单一 vs 复合(EH-01)、特征缺?(EH-04)、口径缺?(EH-05)
   · 规范映射：计量→GB50854 / 计价→GB50500 / 安装→GB50856   ← 治标准漂移
  │ 单一意图                              │ 复合意图
  ▼                                      ▼
② 能力层（工具，结构化）                    ④ 复合编排器 (32b)
   · norm_qa：RAG→受限引用生成 (8b) +FR-K07     拆解→逐子任务回①→综合
   · cost：召回→置信门控→直配/消歧(8b)/澄清       FR-X02 比选 / X03 结算 / EH-01
   · price：确定性取数      · calc：确定性算钱(C-04)
  │
  ▼
③ 校验闸（非 LLM，强制）：溯源必带(C-01) · 无命中即拒(C-03) · 口径纯净(C-02)
  │
  ▼  结构化卡（依据可见）+ 答案
```

### 9.2 各层职责 · 模型落位 · 现状

| 层 | 职责 | 模型 | 现状 | 代码 / 缺口 |
|---|---|:--:|:--:|---|
| ① 前置路由 | 能力分流 + 形态判定 + 规范映射，确定性 | 无 LLM | 🟢 已接入调度 | **T-A1**（`routing/prerouter.py`：能力分流 norm/cost/price/compound + 形态 EH-01/04/05，`/route` 端点）+ **T-A2**（`norm/standard_router.py`：规范映射，已接进 norm_qa）均落地，确定性无 LLM。**已接入实际调度**：④ 编排器 `orchestrate` 消费本层，且经 MCP 前门工具 `ce-task_orchestrate` 暴露给 lead-agent（原始请求 → 本层路由 → 派发） |
| ② 能力层 | RAG 接地生成 / 置信门控选码 / 取数 / 算钱 | **8b** | 🟢 基本就位 | 组价 HITL 图（§1–7 `cost/graph.py`）+ MCP `ce-task`(`common/mcp_server.py`) + 门控(`gates.py`) + 计算工具(`pricing.py`) + norm RAG(`norm/router.py`) |
| ③ 校验闸 | 溯源 / 拒答 / 口径纯净，结构化拦截 | 无 LLM | 🟢 已成层(norm+cost) | **T-A3 已落地（两侧）**：契约下沉 `common/guards.py`（`GuardReport`：verdict/tier/caliber_pure/provenance_complete/violations）。norm 侧 `norm/guards.py`（C-01 溯源完整 / C-02 他部+跨版剔除 / C-03 零召回拒答）；**cost 侧 `cost/guards.py::audit_cost_result`**（C-03 选不出码→reject/tier=none；C-02 定额子目跨版串库→caliber_pure=False；C-01 定额/信息价缺来源→provenance_complete=False），在 `orchestration.compose` 末步跑、挂 `meta.guard`，与 norm **同契约**。C-02 联网 Tier-2 降级标注随 §8 web 兜底并入 |
| ④ 复合编排 | 拆解(EH-01) + 综合 + 高阶推理(FR-X/FR-C) | **32b** | 🟢 已成前门 | **T-A4**（`routing/orchestrator.py`：32b 拆解→子任务回①路由→派发→32b 综合，降级安全，`/orchestrate` 端点）落地；接住 §2.2 冲突4「缺 Orchestrator」。**已暴露为四层骨架前门**：MCP 工具 `ce-task_orchestrate`（`common/mcp_server.py`），lead-agent 发原始请求即整条骨架跑通，子结果带 `meta.guard`。**待**真 LLM 质量抽检 + FR-I 价格后端 + 嵌套/依赖子任务 |

### 9.3 模型分层结论（8b / 32b）

需求在「推理难度 × 延迟预算」上**双峰**，单一模型无法同时满足：

- **桶 A（接地检索/选码/取价/生成）**：正确性靠 RAG+工具+闸兜住，与模型大小无关，真正诉求是**快**（FR-K P95≤3s、FR-P01≤2s）。32b 跑带引用生成几乎必超 3s → **桶 A 用 8b**。
- **桶 B（复合推理/判断/结算）**：FR-X02 比选「哪种更省」、FR-X03 结算/索赔、FR-C 漏项/错套、EH-01 拆解是**真·推理**，8b 给不出可信结论；低频、5s 预算宽 → **桶 B 用 32b**。

**分层落位（GPU 充足版，2026-06-30——GPU 足以并存 8b+32b，分层无成本顾虑）：**

| 槽 | 模型 | 理由 |
|---|:--:|---|
| norm_qa 受限引用生成（FR-K） | **8b** | P95≤3s 延迟敏感；引用生成 8b 已验够用（E.7.3 实测） |
| 高置信直配（FR-P01）/ 取价（FR-I）/ 澄清 | **8b** | ≤2s、高频、低推理 |
| **选码候选消歧（τ 区间，FR-P02）** | **32b** | 8b 最不可靠（现浇/预制、矩形柱召回、幻觉置信95%）且选错最贵；非 2s 直配路径、可承受 32b 延迟。**T-A5 已接线**：`select_code` 缺省桶 B（`SELECT_LLM`），成对 env 一设即升 32b |
| 复合推理/判断/结算（FR-X02/03、FR-C、EH-01 拆解） | **32b** | 真·推理，5s 预算宽 |
| ① 路由 + 规范映射 | 无 LLM | 确定性，模型选择在此无关 |

> **关键**：延迟由**单请求 per-token 速度**决定，**多 GPU 解决不了**——故 FR-K/FR-P01 生成仍钉 8b，与 GPU 数量无关；GPU 充足只是让「8b+32b 并存」零成本，并让选码消歧从 8b 升到 32b（之前因 GPU 紧张才留 8b）。
> **唯一翻盘前提**：若放弃 FR-K P95≤3s，GPU 充足下可「32b 全量」求质量齐整——不建议，FR-K 最高频，慢了天天硌用户。
> **退路**：若运维只能起一个模型 → 选 8b（守住全部红线 C-01/03/04 + 所有延迟 NFR，只让步可降级的 FR-X 高阶推理；32b-only 反而结构性违反高频路径 P95）。

### 9.4 落地顺序

①（含规范映射，直接消标准漂移、把路由从弱模型夺回）→ ④（复合桶接 32b）→ ③ 统一成层。② 已基本就位（这两天的 MCP 工具 + 依据卡 + 门控 + 拒答）。

> **进度（2026-07-01）**：四层骨架**全部接线成篇**——①（T-A1+T-A2）、②、③（T-A3 **norm+cost 两侧**）、④（T-A4，**已成前门 MCP 工具 `ce-task_orchestrate`**）、⑤（T-A5 模型分层，桶 B 32b 复合+选码消歧）。骨架已可端到端跑：lead-agent 原始请求 → `ce-task_orchestrate` → ① 确定性路由 → 单一直派②/③ 或 复合④拆解-综合，子结果带 `meta.guard`（C-01/02/03，两侧同契约）。**剩 T-A6 端到端验证**（服务器真跑全链 + 32b 选码质量/延迟分桶 + 路由≥95% + 红线回归；升 32b 靠成对 `BCRAG_ORCH_LLM_*` env）+ 前端 orchestrate 结果卡渲染（follow-up）。本地全链单测/eval 已通（standard_router 19、norm guards 9、**cost guards 7**、prerouter 15、orchestrator 8、compose meta.guard 三路径；金标 family 6/6、能力分流 17/17；T-A5 config 双向回落 + select_code 桶 B 绑定）。

### 9.5 TODO（落代码）

- [x] **T-A1 · 前置路由层（①）· 已落地（模块+端点）**：能力分流（norm/cost/price/compound）+ 形态判定，确定性关键词规则无 LLM。实现 = `routing/prerouter.py`：能力分流优先级（复合>价格>组价>项目核对>规范兜底）+ 四维信号（source_type / needs_calc C-04 / needs_context FR-C / intent_count）+ 形态（特征完整度 EH-04 仅 cost、口径完整度 §4.0）+ `clarify` 裁定（**按 §8 块1 收窄**：cost 仅特征缺反问、版本缺默认不问；norm 缺口径走 EH-05 反问）。产出 `RouteDecision`（供下游编排器消费，本层不执行）。接线：`POST /route` 端点（`routing/router.py` 挂进 `main.py`）。与 T-A2 串联——本层先定 capability=norm，再由 `standard_router` 定哪部 GB，不重复做规范映射。验证：内置自测 15/15；金标 `benchmark/routing_eval` **能力分流 17/17=100%**（clarify 与旧 expect 分歧 3 例=B1/B10 版本缺不再问 + A6 边界，§8块1 设计演进非错）。**已接入实际调度（2026-07-01）**：④ 编排器 `orchestrate` 消费本层，且经 MCP 前门工具 `ce-task_orchestrate` 暴露给 lead-agent（原始请求整条骨架跑通，见 T-A4）。代码 `ce-services/routing/{prerouter,router}.py` + `main.py` + `tools/prerouter_eval.py`
- [x] **T-A2 · 规范选择确定化（①，高优先）· 已落地**：「问题类型→规范代号」确定性映射（计量→GB50854 / 计价→GB50500 / 安装→GB50856），从 LLM 自由判断手里夺回——**直接根治标准漂移 bug**（50854→50500）。实现 = `norm/standard_router.py`（关键词规则两轴：intent 计价/计量 + discipline 房建/安装，纯函数零 LLM；降级安全：零命中回退 hint、无 hint 默认 50854；版本轴 query>hint>default 仅轻解析，版本默认策略留 §8 块1/T9-1）。接线：`/norm/qa` 端点 + `ce-task_norm_qa` MCP 工具——`standard` 改可选 hint，进检索前跑 `resolve_standard`，family 与 hint 冲突即**夺回**（`overrode_hint`，warning 日志），meta 加 `standard_resolution` 全证据。验证：内置自测 19/19（含 2 例漂移夺回 + 版本钳制 50856-2013→2024）；金标 `benchmark/routing_eval` norm-qa family 选择 6/6=100%（A6 越界跳过，归校验闸 T-A3）。代码 `ce-services/norm/standard_router.py` + `norm/router.py` + `common/mcp_server.py` + `tools/standard_router_eval.py`。**待服务器端到端验证**（真 :8100/:8101，并入 T-A6）
- [x] **T-A3 · 校验闸成层（③）· 已落地（norm-qa 侧）**：把溯源(C-01)/拒答(C-03)/口径纯净(C-02)统一为显式 guard（生成后/检索后确定性执行，不靠 LLM 自觉）。实现 = `norm/guards.py`：**C-03** 零召回拒答集中化（`reject_no_recall`，给出路 + tier=none）；**C-02** 口径纯净（`audit_answer` 逐条 cited_clause 抽 family/version，与 resolved 冲突即剔除——他部=串库、跨版=同码不同义；全剔光则降级拒答 verdict=reject）；**C-01** 溯源完整（保留条 `standard` 确定性规范化为 resolved 全码，标准号+版本恒带齐、不靠 LLM 抄对；缺条款号标 `provenance_complete=False`）。产出结构化 `GuardReport`（verdict/violations/caliber_pure/provenance_complete/tier，进 `meta.guard`）。接线 `/norm/qa` + `ce-task_norm_qa`：零召回走 reject_no_recall、生成后跑 audit_answer + warning 日志。family 抽取复用 `standard_router.family_version_of`（兼容 store 名 `GB_T50854-2024_…`）。验证：内置自测 9/9（他部剔除 / 跨版剔除→reject / 缺条款号标记不剔 / 全合规 pass）。**范围 —— norm+cost 两侧均成层（2026-07-01）**：契约下沉 `common/guards.py`（`GuardReport` 能力无关）；**cost 侧新增 `cost/guards.py::audit_cost_result`**——C-03 选不出码→verdict=reject/tier=none（转人工不杜撰）、C-02 定额子目 `spec_version` 跨版串库→caliber_pure=False、C-01 定额缺库号/信息价命中缺来源→provenance_complete=False（价格未就绪则缺口透传、不 reject——选码有价值），在 `orchestration.compose` 末步跑并挂 `meta.guard`，`router.py` merge meta 不 clobber。cost 自测 7/7 + compose meta.guard 三路径。**C-02 联网降级标注**（Tier-2）随 §8 web 兜底落地时并入此层。代码 `ce-services/common/guards.py` + `norm/guards.py` + `cost/guards.py` + `cost/orchestration.py` + `cost/router.py` + `common/mcp_server.py`
- [x] **T-A4 · 复合编排器（④）· 已落地（模块+端点）**：EH-01 拆解 → 子任务回 ① 路由 → 派发 → 综合。实现 = `routing/orchestrator.py`：`decompose`（32b 拆解，降级安全：失败/非法→单任务）+ 逐子任务 `prerouter.route` + `dispatch_subtask`（norm→`norm.pipeline`、cost→`orchestration.compose`、price→诚实标 unsupported、子任务失败标 error 不拖垮整单）+ `synthesize`（32b 综合，降级：确定性拼接 + 汇集引用不丢溯源 C-01）。`orchestrate` 单一意图直派 / 复合走拆解—综合回环。模型分层（§9.3）：拆解+综合走 **32b**（`ORCH_LLM`，config 已加最小句柄），能力执行走 **8b**（桶 A）。`dispatch_fn/decompose_fn/synthesize_fn` 可注入 → 无服务/LLM 单测。接线 `POST /orchestrate`（挂 main.py）。验证：内置自测 8/8（单一直派不拆解 / 复合拆 2 子任务各自回①路由 cost+norm / 综合汇引用 / 降级拼接保留溯源）。承 §2.2 冲突4 / §3 改3「架构归位」——**T-A1/T-A3 标注的「待接入实际调度」由本编排器承接**。**已成四层骨架前门（2026-07-01）**：新增 MCP 工具 `ce-task_orchestrate`（`common/mcp_server.py`，`instructions` 标「前门，不确定问谁/一句话多诉求时用它」），lead-agent 发原始请求即整条 ①→②/③或④ 骨架跑通；单一结果/复合子结果均带 `meta.guard`（norm+cost 同契约）。原语 `ce-task_norm_qa`/`ce-task_cost_compose` 并列保留（非 chokepoint）。`extensions_config.json` ce-task 描述加 orchestrate。**待**：真 LLM 拆解/综合质量抽检（并入 T-A6）、前端 orchestrate 结果卡渲染（`ce-tool-result.tsx` 加 mode=single/compound 分支）、lead-agent 工具 allow-list 确认（服务器验）、FR-I 价格取数后端接入、嵌套复合/子任务依赖（当前独立子任务）。代码 `ce-services/routing/orchestrator.py` + `routing/router.py` + `common/mcp_server.py` + `main.py` + `common/config.py`(ORCH_LLM)
- [x] **T-A5 · 模型分层接线 · 已落地（2026-07-01，本地验证）**：桶 A(② norm_qa生成/直配/取价/澄清) 走 8b、桶 B(④复合拆解/综合 + **选码消歧**) 走 32b（落位表见 §9.3）。落地 = **单点接线在 `common/config.py`**：桶 B 定义一次（`ORCH_LLM_*` 复合编排 + 新增 `SELECT_LLM_*` 选码消歧，默认同端点），**URL 与 model 成对回落 8b**（修掉旧 `ORCH_LLM` 的「8b 端点 + 32b model id」不匹配隐患）——仅当显式设 `BCRAG_ORCH_LLM_URL` 才升 32b，故本地/未部署一切照旧走 8b、**零风险**。选码路由到桶 B 走**唯一 chokepoint**：`selection.select_code` 缺省 = `SELECT_LLM`（overridable 供 benchmark 比 8b/32b），并从 `provenance.list_match` / `orchestration.compose` **剥掉透传 8b 的死参**（二者唯一 LLM 步就是选码）；`compose` 四调用点（router/mcp/orchestrator/graph）同步去参。取价/澄清（`clarify.extract_missing_features`）/norm 生成仍显式走 8b。`config.yaml` 注册 qwen3-32b-awq 为**可用非默认**模型（8b 仍列表首=agent 默认；deer-flow agent 保持 8b，桶 B 消费方是 ce-services 内部、经 env 指端点不读 config.yaml）。**服务器升 32b**：成对 `export BCRAG_ORCH_LLM_URL=http://172.19.2.2:8001 BCRAG_ORCH_LLM_MODEL_ID=/models/Qwen3-32B-AWQ`（可再单设 `BCRAG_SELECT_LLM_*` 指另一实例）。验证：config 成对回落/升级两向、`select_code` 默认绑桶 B、`list_match`/`compose` 签名去参、orchestrator 8/8 + prerouter 15/15 自测全过。代码 `ce-services/common/config.py` + `cost/{selection,provenance,orchestration,graph,router}.py` + `common/mcp_server.py` + `routing/orchestrator.py` + `config.yaml`。**GPU 分配**：32b 独占 172.19.2.2，reranker 占 cuda:2（ce-code 经验），互不挤。**待 T-A6 服务器真跑**验 32b 选码质量/延迟
- [ ] **T-A6 · 端到端验证**：路由正确率 ≥95%（脏请求 EH-01~05，复用 `benchmark/routing_eval/` 金标）+ 延迟分桶 P95（FR-K≤3s/FR-P01≤2s/复合≤5s）+ 复合桶质量抽检；红线回归（检索层算数=0、组价/价格联网=0）
  - **部分已验（2026-07-01，:8101 真跑 + 32b env）**：① 前门 `/orchestrate` 单一意图直派通（`mode:single`、`route.capability:cost` 确定性正确）；③ cost 侧 `meta.guard` 契约真数据齐（选不出码→reject/tier=none/C-03、缺定额→C-01 warn、跨版→C-02）；MCP 加载 6 工具（ce-task 3 含 orchestrate）。真跑修 1 bug（need_review 带 code 仍 reject，commit a974a368）。
  - **🔴 发现·复合路由精度 gap（并入本 T-A6 调优，非骨架 bug）**：`prerouter` compound 检测目前**仅靠 `COMPOUND_KW` 关键词**——「哪种**做法**更省」因中缀「做法」漏配「哪种更省」；且 cost∧norm 同时命中不判 compound。致「套码+按什么计量+哪种更省」误判 `single/cost`、④拆解未触发。**两档修法**：轻=补 `COMPOUND_KW` 柔性变体；稳=加「多能力命中→compound」启发式（需 `benchmark/routing_eval` 金标回归防误触）。演示 ④ 可先用命中关键词的输入（如含「哪种更省」精确串）。
  - **agent 入口闭环（2026-07-01，服务器验通 + 呈现层修复）**：
    - **闭环验通**：前端默认 **lead agent** 直接调 `ce-task_orchestrate` 跑通——复合输入「现浇柱套码 + 和预制柱哪种更省」→ `mode:compound`，拆 s1(cost，code=null/reject/C-03) + s2(norm，引 GB50854 E.7.3/E.3.1/E.7.9，guard pass)，32b 综合。**后端骨架 + 校验闸真数据全绿、引用带真溯源。**
    - **🔴 呈现层杜撰（真事故）**：默认 lead agent 拿到干净 result 后，在 prose 里**自行补了前门根本没返回的编码 `010503001`/`010504001` + 条文 `E.4.1`**——§9 把红线挡在确定性后端，却漏在**弱模型转述这一步**。根因：lead prompt 虽已有「不编造」红线，弱模型仍无视（印证「红线不能靠 prompt 靠自觉」）。
    - **修（呈现层收敛，`[backend]`）**：改 `lead_agent/prompt.py SYSTEM_PROMPT_TEMPLATE`——① `<routing>`/`<skill_runbook>` 从「弱模型分 norm/cost/both + 调脚本」改为「造价领域原话整条交 `ce-task_orchestrate` 前门」（确定性路由夺回弱模型手里）；② `<safety_redline>` 硬化「只逐字转述前门 `answer`/`cited_clauses`，前门没返回的编码/条文/价格一个字都不能写」（点名 010504001/E.4.1 事故）。**须重启网关**（改的是内置模板常量，非 system_prompt_path 热加载）。
    - **前门 agent `ce-router`** 仍在（`config.yaml`，严格 prompt 模板），但**前端 gallery 选不到**（gallery=per-user dir，config.yaml subagents 是委派子代理）——故实际闭环走的是**默认 lead agent + 改后 prompt**，非 ce-router。
    - **durable 修法（已做，2026-07-01）**：prompt 收敛≠根治（弱模型仍可能加料），故**前端渲染 orchestrate 结构化结果卡**——`ce-tool-result.tsx` 加 `orchestrate` 分支（single/compound）：直接摆出路由落点 + 各能力子结果的选码/条文 + **校验闸 guard 徽标**（verdict=reject/tier=none/口径不纯/溯源不全），让用户看到 ground truth（code=null / 真 cited_clauses），不必信弱模型那段可能加料的 prose。`GuardBadges`/`SubtaskEnvelope`/`ClauseList`(抽复用) 就位；`isCeTool` 已含 `ce-task_` 故自动委派、无需改 message-group。
  - **待**：改后 lead prompt 服务器重启验（造价请求自动走前门 + 不再补编码/条文）；前端 `pnpm check`（本地无 node_modules）+ orchestrate 卡肉眼验；④ 复合真跑质量抽检。
