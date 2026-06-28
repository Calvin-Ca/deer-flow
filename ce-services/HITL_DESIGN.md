# 工程造价 Human-in-the-Loop（HITL）编排方案

> 适用范围：`工程造价完整流程.md` 描述的「项目特征 → 总造价」13 步全流程。
> 本文是**编排层（ce-services）设计文档**，定义为什么要可中断编排、怎么分层、数据契约长什么样。
> 计算原语归 `ce-cost`，编排图归 `ce-services`，本文是两者的接口约定。

---

## 0. 一句话结论

**这个 13 步流程装不进 `cost.py` 端到端黑盒——HITL 是压垮黑盒的稻草。**
解决办法：把**编排逻辑上提成一个可中断的状态机（langgraph 图）**，`cost.py` 拆成一组**确定性原语**；
每个数字都带**结构化的来源（provenance）**，每个需要人介入的点都是一个**可暂停、可恢复的闸门（interrupt）**。

---

## 1. 背景与核心判断

### 1.1 为什么黑盒做不到

`python3 cost.py` 一旦启动就是黑盒：跑完才一次性返回 stdout，**中途不能暂停、中间态不发前端**。
而本流程的两类需求恰好都要这两样能力：

| 用户需求 | 本质 | 黑盒能否满足 |
|---|---|---|
| 让用户知道决策是否有问题（展示信息源/依据） | 要**展示中间态** | ❌ |
| 需要用户提交信息（版本/定额确认/信息价来源…） | 要**中途暂停等输入再继续** | ❌ |

所以不是「优化 cost.py」，而是**重新分层**。这也给「中间步直调 MCP 原语」找到了硬理由：
之前直调 MCP 只是为了前端可见性（nice to have），HITL 让它变成**必须**。

### 1.2 重新分层后的职责

| 层 | 职责 | 归属 |
|---|---|---|
| **计算原语** | 选码 / 套定额 / 标准化 / 查价 / 算费，确定性、返回结构化依据 | `ce-cost`（MCP/可调函数） |
| **编排图** | 步骤状态机、闸门、状态持久化、provenance 事件、可恢复 | `ce-services`（langgraph） |
| **会话门面** | 意图路由 + setup 澄清，**不当逐步编排器** | `lead_agent` |
| **展示/交互** | 依据卡 + 确认/录入控件，从结构化 payload 渲染 | 前端 |

> ⚠️ **不要把 HITL 塞进 cost-agent 这个 skill。** 弱模型（Qwen3-8B）当不了 13 步财务流程的编排器——会跳闸、会乱调、会编依据。**编排放代码（图）里，模型只在「需要判断」的节点用。**

---

## 2. 四条设计原则

1. **Provenance 是数据，不是模型口述。**
   每个数字（编码/价格/费率）必须由原语**返回**其来源字段，agent 只**透传**。
   造价要交付、要审计、错一个编码就是真金白银；让弱模型用文字写「依据」＝它会编造条文号和价格来源。

2. **门控，不逐步 gate。**
   13 步逐个确认＝不可用。只有 `need_review` / 多候选 / 缺价 / 政策参数 才中断。
   模式 = **1 个前置 setup 闸 ＋ N 个例外闸 ＋ 1 个末尾总览 review**。

3. **Override 优于重算。**
   用户改了某个定额，就**钉住**正确值、下游确定性重算；不要重跑 LLM 匹配（弱模型会漂）。
   用户修正＝权威锁定输入。

4. **状态持久化、可跨会话恢复。**
   造价是长任务，用户会走开再回来。已确认编码、override、选定价存成按 `task_id` 持久化的状态文档
   （langgraph checkpointer），HITL 可跨会话续上。

---

## 3. 13 步 → 编排节点映射

| 步骤 | 性质 | 节点类型 | HITL 闸 |
|---|---|---|---|
| ① 项目特征输入 | 用户输入 | 入口录入 | setup 录入 |
| ② 清单编码 | 判断+检索 | 原语 + **确认闸** | ✅ 确认型（候选+依据，可改） |
| ③ 工程量 | 确定性几何 | 原语 | ❌（已完成） |
| ④ 套定额 | 判断 | 原语 + **确认闸** | ✅ 确认型（子目+依据） |
| ⑤ 材料标准化 | 判断 | 原语 + 条件闸 | ⚠️ 仅歧义时 |
| ⑥ 查信息价 | 来源+数据 | 原语 + **来源闸**+缺价闸 | ✅ 来源选择 + 逐项例外 |
| ⑦ 资源费 | 纯算术 | 原语 | ❌ |
| ⑧ 综合单价（费率） | 政策参数 | **录入闸** | ✅ 录入型（管理费/利润/风险） |
| ⑨ 分部分项 | 纯算术 | 原语 | ❌ |
| ⑩⑪ 措施/其他 | 项目参数 | 录入闸 | ✅ 录入型 |
| ⑫ 规费税金 | 参数 | 录入闸 | ✅ 录入型（税率/口径） |
| ⑬ 汇总 | 纯算术 | 原语 + **末尾 review** | ✅ 总览复核 |

