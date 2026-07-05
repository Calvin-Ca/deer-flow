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

import type { Message } from "@langchain/langgraph-sdk";

import { extractTextFromMessage } from "@/core/messages/utils";

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

/**
 * 从造价 MCP 工具结果里抠出组价 HITL 会话 task_id（结果可能是 JSON 字符串或已解析对象）。
 * 覆盖两条点火路径：① 前门 ``ce-task_orchestrate`` → ``{mode:"hitl", task_id}``；
 * ② 直连 ``ce-task_start_cost_session`` → ``{task_id, marker}``。非 HITL 结果 → null。
 */
export function hitlTaskIdFromResult(
  result?: string | Record<string, unknown> | null,
): string | null {
  if (!result) return null;
  let obj: Record<string, unknown> | undefined;
  if (typeof result === "string") {
    try {
      obj = JSON.parse(result) as Record<string, unknown>;
    } catch {
      return extractCostHitlMarker(result)?.taskId ?? null;
    }
  } else {
    obj = result;
  }
  const taskId = obj?.task_id;
  if (typeof taskId === "string" && taskId) return taskId;
  const marker = obj?.marker;
  if (typeof marker === "string") return extractCostHitlMarker(marker)?.taskId ?? null;
  return null;
}

/**
 * 扫描一个回合的消息，取出被点火的组价 HITL 会话 task_id（优先结构化的 ce-task 工具**结果**，
 * 不依赖弱模型把 marker 贴进正文）。返回最后一个命中的 task_id，无则 null。用于把组价控件渲染到
 * 回合**末尾**（agent 文字之后），而非钉在工具调用处（中间过程块）导致「卡上、字下」方位错乱。
 */
export function extractHitlTaskIdFromMessages(messages: Message[]): string | null {
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
      const id = hitlTaskIdFromResult(result);
      if (id) taskId = id;
    }
  }
  return taskId;
}
