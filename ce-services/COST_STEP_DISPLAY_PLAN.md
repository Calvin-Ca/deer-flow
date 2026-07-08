# 组价中间步骤前端可视化 · 实施方案

> 目标：用户输入「实体名称 + 做法特征」触发组价后，**清单匹配 → 套定额 → 取价 → 单位工程汇总 → 单项工程汇总 → 总造价**
> 各步骤在前端逐步显示。本文是分阶段落地方案，对齐现有代码与红线，供 Mac↔服务器对照。
>
> 相关文档：编排/分层原则 `HITL_DESIGN.md`、任务层需求 `PRD.md`、算钱原语 `cost/pricing.py`、图 `cost/graph.py`。
> 组价步骤可视化设计记录；当前服务边界以 `INTERFACE_CONTRACTS.md` 为准。

---

## 0. 现状事实（方案据此设计，勿重复排查）

1. **图是单层汇总**：`cost/graph.py` 的 `_compute_rollup`（graph.py:499）只做 `items[] → Σ total_price → rollup_cost`
   一层「分部分项 → 总造价」，**没有单位工程/单项工程分组**。两级汇总需加分组维度 + 层级汇总原语。
2. **当前是 `.invoke()` 批量返回**：`session.start/resume`（`cost/session.py`）跑到下个闸才一次性返回，
   `events`（`operator.add` 累积，`cost/state.py:55`）虽全产出但**一批到达**。逐节点流式需加 SSE 端点 + `.stream()`。
3. **代理层会缓冲**：`frontend/src/app/ce-cost/[...path]/route.ts:35` 用 `await response.arrayBuffer()` 整体缓冲——
   **流式必须先改这里为 body 透传**，否则 SSE 到不了浏览器。
4. **前端已有 HITL 内嵌卡但不渲染 events**：`cost-hitl-inline.tsx` 的 `Snapshot`（cost-hitl-inline.tsx:20）
   没接 `events` 字段，widget 只画「当前闸 + 最终 DoneSummary」，**从不画步骤时间线**。`CostEvent` 类型
   已定义（`core/cost/types.ts:21`），数据管道全通，只差渲染。

**结论**：数据/编排/持久化已就位，缺的是①前端时间线渲染 ②秒级流式 ③两级汇总。按「先见效、后加料」分四阶段。

---

## 1. 阶段 0 · 前端步骤时间线（纯前端，零后端改动，最快见效）

现有 `events` 已全产出，先把「批量到达」的时间线画出来——每到一个闸，时间线向前跳一批。

| 文件 | 改动 |
|---|---|
| `frontend/src/components/workspace/cost/step-timeline.tsx` | **新增** `CostStepTimeline({events})`：竖向步骤列表，`STEP_LABELS` 中文映射 + 状态徽章 + 复用 `EvidenceCard` 依据卡 + 自动采纳/待确认标记 + 「置信 X ≥ τ」 |
| `frontend/src/components/workspace/cost/cost-hitl-inline.tsx` | `Snapshot` 加 `events`；`getSessionState`(读 `v.events`)、`resumeSession`(读 `res.events`) 两处填入；当前闸卡片上方渲染 `<CostStepTimeline>` |

**STEP_LABELS 映射（后端 step → UI）**：

| 后端 step | UI 标签 |
|---|---|
| `caliber` | 口径声明 |
| `list_match` / `list_gate` | 清单匹配 / 清单编码确认 |
| `from_price_compose` / `quota_gate` | 套定额取数 / 定额确认 |
| `price_item:*` / price auto_pass | 取价（信息价） |
| `compute_unit_price[*]` | 综合单价 |
| `quantity_gate` | 工程量 |
| `params_gate` | 措施/规费/税金 |
| `unit_rollup:*` / `single_rollup:*` | 单位工程汇总 / 单项工程汇总（阶段 2 加） |
| `rollup` | 造价汇总（总造价） |
| `no_pricing` | 未计价（缺定额，未虚构） |

红线：只从结构化 `events` 渲染，不解析模型自然语言（HITL_DESIGN §8）。

**验收**：起一次 HITL 会话，逐闸 resume，时间线随之逐批展开；done 后时间线 + DoneSummary 并存；重开对话（`getSessionState`）时间线可从持久化 `events` 重建。

---

## 2. 阶段 1 · SSE 逐节点流式（后端 + 代理 + 前端）

把「每到闸批量跳」升级为「每个节点跑完秒级点亮」。

