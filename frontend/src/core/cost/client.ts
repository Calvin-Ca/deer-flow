import type {
  CostDecision,
  CostEvent,
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

/**
 * One SSE message from the streaming session endpoints (mirrors
 * ``ce-services/cost/session.py`` ``_run_stream``). The graph runs node-by-node
 * and each step is pushed as it completes, so the timeline reveals live.
 */
export type CostStreamMessage =
  | { type: "start"; task_id: string }
  | { type: "event"; event: CostEvent }
  | { type: "interrupt"; gate: CostInterrupt }
  | {
      type: "done";
      status: string;
      rollup?: Record<string, unknown> | null;
      items?: Array<Record<string, unknown>>;
      overrides?: Array<Record<string, unknown>>;
      audit_count?: number;
    }
  | { type: "error"; detail: string };

/** Read an SSE (``text/event-stream``) body, parsing each ``data:`` frame as JSON. */
async function consumeSSE(
  res: Response,
  onMessage: (msg: CostStreamMessage) => void,
): Promise<void> {
  if (!res.ok || !res.body) {
    const detail = res.body ? await res.text() : "";
    throw new Error(`cost stream failed (${res.status})${detail ? `: ${detail}` : ""}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; parse each complete frame.
    for (;;) {
      const sep = buffer.indexOf("\n\n");
      if (sep === -1) break;
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload) onMessage(JSON.parse(payload) as CostStreamMessage);
      }
    }
  }
}

/** Resume a paused session, streaming each node's step as it completes. */
export async function streamResume(
  taskId: string,
  decision: CostDecision,
  onMessage: (msg: CostStreamMessage) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/session/${taskId}/resume/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  await consumeSSE(res, onMessage);
}

/** Start a session, streaming each node's step as it completes. */
export async function streamStart(
  input: StartSessionInput,
  onMessage: (msg: CostStreamMessage) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/session/start/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  await consumeSSE(res, onMessage);
}
