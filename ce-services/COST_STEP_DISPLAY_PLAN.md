# 组价中间步骤前端可视化 · 实施方案

> 目标：用户输入「实体名称 + 做法特征」触发组价后，**清单匹配 → 套定额 → 取价 → 单位工程汇总 → 单项工程汇总 → 总造价**
> 各步骤在前端逐步显示。本文是分阶段落地方案，对齐现有代码与红线，供 Mac↔服务器对照。
>
> 相关文档：编排/分层原则 `HITL_DESIGN.md`、任务层需求 `PRD.md`、算钱原语 `cost/pricing.py`、图 `cost/graph.py`。
> 主线挂点见根 `AGENT_TODO.md`「M4 · 组价步骤可视化」。

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
| `ce-services/common/mcp_server.py` | **新增** `@mcp.tool() start_cost_session(feature, spec, region, features?)`：调 `session.start` 起会话，返回 `{task_id, marker:"```cost-hitl…", first_gate}`。agent 原样转贴 marker → 前端出卡。**只点火不编排**（编排留图里，红线） |

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

让 `orchestrate` 起会话后不返回、原地等 done 会死锁：工具不返回 → 前端拿不到 `task_id` → 卡片不渲染
→ 用户无处可点 → 闸推进不了 → 会话永停首闸 → 工具永远等不到 done。**「卡片要 task_id 才能渲染」与
「工具阻塞等完」互斥**，故工具必须立刻返回（现状解耦即由此逼出）。B2 不走阻塞，走**节点内嵌套 interrupt**。

### 7.2 B1（坏）vs B2（好）—— 关键区分

- **B1**：LLM 每闸进环（读闸→转述→收决策→resume）。🔴 撞 HITL_DESIGN §10 / AGENT_DEV §9 line 279
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