| 文件 | 改动 |
|---|---|
| `ce-services/cost/session.py` | **新增** `stream_start()` / `stream_resume()` 生成器：`_graph.stream(input, config, stream_mode="updates")`，每节点产出即 `yield` 其 `events` 增量；命中 `__interrupt__` → yield 闸 payload 收尾；done/blocked → yield 终态 |
| `ce-services/cost/router.py` | **新增** `POST /cost/session/start/stream` + `/{task_id}/resume/stream`，`StreamingResponse(media_type="text/event-stream")`，逐条 `data: {json}\n\n`。**旧 `invoke` 端点保留**（skill/兜底/`get_state` 仍用） |
| `frontend/src/app/ce-cost/[...path]/route.ts` | 缓冲改**流式透传**：`new Response(response.body, {status, headers})`，保留 `text/event-stream` 头（对 JSON 端点无害） |
| `frontend/src/core/cost/client.ts` | **新增** `streamStart()` / `streamResume()`：`fetch` + `response.body.getReader()` 解析 SSE（EventSource 不支持 POST，用 fetch-stream），回调逐条 event |
| `cost-hitl-inline.tsx` | `decide` 改走流式：事件到一条 `append` 一条到时间线，`interrupt`/`done` 到达再定闸/终态 |

要点：langgraph `.stream()` + SqliteSaver + interrupt/resume 原生支持；节点间有 checkpoint，**resume 不重跑 LLM**（原则 3 不破）。

**验收**：curl SSE 端点见逐条 `data:`；前端「清单匹配→套定额→取价」逐条点亮而非批量跳。

---

## 3. 阶段 2 · 两级汇总（单位工程 / 单项工程）

造价层级：`单项工程 > 单位工程 > 分部分项 > 清单项`。给 item 加分组标签，汇总按标签聚合。

| 文件 | 改动 |
|---|---|
| `ce-services/cost/state.py` | item 加可选 `unit_work`(单位工程名) / `single_work`(单项工程名)；缺省归一到「默认单位工程/默认单项工程」→ 单组树，**向后兼容单构件** |
| `ce-services/cost/router.py` + `session.py` | `SessionStartRequest.features` 从 `list[str]` 扩为兼容 `list[{feature, unit_work?, single_work?}]`；起会话写入 item 分组标签 |
| `ce-services/cost/pricing.py` | **新增** `rollup_hierarchy()` 原语：按 `(single_work, unit_work)` 聚合各 item `total_price` → 单位工程分部分项合价 → 单项工程合价 → Σ 复用现有 `rollup_cost` 出总造价。Decimal 量化到分、pydantic 闸门、`missing_unit_price_items` 逐层计数（不虚构） |
| `ce-services/cost/graph.py` | `_compute_rollup` 改用 `rollup_hierarchy`；`rollup_node` **逐层发 events**：每单位工程一条 `unit_rollup:{name}`、每单项工程一条 `single_rollup:{name}`、末条 `rollup` 总造价 → 时间线自然展开「单位工程汇总→单项工程汇总→总造价」 |
| `ce-services/cost/graph.py` · `price_gate_node` | **补 `取价` event**（时间线完整性）：现 `price_gate_node`（graph.py:307）命中信息价自动过时**不发 event**，仅缺价录入才有 `price_item:*`，导致时间线看不到「取价」这步。补一条 auto_pass 事件（`step:"price"`，`detail:{materials:n, priced:n, missing:m}`），命中/缺价都进依据时间线 |
| `frontend/src/core/cost/types.ts` | rollup/review 响应加 `hierarchy` 结构；`STEP_LABELS` 加 `unit_rollup`/`single_rollup` |
| `frontend/.../gates.tsx` + `cost-hitl-inline.tsx` | `ReviewGate` / `DoneSummary` 渲染层级树（单项工程 › 单位工程 › 分部分项合价，可折叠），总造价置顶 |

**hierarchy 数据契约（草案）**：

```jsonc
{
  "single_works": [
    { "name": "单项工程1",
      "unit_works": [
        { "name": "单位工程1-1", "subtotal": 12345.67, "item_count": 3, "missing_unit_price_items": 0 }
      ],
      "subtotal": 12345.67 }
  ],
  "subtotal": 12345.67,              // = Σ 单项工程，等于旧 flat subtotal（向后兼容）
  "measure_fee": 0, "other_fee": 0, "fee_levy": 0,
  "pre_tax_total": 0, "tax": 0, "total": 0,   // 顶层沿用 rollup_cost 口径
  "missing_unit_price_items": 0
}
```

