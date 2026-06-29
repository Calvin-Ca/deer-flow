# 智能体开发文档

> 文档定位：从 `AGENT_PRD.md`（需求/路由权威）落到实现态的开发记录。本篇聚焦 **HITL（人介入）方案**——现状盘点、与 PRD 的契合/冲突评估、改造方案。
> 关联：需求见 `AGENT_PRD.md`；HITL 图实现见 `ce-services/cost/`（`graph.py` / `session.py` / `gates.py`）；HITL 设计细节见 `ce-services/HITL_DESIGN.md`。

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
