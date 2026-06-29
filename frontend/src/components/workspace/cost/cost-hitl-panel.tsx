"use client";

import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { resumeSession, startSession } from "@/core/cost/client";
import { displayValue } from "@/core/cost/format";
import type {
  CostDecision,
  CostEvent,
  CostSessionResponse,
} from "@/core/cost/types";

import { ConfirmGate, EvidenceCard, InputGate, ReviewGate } from "./gates";

const STATUS_VARIANT: Record<string, "default" | "destructive" | "secondary"> = {
  done: "default",
  blocked: "destructive",
};
const STATUS_LABEL: Record<string, string> = {
  awaiting_input: "等待输入",
  done: "已完成",
  blocked: "阻塞",
  running: "运行中",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "secondary"}>
      {STATUS_LABEL[status] ?? status}
    </Badge>
  );
}

/** Provenance event timeline — one 依据卡 per graph node (paused or not). */
function EventTimeline({ events }: { events: CostEvent[] }) {
  if (!events.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">依据时间线</CardTitle>
        <CardDescription>逐节点 provenance（共 {events.length} 条）</CardDescription>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-[360px] pr-3">
          <div className="space-y-2">
            {events.map((ev, i) => (
              <div key={i} className="rounded-md border p-2 text-xs">
                <div className="mb-1 flex items-center justify-between">
                  <span className="font-mono">{ev.step}</span>
                  <span className="flex items-center gap-1">
                    {ev.paused && (
                      <Badge variant="outline" className="text-[10px]">
                        暂停
                      </Badge>
                    )}
                    {ev.auto_pass && (
                      <Badge variant="secondary" className="text-[10px]">
                        自动采纳
                      </Badge>
                    )}
                    <span className="text-muted-foreground">{ev.status}</span>
                  </span>
                </div>
                {ev.auto_pass && ev.tau != null && ev.confidence != null && (
                  <div className="text-muted-foreground mb-1 text-[10px]">
                    置信 {ev.confidence.toFixed(2)} ≥ 阈值 {ev.tau.toFixed(2)}，故未打断、自动采纳
                  </div>
                )}
                <EvidenceCard
                  sourceType={ev.provenance?.source_type}
                  sourceRef={ev.provenance?.source_ref}
                  confidence={ev.provenance?.confidence ?? ev.confidence}
                />
              </div>
            ))}
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}

const SPEC_OPTIONS = ["2024", "2013"];

export function CostHitlPanel() {
  const [feature, setFeature] = useState("");
  const [spec, setSpec] = useState("2024");
  const [region, setRegion] = useState("深圳");
  const [session, setSession] = useState<CostSessionResponse | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleStart() {
    if (!feature.trim()) return;
    setBusy(true);
    try {
      const res = await startSession({
        feature: feature.trim(),
        spec,
        region: region.trim() || "深圳",
      });
      setSession(res);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "起会话失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleDecision(decision: CostDecision) {
    if (!session) return;
    setBusy(true);
    try {
      const res = await resumeSession(session.task_id, decision);
      setSession(res);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "提交失败");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setSession(null);
    setFeature("");
  }

  const interrupt = session?.interrupt ?? null;

  return (
    <div className="mx-auto w-full max-w-5xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">智能组价 · HITL</h1>
          <p className="text-muted-foreground text-sm">
            可中断组价编排：编码 / 定额 / 信息价 / 费率 / 参数 / 总造价六类闸，每数字带来源依据。
          </p>
        </div>
        {session && (
          <div className="flex items-center gap-2">
            <StatusBadge status={session.status} />
            <Button variant="outline" size="sm" onClick={reset}>
              新建会话
            </Button>
          </div>
        )}
      </div>

      {!session && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">起组价会话</CardTitle>
            <CardDescription>填构件/做法描述与口径，跑到首个需人介入的闸。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Textarea
              placeholder="构件/做法描述，如：C30 现浇矩形柱"
              value={feature}
              onChange={(e) => setFeature(e.target.value)}
            />
            <div className="flex flex-wrap items-center gap-3">
              <div className="space-y-1">
                <label className="text-muted-foreground text-xs">国标版本</label>
                <Select value={spec} onValueChange={setSpec}>
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SPEC_OPTIONS.map((s) => (
                      <SelectItem key={s} value={s}>
                        {s}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <label className="text-muted-foreground text-xs">地区</label>
                <Input
                  className="w-32"
                  value={region}
                  onChange={(e) => setRegion(e.target.value)}
                />
              </div>
            </div>
            <Button disabled={busy || !feature.trim()} onClick={handleStart}>
              开始
            </Button>
          </CardContent>
        </Card>
      )}

      {session && (
        <div className="grid gap-4 md:grid-cols-[1fr_360px]">
          <div className="space-y-4">
            {interrupt?.gate_type === "confirm" && (
              <ConfirmGate
                interrupt={interrupt}
                busy={busy}
                onDecide={handleDecision}
              />
            )}
            {interrupt?.gate_type === "input" && (
              <InputGate
                interrupt={interrupt}
                busy={busy}
                onSubmit={handleDecision}
              />
            )}
            {interrupt?.gate_type === "review" && (
              <ReviewGate
                interrupt={interrupt}
                busy={busy}
                onApprove={() => handleDecision({ action: "approve" })}
              />
            )}
            {!interrupt && session.status === "done" && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">组价完成</CardTitle>
                  <CardDescription>
                    总造价 {displayValue(session.rollup?.total)}
                  </CardDescription>
                </CardHeader>
                <CardContent className="text-muted-foreground text-sm">
                  审计 {session.audit_log.length} 条 · override {session.overrides.length} 条
                </CardContent>
              </Card>
            )}
            {!interrupt && session.status === "blocked" && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-base text-destructive">会话阻塞</CardTitle>
                  <CardDescription>
                    选不出码且无人工覆盖 —— 交人工处理（红线：不硬编）。
                  </CardDescription>
                </CardHeader>
              </Card>
            )}
          </div>
          <EventTimeline events={session.events} />
        </div>
      )}
    </div>
  );
}
