import { describe, expect, it } from "vitest";

import {
  extractCostLaunchPayload,
  hitlTaskIdFromLaunchResult,
} from "@/core/cost/marker";

describe("cost HITL launch extraction", () => {
  it("prefers structured launch payloads with task_id", () => {
    expect(
      hitlTaskIdFromLaunchResult({
        task_id: "task-123",
        status: "awaiting_input",
        interrupt: { gate_type: "confirm", node: "list_gate" },
        ui_hint: { kind: "inline_session" },
      }),
    ).toBe("task-123");
  });

  it("accepts JSON strings from tool results", () => {
    expect(
      hitlTaskIdFromLaunchResult(
        JSON.stringify({
          task_id: "task-456",
          status: "awaiting_input",
          interrupt: { gate_type: "input", node: "setup" },
        }),
      ),
    ).toBe("task-456");
  });

  it("still supports the legacy fenced marker for backward compatibility", () => {
    const marker = "```cost-hitl\n{\"task_id\":\"task-789\"}\n```";
    expect(extractCostLaunchPayload(marker)?.taskId).toBe("task-789");
    expect(hitlTaskIdFromLaunchResult(marker)).toBe("task-789");
  });
});
