---
name: cost-agent
description: "Use this skill when working on construction cost tasks: selecting bill-code candidates, querying structured pricing data, coordinating deterministic calculation, or handing a full HITL workflow to the application session API. The previous broad task-layer MCP front door is disabled because it mixed routing, QA, pricing, calculation, and HITL."
---

# Cost Agent

Do not call the previous broad task-layer MCP front door. It is not an agent-visible tool.

Use the current global MCP primitives:

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

Service-side calculation functions remain internal implementation details, not MCP front doors:

- `cost_compute_unit_price_tool`
- `cost_rollup_tool`
- `cost_rollup_hierarchy_tool`
- `cost_gate_decision_tool`
- `cost_build_manual_quota_basis_tool`
- `cost_select_quota_tool`

Workflow:

1. Resolve the user request to the right cost capability.
2. For single-point work, call the narrow `ce-rag_*` / `ce-db_*` primitive that matches the known input.
3. For bill-code selection, recall candidates with `ce-rag_match_bill_item`, then select only within returned candidates or stop for review.
4. For full stateful pricing, do not emulate the workflow with MCP calls; hand off to the application Cost Session API.
5. Stop on `need_review` or `needs_human_input`.
6. Resume only after the human decision is normalized into the expected input shape.