**v1 范围诚实标注（写入 DEV 决策，不静默）**：措施费/规费本应挂单位工程级，v1 先保持**项目级**（现 `params_gate` 不动），
两级汇总只对「分部分项合价」做树状聚合，顶层加项目级费用 + 税金。单位工程级费用录入留作 v2。

**验收**：单组退化 = 旧 rollup 数值一致；多组层级 Σ 正确；缺价逐层 `missing` 计数不虚构总价。

---

## 4. 阶段 3（可选）· 点火型 MCP 工具

| 文件 | 改动 |
|---|---|
| `ce-services/common/mcp_server.py` | **新增** `@mcp.tool() start_cost_session_tool(feature, spec, region, features?)`：调 `session.start` 起会话，返回 `{task_id, status, interrupt, ui_hint}`。agent 只转述“会话已启动”这类短句，前端据 `task_id` + `interrupt` 出卡。**只点火不编排**（编排留图里，红线） |

`stateless_http` 无碍——状态在 SqliteSaver 按 `task_id` 持久化，工具只负责点火。

---

## 5. 红线自检（全程不破）

- 编排在 langgraph 图，LLM 只在选码/标准化判断节点用（HITL_DESIGN §1.2/§10）。
- 算钱在 `pricing.py` 确定性原语 + pydantic 闸门，费率/税率由用户录入不杜撰。
- 缺价 `no_source` / 选不出码 `need_review` / 缺定额 `no_pricing` 如实透传；两级汇总逐层 `missing` 计数不虚构总价。
- 前端只渲染结构化 `events`/payload，不解析模型散文。

## 6. 阶段顺序与依赖

阶段 0（前端，独立）→ 阶段 1（流式，依赖代理透传）→ 阶段 2（两级汇总，后端+前端，独立于 1，但时间线复用 0/1）→ 阶段 3（可选）。
**建议先做阶段 0**：纯前端、零风险、立刻可见步骤显示。

阶段 0/1/2/3 已全部落码并在 Docker 生产栈 e2e 验证通过（2026-07-04：卡片渲染 + 11 步时间线 +
两级层级树 + 算到真总价 45314.8；前门 Option B 确定性点火 HITL）。当前交互模型 = **点火型解耦**
（agent 点火即返回、卡片旁路驱动、agent 不在环）。下节 B2 是**可选**的「Claude 原生停-答-续」升级。

---

## 7. 附录 B2 · Claude 原生「停-答-续」HITL（可选深修，未排期）

> 背景与取舍详见对话 2026-07-04：当前「点火型解耦」下，agent 点完火就产出一段文字（易多嘴、
> 位置在卡下、说反方向），且 agent 不在环、不知道 HITL 何时完成。B2 让 **agent 回合真正暂停在
> 每个闸、人答完再续、done 后由 agent 收尾**——体验对齐 Claude 的权限/计划审批式 HITL。

### 7.1 为什么「阻塞工具等 done」不行（死锁）

让 `orchestrate_tool` 起会话后不返回、原地等 done 会死锁：工具不返回 → 前端拿不到 `task_id` → 卡片不渲染
→ 用户无处可点 → 闸推进不了 → 会话永停首闸 → 工具永远等不到 done。**「卡片要 task_id 才能渲染」与
「工具阻塞等完」互斥**，故工具必须立刻返回（现状解耦即由此逼出）。B2 不走阻塞，走**节点内嵌套 interrupt**。

### 7.2 B1（坏）vs B2（好）—— 关键区分

- **B1**：LLM 每闸进环（读闸→转述→收决策→resume）。🔴 撞 HITL 设计的逐闸确定性边界
  弱模型转述事故。**不做。**
- **B2**：deer-flow agent 图里加**一个确定性节点**，用 langgraph **节点内多次 `interrupt()`** 托管闸循环。
  resume 一个被 interrupt 的节点是**从中断点继续该节点、不回 LLM 节点**，故闸1→闸2 之间 **LLM 不被重调**。
  **LLM 全程只调 2 次**：开头「判要完整组价 → 调 bridge 节点」+ 结尾「拿真 rollup 总结一句」。**不撞红线。**

### 7.3 改造清单（按层）

