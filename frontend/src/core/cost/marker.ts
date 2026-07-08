/**
 * Detects inline cost-HITL launch payloads so the chat can upgrade them into an
 * interactive gate widget instead of rendering a plain code block.
 *
 * Primary format (structured tool result / assistant content):
 *
 *     {"task_id": "abc123", "status": "awaiting_input", "interrupt": {...}}
 *
 * Legacy fallback format kept only for backward compatibility:
 *
 *     ```cost-hitl
 *     {"task_id": "abc123"}
 *     ```
 *
 * The widget then drives ``/api/cost/session/{task_id}/*`` directly — gate
 * interaction never passes back through the (weak) LLM, mirroring how structured
 * tool UIs render inline in the conversation.
 */

import type { Message } from "@langchain/langgraph-sdk";

import { extractTextFromMessage } from "@/core/messages/utils";

const COST_HITL_BLOCK = /```cost-hitl\s*\r?\n([\s\S]*?)```/;

export interface CostHitlMarker {
  taskId: string;
  /** Message content with the marker block stripped (may be empty). */
  cleaned: string;
}

/** Extract the legacy cost-HITL fallback block from a message; null if absent or malformed. */
export function extractCostLaunchPayload(content: string): CostHitlMarker | null {
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

/** 从结构化结果里抠出组价会话 task_id（结果可能是 JSON 字符串或已解析对象）。 */
export function hitlTaskIdFromLaunchResult(
  result?: string | Record<string, unknown> | null,
): string | null {
  if (!result) return null;
  let obj: Record<string, unknown> | undefined;
  if (typeof result === "string") {
    try {
      obj = JSON.parse(result) as Record<string, unknown>;
    } catch {
      return extractCostLaunchPayload(result)?.taskId ?? null;
    }
  } else {
    obj = result;
  }
  const taskId = obj?.task_id;
  if (typeof taskId === "string" && taskId) return taskId;
  const session = obj?.session;
  if (session && typeof session === "object") {
    const nestedTaskId = (session as Record<string, unknown>).task_id;
    if (typeof nestedTaskId === "string" && nestedTaskId) return nestedTaskId;
  }
  const marker = obj?.marker;
  if (typeof marker === "string") return extractCostLaunchPayload(marker)?.taskId ?? null;
  return null;
}

/** 扫描一个回合的消息，取出 legacy cost-hitl marker；当前 session 点火不再来自 MCP 工具结果。 */
export function extractLaunchTaskIdFromMessages(messages: Message[]): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (!message) continue;
    const marker = extractCostLaunchPayload(extractTextFromMessage(message));
    if (marker) return marker.taskId;
  }
  return null;
}

// Backward-compatible aliases kept for existing imports.
export const extractCostHitlMarker = extractCostLaunchPayload;
export const hitlTaskIdFromResult = hitlTaskIdFromLaunchResult;
export const extractHitlTaskIdFromMessages = extractLaunchTaskIdFromMessages;
