# ce-rag / ce-db / ce-task Interface Contracts

This document defines the service boundary and data semantics between `ce-rag`, `ce-db`, and `ce-task`.
It is not a public API manual or an OpenAPI replacement. Its purpose is to prevent the task layer and agents
from confusing semantic candidates, evidence, and structured truth.

## 1. Service Boundaries

### ce-rag

`ce-rag` provides semantic retrieval, candidate recall, and evidence.

It is responsible for:

- retrieving regulation clauses from indexed corpora;
- recalling bill-item candidates from natural-language construction descriptions;
- retrieving auxiliary table and pricing-rule evidence;
- returning evidence with explicit provenance and truth level.

It is not responsible for:

- declaring a recalled bill item as the final selected code;
- returning structured pricing truth;
- calculating prices, fees, or totals;
- replacing `ce-db` for deterministic key-based lookups.

### ce-db

`ce-db` provides structured truth and pricing data.

It is responsible for:

- looking up bill items by explicit code and spec;
- looking up quotas, fee rates, price composition, auxiliary table rows, and resources;
- querying information prices by name, region, period, and category;
- composing pricing data from an explicit region, spec, and bill code.

It is not responsible for:

- selecting a bill code from an ambiguous natural-language description;
- semantic retrieval over regulation clauses;
- natural-language explanation or final answer generation.

### ce-task

`ce-task` provides routing, orchestration, candidate decision, and HITL entry points.

It is responsible for:

- routing user requests into `norm`, `cost`, `price`, or `compound` capabilities;
- selecting the applicable standard for norm QA;
- calling `ce-rag` for candidates and evidence;
- selecting a code from recalled candidates, or stopping for review;
- calling `ce-db` only after the required structured key is available;
- enforcing guardrails such as no fabricated price and no final truth from semantic candidates.

It must not:

- treat `ce-rag` bill-match results as final bill truth;
- let an LLM fabricate missing prices, quotas, or fee rates;
- continue to `cost_price_compose_envelope_tool` when code selection returns `need_review`;
- use `retrieve_evidence` as a shortcut for business workflows that have a dedicated endpoint.

## 2. Truth Levels

All cross-service results should be interpreted through these truth levels.

| Term | Meaning | Typical Source | Allowed Use |
| --- | --- | --- | --- |
| `semantic_candidate` | A plausible match returned by semantic retrieval. It is not final truth. | `ce-rag /search/bill-match` | Input to selection or HITL review. |
| `ground_truth` | Structured or directly grounded truth from authoritative local data. | `ce-db /bill/{code}`, regulation clauses from indexed standard corpora | Can support final business output when guardrails pass. |
| `evidence` | Citable support for an answer or decision. | `ce-rag` clause, auxiliary table, price-rule evidence | Can be shown to users and audited. |
| `projection` | Structured projection derived from rules, tables, or indexed documents. | `ce-rag search_aux_table`, `search_price_rule` | Supportive evidence; not a substitute for DB truth when a DB endpoint exists. |
| `price_source` | Structured price, quota, fee, or resource data. | `ce-db price_query`, `cost_price_compose_envelope_tool`, `fee_rate_lookup` | Can support pricing output; missing data must stay explicit. |

## 3. ce-rag Contracts

### `POST /search/clause`

Purpose: regulation clause retrieval.

Request semantics:

- `query`: natural-language regulation or measurement question.
- `standard`: explicit normalized standard identifier, for example `gb50854-2024`.
- `top_k`: maximum returned clauses.
- `skip_rerank`: optional retrieval behavior flag.

Response semantics:

- `clauses`: retrieved regulation clauses.
- `evidence`: citable clause evidence.
- `meta`: retrieval metadata.
- `count`: number of returned clauses.

Contract:

- Returned clauses are grounded in the selected local standard corpus.
- The caller must not cite clauses from another standard family or version after guard audit.
- Zero recall is a valid business state and must not be treated as fabricated answer material.

### `POST /search/bill-match`

Purpose: construction description to bill-item candidates.

Request semantics:

- `description`: natural-language construction item or component description.
- `spec`: bill specification version, for example `2013` or `2024`.
- `top_k`: maximum returned candidates.
- `code_prefixes`: optional domain filter.

Response semantics:

- `candidates`: semantic candidates with fields such as `code`, `name`, `unit`, `feature`, `chapter`, `score`.
- `evidence`: evidence records with `truth_level = semantic_candidate`.
- `count`: number of returned candidates.

Contract:

- Candidate `code` values are suggestions only.
- A candidate must pass `ce-task` selection or HITL review before it is used as the selected bill code.
- Candidate score is a retrieval signal, not a confidence guarantee.
- Structural penalties such as `type_penalty` and `prefab_penalty` are ranking diagnostics, not final truth.

### `POST /retrieve/evidence`

Purpose: unified evidence retrieval facade.

Supported `corpus` values:

- `clause`
- `bill_match`
- `aux_table`
- `price_rule`

Contract:

- This endpoint returns evidence or candidates according to the selected corpus.
- It is not the primary entry point for complete business workflows.
- For cost composition, callers should use `ce-task /cost/compose` or the equivalent MCP tool (`cost_compose_tool`).
- For known-key pricing, callers should use `ce-db` directly or through `ce-task` tools such as `quota_lookup_tool` and `price_lookup_tool`.

## 4. ce-db Contracts