| 层 | 改动 | 量 |
|---|---|---|
| **deer-flow 后端核心**（`backend/`，gateway langgraph）🔴 | 新增自定义节点 `cost_hitl_bridge` 挂进 lead agent 图：起 `/cost/session/start` → `interrupt(闸payload)` 挂起回合 → resume 拿决策 → `/cost/session/{id}/resume` → 下一闸 → 再 interrupt，循环至 done → 返回 rollup。需对齐两个 langgraph（deer-flow agent 图 + ce-services cost 图）的 interrupt/resume 语义 + checkpointer 持久化中间态 | **中-大**（主要不确定性）|
| **deer-flow 前端核心** | interrupt UI（现渲染通用澄清框）改渲染**富 cost 闸**：把现有 `gates.tsx`(ConfirmGate/InputGate/ReviewGate) + `step-timeline.tsx` + 层级树接进 deer-flow 的 interrupt 渲染路径；resume 提交带 cost 决策格式（confirm action / input dict）。**组件可复用**，数据源从「tool result + getSessionState」换成「interrupt payload」 | **中** |
| **ce-services（任务层）** | 基本不改（session API 已就绪）；`resume` 已返回下一闸完整 payload | **小/零** |
| **协议 + lead prompt** | 定义 deer-flow interrupt ↔ cost gate payload 映射契约；lead prompt 约定「起=调 bridge、结尾=拿 rollup 总结不编」 | **小** |

### 7.4 🚩 前置调研（决定 B2 可行性，先做）

**deer-flow 的 agent 图是否支持「自定义节点 + 节点内多次 `interrupt()`」？** 看 `backend/` 的 lead agent
图定义 + `ask_clarification` 的 interrupt 实现。
- 若支持节点内循环 interrupt → B2 干净形态成立，按 7.3 推进。
- 若只支持「工具触发单次 interrupt」→ B2 干净形态打折，退化 B2-lite 或需更大改造（重估）。
**此调研是 B2 排期前提，约 0.5–1 人日。**

#### 调研结论（2026-07-04，已看代码）

**核心答案：7.3 的「加自定义节点 `cost_hitl_bridge` + 节点内多次 `interrupt()`」形态在 deer-flow 现架构下不成立。** 三条已验证事实：

1. **lead agent 不是手写 `StateGraph`，是 `langchain.agents.create_agent` 预制环**（`backend/…/agents/factory.py:139`，model↔tools ReAct 循环）。**没有一张图能"插节点"**——`create_deerflow_agent` 的唯一扩展面是 14 段 **middleware 链**（`_assemble_from_features`），钩子只有 `before_model / after_model / wrap_model_call / wrap_tool_call`。7.3 里「把 bridge 节点挂进 lead agent 图」这个动作在架构上没有落点。
2. **全库无 native `interrupt()`**（grep `\binterrupt(` = 0 命中）。现有 HITL（`ask_clarification`）**不用** langgraph 的 checkpoint 挂起：`ClarificationMiddleware.wrap_tool_call` 命中即返回 `Command(update={messages:[toolmsg]}, goto=END)`——**直接把这一 run 送到 END 结束**。所谓"续"= 前端下一轮提交一条新 HumanMessage 起新 run（`clarification_middleware.py:153`）。这与 B2 设想的「同一 agent 回合挂起在闸、resume 从中断点续」是**两种模型**。
3. **resume-via-`Command(resume=…)` 未接通**：gateway `RunCreateRequest` 虽声明了 `command`/`checkpoint_id`/`checkpoint`（LangGraph Platform 兼容 schema，`thread_runs.py:39/44-45`），但 `services.py:338 start_run` 构造入参时**只读 `body.input`**（`graph_input = normalize_input(body.input)`），**`body.command` 全程被忽略**。即便 checkpointer 已挂载（worker 有快照/回滚逻辑），也**没有把 `Command(resume=…)` 喂回图的通路**。native interrupt/resume 是"schema 有、链路无"。

**据此对 7.4 分支判定：落在第二分支「只支持单次 interrupt / 需更大改造」。** 现架构原生 HITL 能力 = 「`Command(goto=END)` 结束回合 + 下一轮新 HumanMessage」，本质就是**现状点火型/ B2-lite 的模型**——无法在不接通新链路的前提下把「一个 agent 回合"暂停"着跨多个闸」。

