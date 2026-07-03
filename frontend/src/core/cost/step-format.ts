/**
 * Pure formatting for the cost step timeline — maps a structured ``CostEvent``
 * (emitted per graph node) to its Chinese step label, status badge, and a
 * compact value summary. Kept in ``core/`` (no React) so it's unit-testable and
 * the component (``components/workspace/cost/step-timeline.tsx``) stays a thin
 * renderer. HITL design §8: everything derives from structured fields, never
 * from model prose.
 */
import { displayValue } from "./format";
import type { CostEvent } from "./types";

/** 后端 step（节点名）→ 中文标签。动态后缀（``compute_unit_price[0]`` / ``price_item:0`` /
 * ``unit_rollup:1#楼``）先经 ``baseStep`` 归一，再查此表。 */
export const STEP_LABELS: Record<string, string> = {
  caliber: "口径声明",
  list_match: "清单匹配",
  feature_gate: "特征澄清",
  list_gate: "清单编码确认",
  from_price_compose: "套定额取数",
  quota_gate: "定额确认",
  price: "取价（信息价）",
  price_item: "取价补录",
  quantity_gate: "工程量",
  compute_unit_price: "综合单价",
  rates_gate: "综合单价费率",
  params_gate: "措施/规费/税金",
  unit_rollup: "单位工程汇总",
  single_rollup: "单项工程汇总",
  rollup: "造价汇总（总造价）",
  no_pricing: "未计价",
};

/** 剥掉动态后缀取基名：``compute_unit_price[0]``→``compute_unit_price``、``unit_rollup:1#楼``→``unit_rollup``。 */
export function baseStep(step: string | undefined): string {
  if (!step) return "";
  return step.replace(/\[.*$/, "").replace(/:.*$/, "");
}

/** 从动态 step 里抽出分组名/下标做行内后缀，如 ``单位工程汇总 · 1#楼``。 */
export function stepSuffix(step: string | undefined): string {
  if (!step) return "";
  const colon = step.indexOf(":");
  if (colon >= 0) return step.slice(colon + 1);
  const bracket = /\[(.+)\]/.exec(step);
  return bracket ? `#${bracket[1]}` : "";
}

export function stepLabel(step: string | undefined): string {
  const base = baseStep(step);
  const label = STEP_LABELS[base] ?? base ?? "步骤";
  const suffix = stepSuffix(step);
  return suffix ? `${label} · ${suffix}` : label;
}

/** 状态徽章文案 + 语气（warn=警示色）。null = 不显徽章（纯 ok 的 provenance 事件）。 */
export function statusBadge(ev: CostEvent): { text: string; warn: boolean } | null {
  const s = ev.status;
  if (s === "auto_pass" || (ev.auto_pass && !ev.paused)) return { text: "自动采纳", warn: false };
  if (ev.paused) return { text: "人工确认", warn: false };
  if (s === "ok") return null;
  if (s === "defaulted") return { text: "默认口径", warn: false };
  if (s === "need_review") return { text: "需复核", warn: true };
  if (s === "未就绪") return { text: "未就绪", warn: true };
  if (s === "blocked") return { text: "无法组价", warn: true };
  if (typeof s === "string" && s) return { text: s, warn: false };
  return null;
}

/** 一行落值摘要（从 ``result`` 结构化字段挑关键项，绝不拼模型散文）。 */
export function stepSummary(ev: CostEvent): string | null {
  const r = ev.result ?? null;
  const base = baseStep(ev.step);
  if (!r) {
    // 无 result 的节点（如 from_price_compose）退回 source_ref
    return ev.provenance?.source_ref ? String(ev.provenance.source_ref) : null;
  }
  const pick = (k: string): unknown => r[k];
  switch (base) {
    case "list_gate":
      return pick("code") != null ? `编码 ${displayValue(pick("code"))}` : null;
    case "quota_gate":
      if (pick("no_quota")) return "无定额映射";
      return pick("子目号") != null ? `子目 ${displayValue(pick("子目号"))}` : null;
    case "quantity_gate":
      return pick("quantity") != null ? `Q = ${displayValue(pick("quantity"))}` : null;
    case "compute_unit_price": {
      if (pick("status") === "missing_base") return "缺定额基价，未计价";
      if (pick("status") === "missing_quantity") return "缺工程量，未计价";
      const up = pick("unit_price");
      const tp = pick("total_price");
      return up != null ? `综合单价 ${displayValue(up)}　合价 ${displayValue(tp)}` : null;
    }
    case "unit_rollup":
    case "single_rollup":
      return pick("subtotal") != null ? `分部分项合价 ${displayValue(pick("subtotal"))}` : null;
    case "rollup": {
      const total = pick("total");
      const pre = pick("pre_tax_total");
      return total != null ? `总造价 ${displayValue(total)}` : `税前造价 ${displayValue(pre)}`;
    }
    case "caliber":
      return pick("spec_version") != null
        ? `${displayValue(pick("region"))}·${displayValue(pick("spec_version"))}`
        : null;
    case "no_pricing":
      return typeof pick("blocked_reason") === "string" ? String(pick("blocked_reason")) : null;
    default:
      return null;
  }
}

/** 是否有可展开依据（provenance 有来源/置信度）。 */
export function hasEvidence(ev: CostEvent): boolean {
  return (
    !!ev.provenance?.source_type ||
    !!ev.provenance?.source_ref ||
    ev.provenance?.confidence != null
  );
}