### `GET /bill/{code}?spec={spec}`

Purpose: bill-item truth lookup by explicit code.

Response semantics:

- The result is structured bill truth for the requested `code` and `spec`.

Contract:

- The input `code` must already be explicit, selected, or user-confirmed.
- `ce-db` must not guess a code from natural-language text.
- Missing code should be represented as key-not-found, not as a fabricated fallback.

### `GET /price/compose/{region}/{code}?spec={spec}`

Purpose: compose pricing source data from explicit region, bill code, and spec.

Response semantics:

- Returned data may include applicable quotas, labor/material/machine details, information prices, and price status.

Contract:

- `code` must be explicit and selected before this endpoint is called.
- Missing resource prices must remain explicit, for example as `no_source`.
- `no_source` is a business data state, not a system failure.
- The caller must not ask an LLM to fill missing prices or quotas.

### `GET /price/query`

Purpose: information-price lookup by resource name and optional filters.

Request semantics:

- `name`: resource name or search term.
- `region`: region, defaulting to Shenzhen in current business scope.
- `period`: optional period.
- `category`: optional labor/material/machine category.
- `top_k`: maximum returned rows.

Contract:

- Zero matches should be returned as a normal empty result, not fabricated.
- Dynamic price data is independent of the bill spec unless an endpoint explicitly says otherwise.

## 5. ce-task Contracts

### `POST /route`

Purpose: route a user request to a capability.

Response semantics:

- `capability`: one of `norm`, `cost`, `price`, `compound`, or `out_of_domain`.
- `route_confidence`: `high` or `low`.
- `route_source`: `deterministic`, `llm_fallback`, or `session_sticky`.
- `clarify`: required clarification type when applicable.

Contract:

- Deterministic red-line checks remain authoritative even when LLM fallback supplies the capability.
- `low` confidence should be auditable and covered by routing regression cases.

### `POST /norm/qa`

Purpose: regulation QA with grounded citations.

Expected chain:

1. Resolve the standard deterministically.
2. Call `ce-rag /search/clause`.
3. Generate a cited answer.
4. Audit citations and guardrails.

Contract:

- The final answer must be based on retrieved clauses or an approved fallback path.
- If no credible evidence exists, the system must refuse or explain the searched scope.
- Citations must survive guard audit before being presented as support.

### `POST /cost/compose`

Purpose: one-shot cost composition from construction description.

Expected chain:

1. Call `ce-rag /search/bill-match` for semantic candidates.
2. Run `ce-task` candidate selection within the returned candidate set.
3. If selected code is missing or `need_review`, stop and return review state.
4. Call `ce-db /price/compose/{region}/{code}` only after code selection.
5. Return selection, code, price data or explicit missing-data state, and guard metadata.

Contract:

- Candidate recall is not final code selection.
- `need_review` stops automatic pricing.
- Price or quota gaps must remain explicit.
- LLMs may help select among candidates, but must not invent codes outside the candidate set unless a documented HITL path confirms them.

### `POST /orchestrate`

Purpose: front-door orchestration for single and compound tasks.

Contract:

- Single-capability requests may be dispatched directly to the relevant capability layer.
- Compound requests must be decomposed, then each subtask must route again.
- The synthesized answer must preserve cited clauses, selected codes, and missing-data states from subtasks.

## 6. Status and Error Semantics

| State | Meaning | Caller Behavior |
| --- | --- | --- |
| `200 + count = 0` | Request succeeded, no matching result. | Report no match or continue with approved fallback. |
| `200 + no_source` | Business data source is missing for some requested price/resource. | Keep missing state explicit; do not fabricate. |
| `need_review` | Candidate decision is not safe enough for automation. | Stop automatic downstream calls; request review or HITL. |
| `400` | Invalid request, unknown spec, or missing required parameter. | Fix request; do not retry unchanged. |
| `404` | Explicit key does not exist. | Report missing key or ask for correction. |
| `501` | Version, region, or data slice is not ready. | Return not-ready state without discarding useful upstream selection. |
| `503` | Dependency unavailable, such as Milvus, PG, embedding, or LLM service. | Retry or surface service degradation. |

## 7. Red Lines

- `ce-rag /search/bill-match` returns `semantic_candidate`; it must not be treated as `ground_truth`.
- `ce-db` is the source for structured bill, quota, fee, and price truth.
- `ce-task` must not continue to pricing when code selection is absent or marked `need_review`.
- Missing prices, quotas, rates, or resources must not be filled by an LLM.
- `retrieve_evidence` is an evidence facade, not a replacement for `cost_compose_tool`, `price_query`, or `cost_price_compose_envelope_tool`.
- Cross-version or cross-standard evidence must be filtered or downgraded by guardrails before final presentation.

## 8. Current Contract Coverage

This first version covers the currently validated runtime chain:

- `ce-rag`: `/search/clause`, `/search/bill-match`, `/retrieve/evidence`
- `ce-db`: `/bill/{code}`, `/price/compose/{region}/{code}`, `/price/query`
- `ce-task`: `/route`, `/norm/qa`, `/cost/compose`, `/orchestrate` (MCP tools: `orchestrate_tool`, `norm_qa_tool`, `cost_compose_tool`)

Future updates should add contract details for quota lookup, fee-rate lookup, auxiliary table lookup, price composition lookup,
resource lookup, HITL session flows, and MCP `tools/call` request/response examples.
