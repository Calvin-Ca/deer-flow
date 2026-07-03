/**
 * Types for the ce-services cost HITL session API (port 8101).
 *
 * Mirrors the structured payloads emitted by ``ce-services/cost/{gates,session}.py``:
 * the session response, the three interrupt gate kinds (confirm / input / review),
 * and the per-node provenance events that drive the 依据卡 (evidence cards).
 * The frontend renders strictly from these structured fields — never from
 * free-text model prose (HITL design §8).
 */

/** Provenance block carried by every primitive envelope (HITL design §5.1). */
export interface Provenance {
  source_type?: string | null;
  source_ref?: string | null;
  confidence?: number | null;
  alternatives?: unknown[];
  [k: string]: unknown;
}

/** A per-node provenance event — one per graph node, paused or not (依据卡 data source). */
export interface CostEvent {
  step?: string;
  status?: string;
  provenance?: Provenance | null;
  result?: Record<string, unknown> | null;
  paused?: boolean;
  /** Gate auto-passed without asking the user (依据时间线标「自动采纳」). */
  auto_pass?: boolean;
  /** Decision confidence (coding gate only; null elsewhere). */
  confidence?: number | null;
  /** Auto-pass confidence threshold τ (coding gate only) — renders 「置信 X ≥ 阈值 τ」. */
  tau?: number | null;
  at?: string;
}

/** confirm-gate payload (编码 / 定额) — proposal + evidence + alternatives, user ✓/选备选/改. */
export interface ConfirmInterrupt {
  gate_type: "confirm";
  node: string;
  title: string;
  proposal: Record<string, unknown>;
  evidence: {
    source_type?: string | null;
    source_ref?: string | null;
    confidence?: number | null;
  };
  alternatives: Array<{ code?: string; name?: string; [k: string]: unknown }>;
  actions: string[];
}

/** One field of an input gate (setup / 缺价 / 费率 / 参数). */
export interface InputField {
  key: string;
  type: "enum" | "text" | "number" | "month";
  label: string;
  options?: string[];
  default?: unknown;
  required?: boolean;
}

/** input-gate payload — user fills values or picks a source. */
export interface InputInterrupt {
  gate_type: "input";
  node: string;
  title: string;
  fields: InputField[];
  context?: Record<string, unknown>;
}

/** 单位工程汇总节点（两级汇总，rollup_hierarchy 输出）。 */
export interface UnitWorkRollup {
  name: string;
  subtotal: number;
  item_count: number;
  missing_unit_price_items: number;
}

/** 单项工程汇总节点（含其下单位工程）。 */
export interface SingleWorkRollup {
  name: string;
  subtotal: number;
  item_count: number;
  missing_unit_price_items: number;
  unit_works: UnitWorkRollup[];
}

/** 从 rollup（松类型）里安全取两级汇总树；无 ``single_works`` → null（旧口径/未算价）。 */
export function readHierarchy(
  rollup: Record<string, unknown> | null | undefined,
): SingleWorkRollup[] | null {
  const sw = rollup?.single_works;
  return Array.isArray(sw) && sw.length > 0 ? (sw as SingleWorkRollup[]) : null;
}

/** review-gate payload (末尾 review §13) — total cost overview, always pauses. */
export interface ReviewInterrupt {
  gate_type: "review";
  node: string;
  title: string;
  rollup: Record<string, unknown>;
  actions: string[];
}

export type CostInterrupt = ConfirmInterrupt | InputInterrupt | ReviewInterrupt;

/** Session response shape returned by /cost/session/{start,resume} (session._format). */
export interface CostSessionResponse {
  task_id: string;
  status: "awaiting_input" | "done" | "blocked" | "running" | string;
  interrupt: CostInterrupt | null;
  events: CostEvent[];
  items: Array<Record<string, unknown>>;
  overrides: Array<Record<string, unknown>>;
  audit_log: Array<Record<string, unknown>>;
  rates?: Record<string, unknown> | null;
  params?: Record<string, unknown> | null;
  rollup?: Record<string, unknown> | null;
}

/** confirm-gate user decision (原则 3：钉值，不重跑 LLM). */
export interface ConfirmDecision {
  action: "approve" | "select_alternative" | "manual_override";
  value?: string;
}

/** input/review decision is a plain field-value map (or {action:"approve"} for review). */
export type CostDecision = ConfirmDecision | Record<string, unknown>;