合计：**1 个 setup 闸 + 编码/定额/费率/参数等确认录入闸 + 信息价例外闸 + 1 个末尾 review**。

### 3.1 langgraph 图骨架（示意）

```
[setup] --interrupt(setup)--> [list_coding] --interrupt(confirm?)-->
[quantity] --> [quota] --interrupt(confirm?)--> [normalize] --interrupt(ambiguous?)-->
[price_setup] --interrupt(source)--> [price_query] --interrupt(missing?)-->
[resource_cost] --> [unit_price] --interrupt(rates)--> [subitem_cost] -->
[measure/other/fee] --interrupt(params)--> [rollup] --interrupt(final_review)--> [done]
```

- 实线节点 = 确定性原语调用，无人介入直接过。
- `interrupt(...)` = 闸门，**按门控规则**决定是否真的暂停（见 §6）。
- 每个节点完成后**发一个 provenance 事件**给前端，无论是否暂停。

---

## 4. 两类 HITL 机制

### 4.1 确认型（confirm）—— 编码 / 定额 / 材料

模型/检索给候选，用户 **✓ 通过 / 选备选 / 手动改**。

### 4.2 录入型（input）—— 信息价来源 / 费率 / 项目参数

用户**填值或选来源**。前端按字段类型渲染单选 / 数字框 / 可编辑表格。

> 两类都走 langgraph `interrupt()`（或现成 `ask_clarification`，但 payload 必须**结构化**，不是自由文本问句）。

---

## 5. 数据契约

### 5.1 原语返回（通用 provenance 信封）

所有 `ce-cost` 原语统一返回该结构，**来源是字段不是散文**：

```json
{
  "step": "list_match",
  "status": "ok | need_review | no_source | error",
  "result": { },
  "provenance": {
    "source_type": "spec_clause | quota_lib | price_book | online | user_input",
    "source_ref": "GB50500-2013 §4.2.1 | 深圳2024定额 A1-15 | 深圳信息价2024-06 第32行 | https://...",
    "confidence": 0.0,
    "alternatives": [ ]
  }
}
```

各原语 `result` 字段：

| 原语 | 入参 | result |
|---|---|---|
| `list_match` | 项目特征, spec_version | `{code, name, 项目特征归类, unit}` |
| `pick_quota` | code, spec_version | `{子目号, 人工/材料/机械消耗量}` |
| `normalize_material` | 定额材料 | `{标准材料, 规格型号}` |
| `query_price` | material, region, period, source | `{单价, price_status, 原始行}` |
| `compute_resource_cost` | 消耗量×价 | `{人工费, 材料费, 机械费, 直接费}` |
| `compute_unit_price` | 直接费, 费率 | `{综合单价}` |
| `rollup` | 各项费用 | `{总造价明细}` |

### 5.2 确认型 interrupt payload

```json
{
  "gate_type": "confirm",
  "node": "list_coding",
  "title": "请确认清单编码",
  "proposal": { "code": "010503002001", "name": "矩形梁", "unit": "m3" },
  "evidence": {
    "source_type": "spec_clause",
    "source_ref": "GB50500-2013 §附录E",
    "confidence": 0.72,
    "matched_similar": ["010503002 现浇混凝土梁"]
  },
  "alternatives": [ { "code": "...", "name": "...", "confidence": 0.61 } ],
  "actions": ["approve", "select_alternative", "manual_override"]
}
```

### 5.3 录入型 interrupt payload（以信息价来源为例）

```json
{
  "gate_type": "input",
  "node": "price_source_setup",
  "title": "信息价取价方式",
  "fields": [
    { "key": "source", "type": "enum", "label": "来源",
      "options": ["local 本地信息价库", "online 联网查", "manual 手动输入"], "default": "local" },
    { "key": "region", "type": "text", "label": "地区", "default": "深圳" },
    { "key": "period", "type": "month", "label": "期号（年月）", "required": true }
  ]
}
```

### 5.4 任务状态文档（持久化，按 task_id）

```json
{
  "task_id": "...",
  "spec_version": "2013",
  "region": "深圳",
  "period": "2024-06",
  "price_source": "local",
  "rates": { "management": 0.0, "profit": 0.0, "risk": 0.0, "tax": 0.09 },
  "items": [
    {
      "feature": { },
      "code":    { "value": "010503002001", "locked": true,  "provenance": { } },
      "quota":   { "value": "A1-15",        "locked": false, "provenance": { } },
      "materials": [ { "raw": "混凝土", "std": "C30商品混凝土", "price": { "value": 480, "status": "ok", "provenance": { } } } ]
    }
  ],
  "overrides": [ { "node": "quota", "item": 0, "by": "user", "value": "A1-16", "at": "..." } ],
  "audit_log": [ ]
}
```

- `locked: true` ＝ 用户已确认/覆盖，**下游只读、确定性重算，不再触发 LLM 匹配**。
- `overrides` + `audit_log` ＝ 审计轨迹，支撑造价文件交付。

---

## 6. 闸门触发规则（门控）

每个 `interrupt(...)` 节点先算「要不要真的停下来」：

