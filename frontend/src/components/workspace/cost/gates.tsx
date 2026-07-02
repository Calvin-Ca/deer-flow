"use client";

import { useState } from "react";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { displayValue } from "@/core/cost/format";
import type {
  ConfirmDecision,
  ConfirmInterrupt,
  InputField,
  InputInterrupt,
  ReviewInterrupt,
} from "@/core/cost/types";

/** 依据卡 — renders a primitive's structured provenance (source + confidence). */
export function EvidenceCard({
  sourceType,
  sourceRef,
  confidence,
}: {
  sourceType?: string | null;
  sourceRef?: string | null;
  confidence?: number | null;
}) {
  if (!sourceType && !sourceRef && confidence == null) return null;
  return (
    <div className="bg-muted/40 text-muted-foreground rounded-md border p-2 text-xs">
      <div className="mb-1 font-medium">依据</div>
      {sourceType && (
        <div>
          来源类型：<span className="font-mono">{sourceType}</span>
        </div>
      )}
      {sourceRef && (
        <div className="break-all">
          来源引用：<span className="font-mono">{sourceRef}</span>
        </div>
      )}
      {confidence != null && <div>置信度：{confidence}</div>}
    </div>
  );
}

/** 确认型闸（编码 / 定额）—— proposal + 依据 + 备选，✓/选备选/手改。 */
export function ConfirmGate({
  interrupt,
  busy,
  onDecide,
}: {
  interrupt: ConfirmInterrupt;
  busy: boolean;
  onDecide: (d: ConfirmDecision) => void;
}) {
  const [override, setOverride] = useState("");
  const proposalEntries = Object.entries(interrupt.proposal ?? {}).filter(
    ([, v]) => v != null && typeof v !== "object",
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>{interrupt.title}</CardTitle>
        <CardDescription>确认型闸 · {interrupt.node}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1 text-sm">
          {proposalEntries.map(([k, v]) => (
            <div key={k}>
              <span className="text-muted-foreground">{k}：</span>
              <span className="font-mono">{String(v)}</span>
            </div>
          ))}
        </div>

        <EvidenceCard
          sourceType={interrupt.evidence?.source_type}
          sourceRef={interrupt.evidence?.source_ref}
          confidence={interrupt.evidence?.confidence}
        />

        {interrupt.alternatives?.length > 0 && (
          <div className="space-y-2">
            <div className="text-muted-foreground text-xs font-medium">
              备选（候选内）
            </div>
            {interrupt.alternatives.map((alt, i) => (
              <div
                key={`${alt.code ?? i}`}
                className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
              >
                <span>
                  <span className="font-mono">{alt.code}</span>
                  {alt.name ? ` ${alt.name}` : ""}
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy || !alt.code}
                  onClick={() =>
                    onDecide({
                      action: "select_alternative",
                      value: String(alt.code),
                    })
                  }
                >
                  选此备选
                </Button>
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            disabled={busy}
            onClick={() => onDecide({ action: "approve" })}
          >
            ✓ 通过
          </Button>
          <Input
            className="w-44"
            placeholder="手动改值（编码/子目号）"
            value={override}
            onChange={(e) => setOverride(e.target.value)}
          />
          <Button
            variant="secondary"
            disabled={busy || !override.trim()}
            onClick={() =>
              onDecide({ action: "manual_override", value: override.trim() })
            }
          >
            手动覆盖
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function FieldControl({
  field,
  value,
  onChange,
}: {
  field: InputField;
  value: string;
  onChange: (v: string) => void;
}) {
  if (field.type === "enum") {
    return (
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger className="w-full">
          <SelectValue placeholder={`选择${field.label}`} />
        </SelectTrigger>
        <SelectContent>
          {(field.options ?? []).map((opt) => (
            <SelectItem key={opt} value={opt}>
              {opt}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  const htmlType =
    field.type === "number" || field.type === "month" ? field.type : "text";
  return (
    <Input
      type={htmlType}
      value={value}
      placeholder={field.label}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** 录入型闸（setup / 缺价 / 费率 / 参数）—— 按字段类型渲染，提交字段值 dict。 */
/** Friendly Chinese labels for input-gate context keys (feature-clarify / 缺价 闸). */
const CONTEXT_LABELS: Record<string, string> = {
  feature: "当前描述",
  code: "已选编码",
  unit: "单位",
  raw: "材料",
  std: "标准材料",
  category: "类别",
  spec: "规格",
  consumption: "消耗量",
  price_status: "信息价状态",
  source_ref: "来源",
};

export function InputGate({
  interrupt,
  busy,
  onSubmit,
}: {
  interrupt: InputInterrupt;
  busy: boolean;
  onSubmit: (values: Record<string, unknown>) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      interrupt.fields.map((f) => [
        f.key,
        f.default != null ? displayValue(f.default) : "",
      ]),
    ),
  );

  const missingRequired = interrupt.fields.some(
    (f) => f.required && !values[f.key]?.trim(),
  );

  function submit() {
    const out: Record<string, unknown> = {};
    for (const f of interrupt.fields) {
      const raw = values[f.key]?.trim() ?? "";
      if (raw === "") continue;
      out[f.key] = f.type === "number" ? Number(raw) : raw;
    }
    onSubmit(out);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{interrupt.title}</CardTitle>
        <CardDescription>录入型闸 · {interrupt.node}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {typeof interrupt.context?.message === "string" && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs">
            {interrupt.context.message}
          </div>
        )}
        {interrupt.context && (
          <div className="bg-muted/40 space-y-1 rounded-md border p-2 text-xs">
            {Object.entries(interrupt.context)
              .filter(([k, v]) => k !== "why" && k !== "message" && v != null && v !== "")
              .map(([k, v]) => (
                <div key={k}>
                  <span className="text-muted-foreground">
                    {CONTEXT_LABELS[k] ?? k}：
                  </span>
                  <span className="font-mono">{displayValue(v)}</span>
                </div>
              ))}
            {Array.isArray(interrupt.context.why) && (
              <div className="space-y-0.5">
                <div className="text-muted-foreground">为什么要补这些：</div>
                {(
                  (interrupt.context as {
                    why: Array<{ label?: string; why?: string }>;
                  }).why
                ).map((w, i) => (
                  <div key={i} className="pl-2">
                    · {w.label && <span className="font-medium">{w.label}</span>}
                    {w.why && (
                      <span className="text-muted-foreground"> —— {w.why}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        {interrupt.fields.map((f) => (
          <div key={f.key} className="space-y-1">
            <label className="text-muted-foreground text-xs">
              {f.label}
              {f.required && <span className="text-destructive"> *</span>}
            </label>
            <FieldControl
              field={f}
              value={values[f.key] ?? ""}
              onChange={(v) => setValues((s) => ({ ...s, [f.key]: v }))}
            />
          </div>
        ))}
        <Button disabled={busy || missingRequired} onClick={submit}>
          提交
        </Button>
      </CardContent>
    </Card>
  );
}

export const ROLLUP_LABELS: Record<string, string> = {
  subtotal: "分部分项合价",
  measure_fee: "措施项目费",
  other_fee: "其他项目费",
  fee_levy: "规费",
  pre_tax_total: "税前造价",
  tax: "税金",
  total: "总造价",
};

/** 末尾 review 闸（§13）—— 总造价明细 + 复核通过。 */
export function ReviewGate({
  interrupt,
  busy,
  onApprove,
}: {
  interrupt: ReviewInterrupt;
  busy: boolean;
  onApprove: () => void;
}) {
  const r = interrupt.rollup ?? {};
  const missing = r.missing_unit_price_items;
  const missingCount = typeof missing === "number" ? missing : 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{interrupt.title}</CardTitle>
        <CardDescription>末尾复核 · {interrupt.node}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1 text-sm">
          {Object.entries(ROLLUP_LABELS).map(([k, label]) => {
            const v = r[k];
            if (v == null) return null;
            const emphasize = k === "total";
            return (
              <div
                key={k}
                className={
                  emphasize
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
        {missingCount > 0 && (
          <Badge variant="outline" className="text-destructive">
            {missingCount} 条子目缺综合单价（未计入）
          </Badge>
        )}
        <Button disabled={busy} onClick={onApprove}>
          ✓ 复核通过，定稿
        </Button>
      </CardContent>
    </Card>
  );
}
