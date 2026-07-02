"use client";

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getSessionState, resumeSession } from "@/core/cost/client";
import { displayValue } from "@/core/cost/format";
import type { CostDecision, CostInterrupt } from "@/core/cost/types";

import { ConfirmGate, InputGate, ReviewGate, ROLLUP_LABELS } from "./gates";

/** Normalized snapshot the widget renders from (works for both state + resume shapes). */
interface Snapshot {
  status: string;
  gate: CostInterrupt | null;
  rollup: Record<string, unknown> | null;
  items: Array<Record<string, unknown>>;
  overrides: Array<Record<string, unknown>>;
  auditCount: number;
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
}

/** Pull the value out of a locked field ``{value, locked, ...}``. */
function lockedValue(field: unknown): unknown {
  return asRecord(field)?.value;
}

/** 完成后的可复核明细：总造价构成 + 已确认编码/定额 + 录入/覆盖项（重开对话也能看）。 */
function DoneSummary({ snap }: { snap: Snapshot }) {
  const item = snap.items[0] ?? {};
  const code = lockedValue(item.code);
  const quota = lockedValue(item.quota);
  const r = snap.rollup ?? {};
  return (
    <div className="space-y-2 text-sm">
      <div className="text-muted-foreground">组价完成 · 已确认明细</div>

      <div className="space-y-1">
        {Object.entries(ROLLUP_LABELS).map(([k, label]) => {
          const v = r[k];
          if (v == null) return null;
          return (
            <div
              key={k}
              className={
                k === "total"
                  ? "flex justify-between border-t pt-1 font-semibold"
                  : "flex justify-between"
              }
            >
              <span className="text-muted-foreground">{label}</span>
              <span className="font-mono">{displayValue(v)}</span>
            </div>
          );
        })}
      </div>

      <div className="text-muted-foreground space-y-0.5 border-t pt-1 text-xs">
        <div>清单编码：<span className="font-mono">{displayValue(code)}</span></div>
        <div>定额子目：<span className="font-mono">{displayValue(quota)}</span></div>
      </div>

      {snap.overrides.length > 0 && (
        <div className="border-t pt-1 text-xs">
          <div className="text-muted-foreground mb-0.5">录入/覆盖（{snap.overrides.length}）</div>
          {snap.overrides.map((o, i) => (
            <div key={i} className="flex justify-between">
              <span className="text-muted-foreground">{displayValue(o.node)}</span>
              <span className="font-mono">{displayValue(o.value)}</span>
            </div>
          ))}
        </div>
      )}
      <div className="text-muted-foreground text-[11px]">审计 {snap.auditCount} 条 · 可追溯</div>
    </div>
  );
}

/**
 * Inline cost-HITL widget rendered inside a chat message (triggered by the
 * ``cost-hitl`` marker). Bound to an existing session ``taskId``, it loads the
 * current gate via ``/ce-cost/session/{id}/state`` and drives resume directly —
 * gate decisions are structured clicks, never round-tripped through the LLM.
 * After done it shows a reviewable summary (rebuilt from persisted state on reload).
 */
export function CostHitlInline({ taskId }: { taskId: string }) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSessionState(taskId)
      .then((s) => {
        if (cancelled) return;
        const v = s.values ?? {};
        setSnap({
          status: s.status,
          gate: s.interrupt,
          rollup: asRecord(v.rollup),
          items: (v.items as Array<Record<string, unknown>>) ?? [],
          overrides: (v.overrides as Array<Record<string, unknown>>) ?? [],
          auditCount: Array.isArray(v.audit_log) ? v.audit_log.length : 0,
        });
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "加载会话失败");
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  const decide = useCallback(
    async (decision: CostDecision) => {
      setBusy(true);
      setError(null);
      try {
        const res = await resumeSession(taskId, decision);
        setSnap({
          status: res.status,
          gate: res.interrupt,
          rollup: asRecord(res.rollup),
          items: res.items ?? [],
          overrides: res.overrides ?? [],
          auditCount: res.audit_log?.length ?? 0,
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "提交失败");
      } finally {
        setBusy(false);
      }
    },
    [taskId],
  );

  const status = snap?.status ?? "loading";
  const gate = snap?.gate ?? null;

  return (
    <Card className="border-primary/30 my-2">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          智能组价 · HITL
          <Badge variant="secondary" className="text-[10px]">
            {status === "awaiting_input" ? "等待确认" : status === "done" ? "已完成" : status}
          </Badge>
        </CardTitle>
        <CardDescription className="text-[11px]">task {taskId.slice(0, 8)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {error && <div className="text-destructive text-xs">{error}</div>}

        {gate?.gate_type === "confirm" && (
          <ConfirmGate interrupt={gate} busy={busy} onDecide={decide} />
        )}
        {gate?.gate_type === "input" && (
          <InputGate interrupt={gate} busy={busy} onSubmit={decide} />
        )}
        {gate?.gate_type === "review" && (
          <ReviewGate
            interrupt={gate}
            busy={busy}
            onApprove={() => decide({ action: "approve" })}
          />
        )}

        {!gate && status === "done" && snap && <DoneSummary snap={snap} />}
        {!gate && status === "blocked" && (
          <div className="text-destructive text-sm">
            {typeof snap?.rollup?.blocked_reason === "string"
              ? snap.rollup.blocked_reason
              : "会话阻塞：未能定编码，需人工处理（红线：不硬编）。"}
          </div>
        )}
        {status === "loading" && (
          <div className="text-muted-foreground text-xs">加载会话状态…</div>
        )}
      </CardContent>
    </Card>
  );
}
