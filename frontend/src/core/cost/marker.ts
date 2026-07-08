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

/**
 * 从造价 MCP 工具结果里抠出组价会话 task_id（结果可能是 JSON 字符串或已解析对象）。
 * 覆盖两条点火路径：① 前门 ``ce-task_orchestrate_tool`` → ``{mode:"hitl", task_id}``；
 * ② 直连 ``ce-task_start_cost_session_tool`` → ``{task_id, status, interrupt, ui_hint}``。非 HITL 结果 → null。
 */
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

/**
 * 扫描一个回合的消息，取出被点火的组价 HITL 会话 task_id（优先结构化的 ce-task 工具**结果**，
 * 不依赖弱模型把启动信息贴进正文）。返回最后一个命中的 task_id，无则 null。用于把组价控件渲染到
 * 回合**末尾**（agent 文字之后），而非钉在工具调用处（中间过程块）导致方位错乱。
 */
export function extractLaunchTaskIdFromMessages(messages: Message[]): string | null {
  const resultByToolCallId = new Map<string, string>();
  for (const message of messages) {
    if (message.type !== "tool" || !message.tool_call_id) continue;
    // MCP 工具结果的 content 常是 blocks 数组（[{type:"text",…}]）而非纯字符串——与
    // findToolCallResult 同用 extractTextFromMessage 兼容两种形状。此前 typeof === "string"
    // 严判令 blocks 形态下 task_id 永远提不出、控件从不渲染（07-05 实测：三次 listing 点火
    // 服务端全成功、前端零控件；与 RouteContextMiddleware 踩的是同一类 content 形状坑）。
    const content = extractTextFromMessage(message);
    if (content) {
      resultByToolCallId.set(message.tool_call_id, content);
    }
  }
  let taskId: string | null = null;
  for (const message of messages) {
    if (message.type !== "ai" || !message.tool_calls) continue;
    for (const toolCall of message.tool_calls) {
      if (!toolCall.name?.startsWith("ce-task_")) continue;
      const result = toolCall.id ? resultByToolCallId.get(toolCall.id) : undefined;
      const id = hitlTaskIdFromLaunchResult(result);
      if (id) taskId = id;
    }
  }
  return taskId;
}

// Backward-compatible aliases kept for existing imports.
export const extractCostHitlMarker = extractCostLaunchPayload;
export const hitlTaskIdFromResult = hitlTaskIdFromLaunchResult;
export const extractHitlTaskIdFromMessages = extractLaunchTaskIdFromMessages;
