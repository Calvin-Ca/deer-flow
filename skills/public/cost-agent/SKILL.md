---
name: cost-agent
description: "Use this skill when working on the construction cost workflow in DeerFlow: selecting bill codes, composing pricing data, computing unit prices, rolling up totals, or handling HITL gates for cost tasks. It is the operator-facing entry point for the cost tool set with identifiers ending in _tool."
---

# Cost Agent

Use the cost tool set with explicit identifiers:

- `ce-task_orchestrate_tool`
- `ce-task_cost_compose_tool`
- `ce-task_norm_qa_tool`
- `ce-task_start_cost_session_tool`
- `cost_match_bill_item_tool`
- `cost_price_compose_envelope_tool`
- `cost_compute_unit_price_tool`
- `cost_rollup_tool`
- `cost_rollup_hierarchy_tool`
- `cost_gate_decision_tool`
- `cost_build_manual_quota_basis_tool`
- `cost_select_quota_tool`

Workflow:

1. Resolve the user request to the right cost capability.
2. Use `*_tool` identifiers consistently in tool calls and logs.
3. Stop on `need_review` or `needs_human_input`.
4. Resume only after the human decision is normalized into the expected input shape.

