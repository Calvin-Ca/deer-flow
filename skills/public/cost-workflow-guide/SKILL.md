---
name: cost-workflow-guide
description: "组价 workflow 操作手册：完整组价（cost_workflow_start 逐闸 HITL）与节点点播（cost_workflow_node 的节点名/payload/闸语义/resume 契约）。单点能力另有直调工具（bill_match 选码核实 / quota_recommend 定额推荐 / price_query 询价 / cost_calc 计算），不经本手册。"
---

# Cost Workflow Guide（组价 workflow 操作手册）

> 本 skill 只教「怎么用 workflow」；「何时用什么」由系统提示词路由表决定。
> 单点诉求优先用直调工具：`bill_match`（选码/核实）、`quota_recommend`（定额方案）、
> `price_query`（信息价/走势）、`cost_calc`（确定性计算）——它们不走 workflow。

## 完整组价（有状态、逐闸 HITL）

调 `cost_workflow_start`，把用户给的清单原样传入（`feature` 单条 / `features` 多条），
不预处理、不筛选。流程按阶段推进：选码召回 → 选码闸 → 组价取数 → 定额方案闸 →
询价闸 → 结算。

- 遇 `status=awaiting_input`：如实转述 `interrupt` 里需要用户确认或补充什么，**不替用户选择**；
  拿到用户答复后调 `cost_workflow_resume(task_id, decision)` 续跑。**没有用户新输入时严禁调 resume**。
- 闸载荷带 `recommendation`（系统预排建议）时一并转述其理由，选定仍归用户。
- 用户问进度/依据/中间结果：调 `cost_workflow_state(task_id)` 后转述。

## 节点点播（cost_workflow_node，中间步/单原语）

组价过程中的某一步单独执行时，调 `cost_workflow_node(node, payload)`：

- `bill_match`——清单候选召回（只给候选、不选定）；payload：`description`（特征原文）、`spec?`、`top_k?`。
- `select_bill`——候选内门限选定/复核契约；payload：`candidates`（召回结果）、`description?`。
- `price_compose`——已确认清单码 → 可组定额 + 工料机含量 + 信息价；payload：`code`、`spec?`、`region?`。
- `bill_get` / `quota_get` / `price_query` / `fee_rate_lookup`——已知键取数。
- `unit_price` / `unit_rate` / `line_total` / `rollup` / `check`——确定性计算节点（也可直接用 `cost_calc` 工具）。

## 纪律（与系统红线一致）

1. 选码只在返回的候选内选，选不出就停下转人工复核，不造码。
2. 遇到 `interrupt` / `need_review` / `unsupported_spec` / `unsupported_region` 就停并如实转述。
3. 人工决策归一成闸要求的字段形状（见 `interrupt.required_fields`）后，只用 `cost_workflow_resume` 续跑。
4. 价格/编码/定额号只用工具返回的，缺价如实说 no_source，不编价。
