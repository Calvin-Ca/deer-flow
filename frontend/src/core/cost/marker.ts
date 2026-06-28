/**
 * Detects the inline cost-HITL marker that the cost-agent emits after starting a
 * session, so the chat can upgrade it into an interactive gate widget instead of
 * rendering a plain code block.
 *
 * Marker format (a fenced block the agent relays from ``cost.py start``):
 *
 *     ```cost-hitl
 *     {"task_id": "abc123"}
 *     ```
 *
 * The widget then drives ``/api/cost/session/{task_id}/*`` directly — gate
 * interaction never passes back through the (weak) LLM, mirroring how structured
 * tool UIs render inline in the conversation.
 */

const COST_HITL_BLOCK = /```cost-hitl\s*\r?\n([\s\S]*?)```/;

export interface CostHitlMarker {
  taskId: string;
  /** Message content with the marker block stripped (may be empty). */
  cleaned: string;
}

/** Extract the cost-HITL marker from a message; null if absent or malformed. */
export function extractCostHitlMarker(content: string): CostHitlMarker | null {
  const match = COST_HITL_BLOCK.exec(content);
  if (!match?.[1]) return null;
  try {
    const parsed = JSON.parse(match[1].trim()) as { task_id?: unknown };
    const taskId = typeof parsed.task_id === "string" ? parsed.task_id : null;
    if (!taskId) return null;
    return { taskId, cleaned: content.replace(match[0], "").trim() };
  } catch {
    return null;
  }
}