**若仍要 B2 干净形态（停在每闸、agent 在环），最小诚实路径（较 7.3 重估上移）：**
- **bridge 落成"工具"而非"节点"**（挂在 create_agent 的 tools 节点里），工具体内调 langgraph `interrupt()`。langgraph 支持单节点多次 interrupt，但**每次 resume 会从节点顶重放**——工具体每过一闸就整体重跑一遍，故对 `/cost/session/*` 的 HTTP 调用**必须幂等/带进度守卫**，否则重复起会话/重复推进。这正是 7.5「两 checkpointer 一致性」风险的具体化。
- **接通 interrupt/resume 全链路**（backend 核心改动，`[backend]` + 上游回流对账）：gateway `body.command → graph_input`、worker 支持以 `Command(resume=…)` 驱动 astream、前端 interrupt UI 提交 resume 命令。**工作量从 7.3 的「加一个节点」上移为「接通全链路 + 工具重放安全」**，7.5 三条风险（动核心 / 长挂起 / 双 checkpointer 一致性）全部坐实。

**修订建议**：B2 干净形态**打折确认**，不建议现在启动。**优先 7.6 B2-lite**（done 后前端自动发消息给 agent 收尾，不动核心，量小）或维持现状 D。仅当「agent 必须在场、停在每闸」被确认为硬需求，才按上面"最小诚实路径"重新排期（量级 ≈ 中-大，动 backend 核心）。前端组件（gates/timeline/层级树）无论走哪条都可复用，非瓶颈。

### 7.5 工作量与风险

- **粗估**：前置调研 0.5–1 人日；若 deer-flow 图易扩展，bridge 节点 + 前端接线 **2–4 人日**；若要动 lead
  图核心 / 涉上游回流对账，更多。**相对现状 D 大一个量级。**
- **风险**：① 动 `backend/` 上游 harness 核心（`[backend]` 标注 + 回流对账）；② agent 回合长时间挂起
  （整个 HITL 时长）——checkpointer 支持但需验超时/恢复；③ 两 checkpointer（deer-flow + cost）状态一致性；
  ④ 弱模型在 start/summarize 两端仍可能加料（summarize 有真 rollup、风险低）。

### 7.6 中间档 B2-lite（回调，不动核心）

只要「done 后 agent 收尾」而非「停在每个闸」：保持现点火 + 卡片旁路，卡片 **done 时前端自动发一条消息**
给 agent（「组价完成，总造价 X，请收尾」）→ agent 总结。**不动 deer-flow 核心**（仅前端一处），但**不给
「停在每个闸」**、开头点火文字仍在。量：**小**。

### 7.7 决策口径

先把现状 D 抛光（note 已改 + 可选「引导在上/卡在下」布局）跑顺——**大概率够用**。仅当「agent 必须在场、
停在每个闸」被确认为刚需，才启动 B2（先做 7.4 前置调研再排期）。**B2 未排期，本节为存档。**

---

## 8. 最终落地决策（2026-07-04 综合结论）

> 经 §7 一系列调研（B2 可行性 / 节点重放 × 双 checkpointer 一致性 / subagent / 训练模型 / MCP 粒度）后的定案。
> **总纲：交互维持"点火型解耦"为主干，把智能投在"训练模型 + 置信门控自动过闸"，不投在"全 B2 连续暂停回合"；
> 全 B2（方案 A）仅存档，待"agent 必须在环、停在每闸"被确认为硬需求再启动。**

### 8.1 六条决策

