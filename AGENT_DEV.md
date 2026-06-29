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

1. **PRD 的 HITL 本质是「ReAct 特征澄清循环」，不是固定全流程闸机。** §8.2 明确「澄清结果回填后**重走 §4.4 门控**」——这是 clarify→回填→重新门控的**回环**；当前图是无回边 DAG、单向不可回退，**恰好相反**。
2. **PRD 头号澄清场景——特征澄清——当前图没实现。** `setup_node` 只补 `spec/region`，拿到 `feature` 字符串直接 `list_match`。而 FR-P02/EH-04（「只写'砌筑'，缺砌块/砂浆强度」）要求**先反问补构件关键特征槽再匹配**，当前缺这个闸，等于跳过 PRD 最核心 HITL 用例。
3. **缺「工程量 Q 录入闸」，当前静默按 Q=1 算出错误总价。** Q 由**用户录入**（非 BIM 自动算——PRD §1.2 范围外的是 Q 的几何/扣减**计算**，用户手填一个已知 Q 是正常 HITL 输入，不越界）。但当前图**没有任何闸收 Q**：`_unit_price_for` 构造 `UnitPriceInput` 时不传 `quantity`，而 `pricing.py:80` `quantity` 默认 1.0，故 `total_price = 综合单价 × 1`。`_compute_rollup` 把一堆「Q=1 的综合单价」当分部分项合价相加——**不报错、直接定稿一个名不副实的总造价**。这是「静默用错误默认值」，比报错更危险。
   - 注：综合单价/总造价段本身**不违 C-04**——`compute_unit_price`/`rollup_cost` 是确定性「计算工具能力」，HITL 只负责收 Q/rates/params 再喂给它。问题不在「该不该算总造价」，而在「缺一个把 Q 喂进去的闸」。
4. **缺 Orchestrator/意图路由层。** PRD 骨架是 Orchestrator 按 §4.3 在「两 Agent + 两能力」间路由；当前 HITL 是单体组价管线，无意图分类、无规范问答介入、无复合拆解（EH-01）。它更像「组价 happy-path 脚手架」，不是 PRD 描述的 agent 系统。

---

## 3. 改造方案（保闸粒度，改闸机形态）

不是推倒重来，而是「逐闸 stop-and-go 的粒度留下、固定全流程 DAG 的形态要改」。

| 改动 | 优先级 | 内容 | 解决的冲突 |
|---|---|---|---|
| 改 1 · 特征澄清闸 | 必须 | 在 `list_match` 之前加「特征槽检查闸」：缺关键特征槽（构件类型/强度等级/断面…）就停、回填后**重跑门控** | §2.2 之 1+2（不可回退 & 头号用例缺失一并解决） |
| 改 2 · 补 Q 录入闸 | 高 · **已落地** | 在 `price_gate` 之后、`rates_gate` 之前新增 `quantity_gate` input 闸收工程量 Q，透传 `_unit_price_for(..., quantity=Q)` → `compute_unit_price`，修掉「静默 Q=1 出错误总价」。无基价时跳闸、有基价缺 Q 标 `missing_quantity`/blocked、不静默按 1 计。可经 `SessionStartRequest.quantity` 预供则自动过。总造价段保留（合规、不违 C-04）。代码见 `cost/{graph,gates,session,router,state}.py` | §2.2 之 3 |
| 改 3 · 架构归位 | 中 | 这条线性图应是 Orchestrator 路由后**组价 Agent 内部**的一种 pipeline 形态，意图路由（§4.3）置于其上 | §2.2 之 4 |

### 3.1 回退能力（与改 1 配套，二选一）

- **轻量**：前端发现要改前面时 `start` 新会话重来（最简，丢已填数据）。
- **正经回退**：`session.py` 加 `rewind(task_id, to_gate)`，用 langgraph `update_state` 回到目标闸 checkpoint 再 `resume`——保留之前输入、只重跑被改节点及下游。配合改 1 的「回填重走门控」语义一致，推荐此条。

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
- 图链路：`setup → list_match → list_gate →(有码?)→ compose → quota_gate → price_gate → quantity_gate → rates_gate → params_gate → rollup → done`。

---

## 5. 决策依据透明化（前端展示 · 已落地）

「为什么问你 / 为什么没问你」都进前端**依据时间线**（`CostHitlPanel` 的 `EventTimeline`），数据同源于 provenance 信封：

- **停闸的依据**：confirm 闸 payload 自带 `evidence{source_type, source_ref, confidence}` + `alternatives`；input 闸（含改 1 的特征澄清）用 `context` 字段承载「缺什么特征 / 为什么问」。
- **自动过（高置信）的依据**：自动过不打断，但每个门控节点经 `state.gate_event(...)` 往 `events` 写一条决策事件——`auto_pass=true`，编码闸再带 `confidence`/`tau`，前端渲染「自动采纳」徽标 + 「置信 X ≥ 阈值 τ，故未打断」。
- **覆盖的门控**：`list_gate`（带 τ）/ `quota_gate` / `quantity_gate` / `params_gate` / `rates_gate` 全部发 `gate_event`，所有闸的「停/过」决策在时间线可见、可审计（呼应 §8.2 可观测）。
- 代码：后端 `cost/state.py::gate_event` + `cost/graph.py` 各门控节点；前端 `core/cost/types.ts::CostEvent`（加 `auto_pass/tau/confidence`）+ `cost-hitl-panel.tsx::EventTimeline`。
