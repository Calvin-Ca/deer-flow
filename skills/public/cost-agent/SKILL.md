---
name: cost-agent
description: "Use this skill when working on construction cost tasks: selecting bill-code candidates, querying structured pricing data, coordinating deterministic calculation, or starting/resuming the internal DeerFlow cost workflow. The previous broad task-layer MCP front door is disabled because it mixed routing, QA, pricing, calculation, and HITL."
---

# Cost Agent

Do not call the previous broad task-layer MCP front door. It is not an agent-visible tool.

Use the internal DeerFlow workflow tools for workflow or node-level cost work:

- `cost_workflow_start`
- `cost_workflow_node`
- `cost_workflow_resume`
- `cost_workflow_state`

Workflow nodes may call these current global MCP primitives internally:

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

Deterministic calculation and HITL gates remain workflow-node responsibilities, not lead-agent steps.

Workflow:

1. Resolve the user request to the right cost capability.
2. For a complete stateful pricing task, call `cost_workflow_start`.
3. For intermediate work inside the pricing process, call `cost_workflow_node` with the exact node:
   - `bill_match` for candidate recall.
   - `select_bill` for candidate selection/review contract.
   - `price_compose` for bill-code pricing data.
   - `bill_get`, `quota_get`, `price_query`, `fee_rate_lookup` for known-key data.
   - `unit_price`, `rollup`, `check` for deterministic local nodes.
4. For a raw fact lookup where a workflow node adds no value, the narrow `ce-rag_*` / `ce-db_*` primitive may still be used directly.
5. For bill-code selection, select only within returned candidates or stop for review.
6. Stop on `interrupt`, `need_review`, or `needs_human_input`.
7. Resume only with `cost_workflow_resume` after the human decision is normalized into the expected input shape.
