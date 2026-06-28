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

import { ConfirmGate, InputGate, ReviewGate } from "./gates";

/**
 * Inline cost-HITL widget rendered inside a chat message (triggered by the
 * ``cost-hitl`` marker). Bound to an existing session ``taskId``, it loads the
 * current gate via ``/api/cost/session/{id}/state`` and drives resume directly —
 * gate decisions are structured clicks, never round-tripped through the LLM.
 */
export function CostHitlInline({ taskId }: { taskId: string }) {
  const [gate, setGate] = useState<CostInterrupt | null>(null);
  const [status, setStatus] = useState<string>("loading");
  const [rollup, setRollup] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSessionState(taskId)
      .then((s) => {
        if (cancelled) return;
        setGate(s.interrupt);
        setStatus(s.status);
        setRollup((s.values?.rollup as Record<string, unknown>) ?? null);
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
        setGate(res.interrupt);
        setStatus(res.status);
        setRollup(res.rollup ?? null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "提交失败");
      } finally {
        setBusy(false);
      }
    },
    [taskId],
  );

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

        {!gate && status === "done" && (
          <div className="text-sm">
            <div className="text-muted-foreground mb-1">组价完成</div>
            <div className="flex justify-between font-semibold">
              <span>总造价</span>
              <span className="font-mono">{displayValue(rollup?.total)}</span>
            </div>
          </div>
        )}
        {!gate && status === "blocked" && (
          <div className="text-destructive text-sm">
            会话阻塞：未能定编码，需人工处理（红线：不硬编）。
          </div>
        )}
        {status === "loading" && (
          <div className="text-muted-foreground text-xs">加载会话状态…</div>
        )}
      </CardContent>
    </Card>
  );
}
