"use client";

import { Badge } from "@/components/ui/badge";
import { displayValue } from "@/core/cost/format";

/**
 * Read-only review table for multi-item cost sessions (M2 §3.1 pipeline ⑤, v0).
 *
 * Sequential-graph reality check: item N+1 has no candidate code until the
 * graph reaches it, and every paused gate is a low-confidence item that a
 * human SHOULD inspect individually — so v0 is a whole-session progress view
 * (code / confidence / status per item, current gate highlighted), not a
 * bulk-approve grid. True batch review needs the all-compute-then-gate graph
 * refactor (M3); the ``batch_resume`` endpoint already covers programmatic use.
 */

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
}

interface Row {
  feature: string;
  code: string;
  confidence: string;
  quantity: string;
  state: "confirmed" | "auto" | "current" | "pending" | "skipped";
}

const STATE_LABEL: Record<Row["state"], string> = {
  confirmed: "人工确认",
  auto: "自动采纳",
  current: "办理中",
  pending: "待处理",
  skipped: "未定码",
};

const STATE_BADGE: Record<Row["state"], "default" | "secondary" | "outline" | "destructive"> = {
  confirmed: "default",
  auto: "secondary",
  current: "destructive",
  pending: "outline",
  skipped: "outline",
};

function buildRow(
  item: Record<string, unknown>,
  idx: number,
  currentItem: number,
  sessionStatus: string,
): Row {
  const code = asRecord(item.code);
  // 两种形状都认：钉值后 provenance 在顶层；未过闸时藏在 envelope 里（与导出层同规）
  const prov =
    asRecord(code?.provenance) ?? asRecord(asRecord(code?.envelope)?.provenance);
  const codeValue = code?.value;
  const conf = prov?.confidence;
  const running = sessionStatus === "awaiting_input" || sessionStatus === "running";

  let state: Row["state"];
  if (running && idx === currentItem) {
    state = "current";
  } else if (running && idx > currentItem) {
    state = "pending";
  } else if (codeValue == null) {
    state = "skipped";
  } else if (code?.by === "user") {
    state = "confirmed";
  } else {
    state = "auto";
  }

  return {
    feature: typeof item.feature === "string" ? item.feature : displayValue(item.feature),
    code: codeValue == null ? "—" : displayValue(codeValue),
    confidence: typeof conf === "number" ? conf.toFixed(2) : "—",
    quantity: item.quantity == null ? "—" : displayValue(item.quantity),
    state,
  };
}

/** Whole-session item overview; rendered only for multi-item (batch) sessions. */
export function ReviewTable({
  items,
  currentItem,
  sessionStatus,
}: {
  items: Array<Record<string, unknown>>;
  currentItem: number;
  sessionStatus: string;
}) {
  if (items.length <= 1) return null;
  const rows = items.map((it, i) => buildRow(it, i, currentItem, sessionStatus));
  const done = rows.filter((r) => r.state === "confirmed" || r.state === "auto").length;

  return (
    <div className="rounded-md border">
      <div className="text-muted-foreground flex items-center justify-between border-b px-2 py-1 text-[11px]">
        <span>构件总览（{items.length} 件）</span>
        <span>
          已定码 {done} / {items.length}
        </span>
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-muted-foreground border-b text-left">
            <th className="px-2 py-1 font-normal">#</th>
            <th className="px-2 py-1 font-normal">构件</th>
            <th className="px-2 py-1 font-normal">编码</th>
            <th className="px-2 py-1 font-normal">置信</th>
            <th className="px-2 py-1 font-normal">工程量</th>
            <th className="px-2 py-1 font-normal">状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={i}
              className={r.state === "current" ? "bg-primary/5 border-b" : "border-b"}
            >
              <td className="text-muted-foreground px-2 py-1">{i + 1}</td>
              <td className="max-w-[200px] truncate px-2 py-1" title={r.feature}>
                {r.feature}
              </td>
              <td className="px-2 py-1 font-mono">{r.code}</td>
              <td className="px-2 py-1 font-mono">{r.confidence}</td>
              <td className="px-2 py-1 font-mono">{r.quantity}</td>
              <td className="px-2 py-1">
                <Badge variant={STATE_BADGE[r.state]} className="text-[10px]">
                  {STATE_LABEL[r.state]}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
