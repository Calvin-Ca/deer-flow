---
name: cost-agent
description: "工程造价任务用本技能：清单编码候选选码、结构化组价取数、协调确定性计算、启动/续跑 DeerFlow 内部组价 workflow。原任务层大 MCP 前门已停用（它把路由、问答、询价、计算、HITL 混在一起）。"
---

# Cost Agent（组价技能）

不要调用已停用的旧任务层大 MCP 前门——它不是 agent 可见工具。

workflow 级或节点级的组价工作，用 DeerFlow 内部 workflow 工具：

- `cost_workflow_start`
- `cost_workflow_node`
- `cost_workflow_resume`
- `cost_workflow_state`

workflow 节点内部可能调用以下全局 MCP 原语：

- `ce-rag_search_clause`
- `ce-rag_expand_clause_refs`
- `ce-rag_get_clause`
- `ce-rag_match_bill_item`
- `ce-rag_search_aux_table`
- `ce-rag_search_price_rule`
- `ce-rag_retrieve_evidence`
- `ce-db_bill_get`
- `ce-db_bill_list`
- `ce-db_quota_get`
- `ce-db_quota_list`
- `ce-db_price_query`
- `ce-db_price_compose`
- `ce-db_fee_rate_lookup`
- `ce-db_price_composition_get`
- `ce-db_aux_table_get`
- `ce-db_aux_table_list`
- `ce-db_resource_lookup`

确定性计算与 HITL 闸门是 workflow 节点的职责，不是 lead agent 的步骤。

工作流程：

1. 先把用户诉求归到正确的组价能力上。
2. 完整有状态组价任务，调 `cost_workflow_start`。
3. 组价过程中的中间工作，调 `cost_workflow_node` 并指定节点：
   - `bill_match`——清单候选召回。
   - `select_bill`——候选选定/复核契约。
   - `price_compose`——清单码组价取数。
   - `bill_get`、`quota_get`、`price_query`、`fee_rate_lookup`——已知键取数。
   - `unit_price`、`rollup`、`check`——确定性本地节点。
4. 纯事实查询、workflow 节点无增益时，可直接用窄原语 `ce-rag_*` / `ce-db_*`。
5. 选码只在返回的候选内选，选不出就停下转人工复核。
6. 遇到 `interrupt` / `need_review` / `needs_human_input` 就停。
7. 人工决策归一成期望的输入形状后，只用 `cost_workflow_resume` 续跑。
