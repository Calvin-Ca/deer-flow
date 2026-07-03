import { describe, expect, it } from "vitest";

import {
  baseStep,
  hasEvidence,
  statusBadge,
  stepLabel,
  stepSummary,
} from "@/core/cost/step-format";
import type { CostEvent } from "@/core/cost/types";

describe("baseStep / stepLabel", () => {
  it("maps plain node names to Chinese labels", () => {
    expect(stepLabel("list_match")).toBe("清单匹配");
    expect(stepLabel("list_gate")).toBe("清单编码确认");
    expect(stepLabel("rollup")).toBe("造价汇总（总造价）");
  });

  it("strips [i] / :name suffixes for the base label and appends them", () => {
    expect(baseStep("compute_unit_price[0]")).toBe("compute_unit_price");
    expect(baseStep("unit_rollup:1#楼")).toBe("unit_rollup");
    expect(stepLabel("compute_unit_price[1]")).toBe("综合单价 · #1");
    expect(stepLabel("unit_rollup:1#楼")).toBe("单位工程汇总 · 1#楼");
    expect(stepLabel("single_rollup:1#住宅楼")).toBe("单项工程汇总 · 1#住宅楼");
  });

  it("falls back to the raw step for unknown names", () => {
    expect(stepLabel("mystery")).toBe("mystery");
    expect(stepLabel(undefined)).toBe("步骤");
  });
});

describe("statusBadge", () => {
  it("marks auto-passed gates as 自动采纳", () => {
    const ev: CostEvent = { step: "list_gate", status: "auto_pass", auto_pass: true, paused: false };
    expect(statusBadge(ev)).toEqual({ text: "自动采纳", warn: false });
  });

  it("marks paused gates as 人工确认", () => {
    const ev: CostEvent = { step: "quota_gate", status: "paused", paused: true };
    expect(statusBadge(ev)).toEqual({ text: "人工确认", warn: false });
  });

  it("flags warning statuses (未就绪 / blocked / need_review)", () => {
    expect(statusBadge({ step: "from_price_compose", status: "未就绪" })?.warn).toBe(true);
    expect(statusBadge({ step: "no_pricing", status: "blocked" })?.warn).toBe(true);
    expect(statusBadge({ step: "list_gate", status: "need_review" })?.warn).toBe(true);
  });

  it("shows no badge for a plain ok provenance event", () => {
    expect(statusBadge({ step: "list_match", status: "ok" })).toBeNull();
  });
});

describe("stepSummary", () => {
  it("summarizes coding / quota / quantity from result", () => {
    expect(stepSummary({ step: "list_gate", result: { code: "010502001001" } })).toBe(
      "编码 010502001001",
    );
    expect(stepSummary({ step: "quota_gate", result: { 子目号: "A4-8" } })).toBe("子目 A4-8");
    expect(stepSummary({ step: "quota_gate", result: { no_quota: true } })).toBe("无定额映射");
    expect(stepSummary({ step: "quantity_gate", result: { quantity: 8 } })).toBe("Q = 8");
  });

  it("summarizes 综合单价 and its missing states", () => {
    expect(
      stepSummary({ step: "compute_unit_price[0]", result: { unit_price: 612.34, total_price: 4898.72 } }),
    ).toContain("综合单价 612.34");
    expect(stepSummary({ step: "compute_unit_price[1]", result: { status: "missing_base" } })).toBe(
      "缺定额基价，未计价",
    );
    expect(
      stepSummary({ step: "compute_unit_price[1]", result: { status: "missing_quantity" } }),
    ).toBe("缺工程量，未计价");
  });

  it("summarizes the two-level rollup and total", () => {
    expect(stepSummary({ step: "unit_rollup:1#楼", result: { subtotal: 11967.92 } })).toBe(
      "分部分项合价 11967.92",
    );
    expect(stepSummary({ step: "single_rollup:1#住宅楼", result: { subtotal: 11967.92 } })).toBe(
      "分部分项合价 11967.92",
    );
    expect(stepSummary({ step: "rollup", result: { total: 13045.03, pre_tax_total: 11967.92 } })).toBe(
      "总造价 13045.03",
    );
  });

  it("falls back to source_ref when there is no result", () => {
    expect(
      stepSummary({ step: "from_price_compose", provenance: { source_ref: "深圳2024定额 A4-8" } }),
    ).toBe("深圳2024定额 A4-8");
  });
});

describe("hasEvidence", () => {
  it("is true when provenance carries a source or confidence", () => {
    expect(hasEvidence({ step: "list_match", provenance: { confidence: 0.86 } })).toBe(true);
    expect(hasEvidence({ step: "quota_gate", provenance: { source_ref: "A4-8" } })).toBe(true);
    expect(hasEvidence({ step: "quantity_gate", result: { quantity: 8 } })).toBe(false);
  });
});
