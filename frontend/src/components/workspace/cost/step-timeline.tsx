"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  hasEvidence,
  statusBadge,
  stepLabel,
  stepSummary,
} from "@/core/cost/step-format";
import type { CostEvent } from "@/core/cost/types";
import { cn } from "@/lib/utils";

import { EvidenceCard } from "./gates";

/**
 * 组价步骤时间线 —— 把图逐节点产出的结构化 ``events`` 渲染成「清单匹配 → 套定额 → 取价 →
 * 单位工程汇总 → 单项工程汇总 → 总造价」的可视过程（HITL 设计 §8：只从结构化字段渲染，
 * 不解析模型自然语言）。每条 event = 一行：步骤标签 + 状态徽章 + 落值摘要 + 可展开依据卡；
 * 门控节点据 ``auto_pass`` 标「自动采纳」、据 ``confidence``/``tau`` 显示「置信 X ≥ 阈值 τ」。
 * 纯映射逻辑在 ``core/cost/step-format.ts``（可单测），本组件只做渲染。
 */

/** 单条步骤行（含可展开依据卡）。 */
function StepRow({ ev }: { ev: CostEvent }) {
  const [open, setOpen] = useState(false);
  const badge = statusBadge(ev);
  const summary = stepSummary(ev);
  const warn = ev.status === "blocked" || ev.status === "未就绪";
  const evidence = hasEvidence(ev);

  return (
    <div className="flex gap-2 text-xs">
      <span className={cn("mt-0.5 shrink-0", warn ? "text-destructive" : "text-emerald-600")}>
        {warn ? "!" : "✓"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium">{stepLabel(ev.step)}</span>
          {badge && (
            <Badge
              variant={badge.warn ? "outline" : "secondary"}
              className={cn("text-[10px]", badge.warn && "text-destructive")}
            >
              {badge.text}
            </Badge>
          )}
          {ev.confidence != null && ev.tau != null && (
            <span className="text-muted-foreground">
              置信 {ev.confidence} ≥ τ{ev.tau}
            </span>
          )}
          {summary && <span className="text-muted-foreground font-mono break-all">{summary}</span>}
          {evidence && (
            <button
              type="button"
              className="text-primary/70 hover:text-primary ml-auto shrink-0"
              onClick={() => setOpen((o) => !o)}
            >
              依据{open ? "▾" : "▸"}
            </button>
          )}
        </div>
        {open && evidence && (
          <div className="mt-1">
            <EvidenceCard
              sourceType={ev.provenance?.source_type}
              sourceRef={ev.provenance?.source_ref}
              confidence={ev.provenance?.confidence}
            />
          </div>
        )}
      </div>
    </div>
  );
}

export function CostStepTimeline({ events }: { events: CostEvent[] }) {
  if (!events || events.length === 0) return null;
  return (
    <div className="bg-muted/30 space-y-1.5 rounded-md border p-2">
      <div className="text-muted-foreground text-[11px] font-medium">
        执行过程 · 已完成 {events.length} 步
      </div>
      {events.map((ev, i) => (
        <StepRow key={`${ev.step ?? "step"}-${i}`} ev={ev} />
      ))}
    </div>
  );
}
