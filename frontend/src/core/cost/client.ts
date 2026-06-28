import type {
  CostDecision,
  CostInterrupt,
  CostSessionResponse,
} from "./types";

/**
 * Thin client for the cost HITL session API, going through the same-origin
 * Next.js proxy at ``/ce-cost/*`` (see ``app/ce-cost/[...path]/route.ts``),
 * which forwards to ce-services :8101. Deliberately off ``/api/*`` — next.config
 * rewrites ``/api/:path*`` to the gateway, which would shadow an /api proxy route.
 */
const BASE = "/ce-cost";

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`cost API ${path} failed (${res.status}): ${detail}`);
  }
  return (await res.json()) as T;
}

/** Start a HITL session; runs to the first gate or done. */
export interface StartSessionInput {
  feature: string;
  spec?: string;
  region?: string;
  period?: string;
  price_source?: string;
  rates?: Record<string, unknown>;
}

export function startSession(
  input: StartSessionInput,
): Promise<CostSessionResponse> {
  return postJSON<CostSessionResponse>("session/start", input);
}

/** Resume a paused session with the user's gate decision. */
export function resumeSession(
  taskId: string,
  decision: CostDecision,
): Promise<CostSessionResponse> {
  return postJSON<CostSessionResponse>(`session/${taskId}/resume`, { decision });
}

/** Persisted session state (incl. the currently pending gate, for resuming by task_id). */
export interface CostSessionState {
  task_id: string;
  status: string;
  interrupt: CostInterrupt | null;
  next: string[];
  values: Record<string, unknown>;
}

/** Read the persisted session state without advancing the graph. */
export async function getSessionState(
  taskId: string,
): Promise<CostSessionState> {
  const res = await fetch(`${BASE}/session/${taskId}/state`);
  if (!res.ok) {
    throw new Error(`cost state failed (${res.status})`);
  }
  return (await res.json()) as CostSessionState;
}