| 节点 | 自动通过条件 | 触发暂停条件 |
|---|---|---|
| 编码 | `confidence ≥ τ` 且无多候选 | `need_review` / 多候选并列 / 低置信 |
| 定额 | 唯一子目且 `confidence ≥ τ` | 多子目 / 低置信 |
| 材料标准化 | 唯一映射 | 一对多歧义 |
| 信息价 | 来源已选且全部命中 | 缺价 / `price_status≠ok` / `no_source` |
| 费率/参数 | 已有项目默认值 | 首次、无默认值 |
| 末尾 review | —— | **始终暂停**（总造价定稿前必看） |

> 阈值 `τ` 可配置；保守起步设高（多停几次），跑顺了再放松。

---

## 7. 信息价来源策略（重点）

**一次性策略选择 + 逐项例外**，绝不逐材料问：

1. **setup 阶段**：用户选默认来源（本地库 / 联网 / 手输）+ 地区 + 期号 → 写进状态文档 `price_source`。
2. **批量套用**：`query_price` 对所有材料按该来源取价。
3. **逐项例外闸**：只有在该来源下**查不到 / 状态非 ok / 多规格歧义**的材料，才弹单材料录入闸。

> 反例（禁止）：50 个材料问 50 次「网上还是本地」。来源是策略，不是逐条决策。

每条价都带 provenance：`{source_type, source_ref(文件+行 / URL / "用户输入"), region, period}` → 前端可展开「这条价从哪来」。

---

## 8. 前端展示

| 元素 | 数据来源 | 渲染 |
|---|---|---|
| 依据卡（每个自动决策旁） | 节点 provenance 事件 | 可展开：来源 + 置信度 + 备选 + 原始引用 |
| 确认控件 | confirm payload | 候选表格 + ✓/✗/改 |
| 录入控件 | input payload | 单选 / 数字框 / 可编辑表格 |
| 进度/审计 | audit_log | 时间线：哪步谁改了什么 |
| 总览 review | rollup result | 全量造价明细 + 逐项可追溯 |

关键：前端**只从结构化字段渲染**，不解析模型自然语言。这样弱模型即使措辞乱，展示也不会错。

---

## 9. 落地路径（建议顺序）

1. **🟢 定原语契约**（本地就位 2026-06-28）：§5.1 provenance 信封落 `ce-services/cost/provenance.py`——
   **决策改为「原地包一层」**（不新建 `ce-cost/` 目录、不搬代码），适配器 `list_match`/`from_price_compose`
   裹现有 `bill_match`/`select_code`/`price_compose`。信息价文件名+行号级 `source_ref` 知识层暂未返回，
   best-effort + `TODO(knowledge-layer)` 标注（去标记是本步后续）。
2. **⬜ 打通信息价那一步**（HITL × provenance 交汇最密）：来源闸已在 setup 采集（`price_source`）、缺价逐项闸
   已在图 `price_gate` 实现；待知识层补精确 `source_ref` + 真数据联调。
3. **🟢 建 langgraph 图骨架**（§3.1，本地就位 2026-06-28）：`ce-services/cost/graph.py` 实现
   `setup → 编码 → 定额 → 信息价` + interrupt/resume + SqliteSaver checkpointer 持久化；会话端点
   `/cost/session/{start,resume,state}`。**compute/gate 双拆**避免 resume 重跑 LLM。待服务器联调。
4. **⬜ 接前端**：依据卡 + 两类控件，从 §5.2/5.3 payload 渲染。
5. **🟢 补全费率/措施/规费税金录入闸 + 末尾 review**（本地全图 e2e 验证 2026-06-28）：图补到
   `… price_gate → rates_gate(§8) → params_gate(§10⑪§12) → rollup(§13 末尾 review) → done`。后段三节点确定性算钱
   （`compute_unit_price` / `rollup_cost`，无 LLM）：费率/参数走录入闸（缺政策数停、不杜撰），rollup 始终暂停做总造价复核。
   综合单价**不含税**、税金在 rollup 一次性计（§2.0.9）。待服务器真链路联调。
6. **⬜ 门控阈值调参**：从保守（多停）逐步放松（`CE_HITL_CONFIDENCE_TAU` 默认 0.75）。

> 实现细节（模块职责、端点、跑法、依赖）见 `DEV.md`「HITL 可中断组价图」；进度见 `TODO.md`「主线三」。

---

## 10. 风险与边界

- **弱模型 ≠ 编排器**：Qwen3-8B 只用于「判断型」节点（选码/标准化）的候选生成，**不驱动流程、不决定是否跳闸**——这些在图代码里。
- **原语错误如实透传**：脚本报 503/缺价/`no_source`，原文转达、标 `need_review`，不在沙箱里自救补编。
- **不算超出范围的钱**：本流程到 §3 各步为止；综合单价以上的费率/税金是录入型参数，系统只做带 provenance 的汇总，不替用户定政策数。
- **简单场景仍走旧路**：一次性「选码取数、无需人介入」可继续走 cost.py 端到端；**判据 = 是否需要 HITL/可审计**。两条路并存，边界清晰。