| # | 决策 | 状态 | 依据 |
|---|---|---|---|
| **1 交互模型** | 维持**点火型解耦**：agent 调 `start_cost_session_tool` 点火即返回结构化会话信封，逐闸走 REST `/cost/session/*` 由前端卡片驱动，agent 不在环 | ✅ 已实现 | 全系统**单 checkpointer**，图内 compute/gate 双拆已解重放安全 → 零跨状态机一致性风险 |
| **2 agent 收尾** | **B2-lite**：卡片 `done` 时前端自动发「组价完成，总造价 X，请收尾」（`hide_from_ui`）→ agent 总结一句。只动前端一处，不碰 backend 核心 | ✅ 本次落地 | 拿"收尾"体验不付全 B2 代价；summarize 有真 rollup、风险低 |
| **3 步骤可视化** | 卡片 + 时间线 + 两级层级树，只从结构化 `events` 渲染 | ✅ 已实现（阶段0/1/2） | 显示由"图 emit events"决定，与"是否 MCP"无关 |
| **4 训练模型套定额** | 落成图内**一个 compute 节点外呼**（同 `cost_match_bill_item_tool` / `cost_price_compose_envelope_tool`），**置信门控**（复用混合路由）：高置信自动过、低置信升人闸；算一次写进 session 持久化，gate 只确认 | 🔒 模型未训；**接口已预留** | 贵+非确定性副作用必须留图内 compute 节点、不进重放区；它减少人闸→削弱全 B2 的 ROI |
| **5 红线守卫** | 套定额模型只在真实候选内选、`消耗量/费率`走确定性库查表；费率/税率必须人录入；缺价 `no_source`、选不出码 `need_review` 如实透传；自动过的闸必留依据卡；**改已完成组价的参数不在对话里重算**（导回卡片重开闸/重起会话） | ✅ 本次补齐 | `HITL_DESIGN §1.2/§10`；类型 B 杜撰重算护栏见 cost-agent skill 红线 7 |
| **6 MCP 粒度** | 维持不拆：ce-rag/ce-db 取数原语 + ce-task（orchestrate_tool/norm_qa_tool/cost_compose_tool/start_cost_session_tool）。HITL 逐闸**不暴露成 MCP** | ✅ 已实现 | 拆 HITL 步骤=逼弱模型当编排器（红线）+ 无状态失配 + 一致性倒退 |

### 8.2 本次落地内容（已实现，代码级）

- **cost-agent skill 红线 7**：组价 `done` 后改费率/税率/定额/工程量再算，**禁止用回复文本数字自行重算造价**，导回卡片重开闸或按修正参数重起会话（重算走服务端确定性图）。
- **B2-lite 前端收尾**（决策 2）：`cost-hitl-inline.tsx` 在 resume 流 `done` 分支 emit `cost-hitl-events`；`useThreadStream` 订阅后自动向 thread 发一条 `hide_from_ui` 触发消息 → agent 收尾。按 taskId 去重、只发一次；**只在本次交互真实完成时触发，重开会话不触发**。
- **纯键查任务层薄能力**（Backlog 2）：`ce-task` MCP 新增 `quota_lookup_tool`（已知清单码直查套定额取数）+ `price_lookup_tool`（材料/人工/机械名直查信息价），均纯键查、带 spec 归一 + 口径声明；**docstring 钉红线：从描述选码必走 `cost_compose_tool`，本工具不选码**。底层复用 `cost_client.price_compose`/`price_query`。
- **re-rollup 确定性重算**（Backlog 3）：`graph.recompute_rollup`（纯函数：套修正费率/项目费用，复用 `_unit_price_for`/`rollup_hierarchy` 确定性重算，不碰图/会话/LLM）+ `session.re_rollup`（读持久化累积态调之）+ `POST /cost/session/{id}/re_rollup`。兜住类型 B 的**正确**重算（红线 7 只挡杜撰、本能力提供确定性重算通道）。
- **训练模型套定额接口预留**（决策 4）：新增 `cost/quota_selection.py` —— `select_quota(feature, code, quotas)` + `register_quota_selector(fn)` 挂点，红线兜底（只选候选内子目、越界作废、低置信 need_review）与 `select_code` 同构；`quota_gate_node` 多子目分支改走 `select_quota` 置信门控。**默认无模型 → need_review（多子目维持人工确认，行为不变）**；训练模型就绪后 `register_quota_selector` 注入即生效，无需再改图。
- 自测 `tools/test_backlog.py`（recompute_rollup 4 项 + select_quota 红线 4 项，无 pytest 依赖，服务器 `uv run python tools/test_backlog.py`）。

### 8.3 Backlog 剩余

- **训练模型套定额**（决策 4）：**接口已就位**，待模型训好后实现 `QuotaSelector` 并 `register_quota_selector` 注入；届时补 compute 节点持久化 + benchmark。
- **前端类型检查/e2e**：本次前端改动（cost-hitl-events + hooks 订阅）须在服务器/CI 跑 `pnpm check`。

### 8.4 存档（不启动）

**全 B2 = 方案 A**（纯消费桥接工具 + 图外协调者 exactly-once 推进 + `start` 幂等）：需补通 `command→graph_input`（`services.py:338`）/ worker `Command(resume)` 驱动 / `__interrupt__` SSE 三条链路。**仅当硬需求确认后按方案 A 做，绝不用方案 C（端点逐个加守卫）。** 论证见 §7.4 调研结论。
