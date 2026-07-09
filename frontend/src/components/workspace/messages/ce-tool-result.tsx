/**
 * 造价领域 MCP 工具的中间过程渲染（ce-rag_* / ce-db_*）。
 *
 * 背景：deer-flow 原生 toolCall 流的泛型分支只画一行 label、不渲染工具结果（见 message-group.tsx
 * 的 `else` 分支）。造价取数原语的工具名/入参/结果天然结构化，这里按**稳定的工具名**派发，把
 * 领域结果（cited_clauses / 选码+置信度 / 候选 / 取数）渲染成
 * 折叠中间过程里的依据条目——让造价用户在对话里就看见「凭什么这么答」，而不是埋在 bash stdout 里。
 *
 * 设计：领域逻辑全部收在本文件，上游 `message-group.tsx` 只加一个 `isCeTool(name)` 委派分支，
 * 不把造价业务字段塞进通用组件。结果形状做防御性读取（MCP 序列化/加载中/异常透传都不崩）。
 */
import {
  CircleHelpIcon,
  DatabaseIcon,
  FileSearchIcon,
  ListChecksIcon,
  ScrollTextIcon,
} from "lucide-react";

import {
  ChainOfThoughtSearchResult,
  ChainOfThoughtSearchResults,
  ChainOfThoughtStep,
} from "@/components/ai-elements/chain-of-thought";
import { cn } from "@/lib/utils";

/** 本组件能渲染的造价领域 MCP 工具前缀。 */
export function isCeTool(name: string): boolean {
  return (
    name.startsWith("ce-rag_") ||
    name.startsWith("ce-db_") ||
    name.startsWith("cost_workflow_")
  );
}

type Rec = Record<string, unknown>;

function asRec(v: unknown): Rec | undefined {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Rec) : undefined;
}
function asArr(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}
function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}
function num(v: unknown): number | undefined {
  return typeof v === "number" && !Number.isNaN(v) ? v : undefined;
}
/** 折叠卡里只显原文片段，截断到 n 字（完整原文在最终答案/可点开溯源里）。 */
function clip(s: string, n = 140): string {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n) + "…" : t;
}

/** 把任意工具 result（string / object / 加载中 undefined）规整成 object。 */
function resultObj(result?: string | Rec): Rec | undefined {
  if (!result) return undefined;
  if (typeof result === "string") {
    try {
      return asRec(JSON.parse(result));
    } catch {
      return undefined;
    }
  }
  return asRec(result);
}

export function CeToolResult({
  name,
  args,
  result,
}: {
  name: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
}) {
  const r = resultObj(result);
  if (name.startsWith("cost_workflow_")) {
    return <CostWorkflow name={name} args={args} r={r} />;
  }
  const tool = name.replace(/^ce-(rag|db)_/, "");

  switch (tool) {
    case "search_clause":
      return <NormQa args={args} r={r} />;
    case "match_bill_item":
      return <BillMatch args={args} r={r} />;
    case "price_compose":
      return <PriceCompose args={args} r={r} />;
    case "quota_get":
      return <QuotaLookup args={args} r={r} />;
    case "price_query":
    case "bill_get":
    case "search_aux_table":
    case "search_price_rule":
    case "retrieve_evidence":
    case "fee_rate_lookup":
    case "price_composition_get":
    case "aux_table_get":
    case "aux_table_list":
    case "resource_lookup":
    case "bill_list":
    case "quota_list":
      return (
        <ChainOfThoughtStep icon={DatabaseIcon} label={`造价工具：${tool}`}>
          {Array.isArray(r?.evidence) && r.evidence.length > 0 && (
            <ChainOfThoughtSearchResults>
              {r.evidence.slice(0, 6).map((item, i) => {
                const rec = asRec(item);
                const title = str(rec?.title) ?? str(rec?.snippet) ?? "命中";
                return (
                  <ChainOfThoughtSearchResult key={i}>
                    {title}
                  </ChainOfThoughtSearchResult>
                );
              })}
            </ChainOfThoughtSearchResults>
          )}
        </ChainOfThoughtStep>
      );
    default:
      return (
        <ChainOfThoughtStep icon={DatabaseIcon} label={`造价工具：${tool}`} />
      );
  }
}

function CostWorkflow({
  name,
  args,
  r,
}: {
  name: string;
  args: Rec;
  r?: Rec;
}) {
  const taskId = str(r?.task_id);
  const status = str(r?.status) ?? str(r?.node) ?? "workflow";
  const interrupt = asRec(r?.interrupt);
  const gateType = str(interrupt?.gate_type);
  const node = str(interrupt?.node) ?? str(r?.node);
  const label = name.replace(/^cost_workflow_/, "造价 workflow：");
  const desc =
    r === undefined
      ? "执行中…"
      : [status, taskId, gateType ? `待确认：${gateType}` : undefined, node]
          .filter(Boolean)
          .join(" · ");
  const question = str(interrupt?.question);
  const argFeature = str(args.feature) ?? str(args.node);
  // 计算过程 breakdown（unit_rate 等确定性计算返回）：顶层或 result 内
  const breakdown = asArr(r?.breakdown ?? asRec(r?.result)?.breakdown)
    .map(asRec)
    .filter((x): x is Rec => !!x);

  return (
    <ChainOfThoughtStep icon={ListChecksIcon} label={label} description={desc}>
      {(argFeature !== undefined || question !== undefined) && (
        <div className="text-muted-foreground space-y-1 text-xs">
          {argFeature && <div>{argFeature}</div>}
          {question && <div>{question}</div>}
        </div>
      )}
      {breakdown.length > 0 && <CalcBreakdown rows={breakdown} />}
    </ChainOfThoughtStep>
  );
}

/** 综合单价等确定性计算的逐步过程展示（人材机费 → 管理费/利润 → 综合单价，每步算式+金额）。 */
function CalcBreakdown({ rows }: { rows: Rec[] }) {
  return (
    <div className="mt-1 space-y-0.5 text-xs">
      {rows.map((row, i) => {
        const item = str(row.item);
        const formula = str(row.formula);
        const amount = num(row.amount);
        // 末行是目标值（综合单价 / 清单合价 / 总造价…）→ 加粗分隔
        const isFinal = i === rows.length - 1;
        return (
          <div
            key={i}
            className={cn(
              "flex items-baseline justify-between gap-3",
              isFinal && "border-border/60 mt-0.5 border-t pt-0.5 font-medium",
            )}
          >
            <span className="text-foreground shrink-0">{item}</span>
            {formula && (
              <span className="text-muted-foreground flex-1 truncate text-[11px]">
                {formula}
              </span>
            )}
            <span className="tabular-nums shrink-0">{amount?.toFixed(2)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 规范问答 / 条文检索：展示命中条文数 + cited_clauses 或 evidence。 */
function NormQa({ args, r }: { args: Rec; r?: Rec }) {
  const query = str(args.query) ?? "规范问答";
  const retrieved = num(asRec(r?.meta)?.retrieved);
  const cited = asArr(r?.cited_clauses)
    .map(asRec)
    .filter((c): c is Rec => !!c);
  const evidenceClauses =
    cited.length > 0
      ? cited
      : asArr(r?.evidence)
          .map(asRec)
          .filter((c): c is Rec => !!c)
          .map((item) => ({
            standard: asRec(item.metadata)?.standard,
            clause: asRec(item.metadata)?.node_path,
            text: item.snippet,
          }));
  const desc =
    r === undefined
      ? "检索中…"
      : retrieved === 0 || evidenceClauses.length === 0
        ? "未检索到相关条文"
        : `命中 ${retrieved ?? evidenceClauses.length} 条，引用 ${evidenceClauses.length} 条`;
  return (
    <ChainOfThoughtStep
      icon={ScrollTextIcon}
      label={`规范问答：${query}`}
      description={desc}
    >
      <ClauseList clauses={evidenceClauses} />
    </ChainOfThoughtStep>
  );
}

/** cited_clauses 片段列表（标准号 + 条文号 + 原文截断）；norm 与编排前门复用。 */
function ClauseList({ clauses }: { clauses: Rec[] }) {
  if (clauses.length === 0) return null;
  return (
    <div className="space-y-2">
      {clauses.slice(0, 6).map((c, i) => {
        const head =
          [str(c.standard), str(c.clause)].filter(Boolean).join(" ") || "条文";
        const text = str(c.text);
        const contextual = str(c.relevance) === "contextual";
        return (
          <div
            key={i}
            className="border-primary/50 bg-muted/40 rounded-r-md border-l-2 py-1.5 pr-2 pl-2.5"
          >
            <div className="text-muted-foreground mb-0.5 flex items-center gap-1.5 text-[11px]">
              <span className="font-mono">{head}</span>
              {contextual && (
                <span className="bg-background/70 rounded px-1 text-[10px]">
                  背景参考
                </span>
              )}
            </div>
            {text && (
              <p className="text-foreground text-xs leading-relaxed">
                {clip(text)}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** 清单候选召回：候选数 + Top 候选（编码 + 名称）。 */
function BillMatch({ args, r }: { args: Rec; r?: Rec }) {
  const query = str(args.description) ?? str(args.query) ?? "清单候选召回";
  const count = num(r?.count);
  const candidates = asArr(r?.candidates)
    .map(asRec)
    .filter((c): c is Rec => !!c);
  return (
    <ChainOfThoughtStep
      icon={ListChecksIcon}
      label={`清单候选召回：${query}`}
      description={r === undefined ? "召回中…" : `${count ?? candidates.length} 个候选`}
    >
      {candidates.length > 0 && (
        <ChainOfThoughtSearchResults>
          {candidates.slice(0, 8).map((c, i) => (
            <ChainOfThoughtSearchResult key={i}>
              {[str(c.code), str(c.name)].filter(Boolean).join(" ") || "候选"}
            </ChainOfThoughtSearchResult>
          ))}
        </ChainOfThoughtSearchResults>
      )}
    </ChainOfThoughtStep>
  );
}

/** 组价取数：清单码 + 定额子目数 + 各子目编码。 */
function PriceCompose({ args, r }: { args: Rec; r?: Rec }) {
  const code = str(args.code) ?? "组价取数";
  const quotas = asArr(r?.quotas)
    .map(asRec)
    .filter((q): q is Rec => !!q);
  return (
    <ChainOfThoughtStep
      icon={FileSearchIcon}
      label={`组价取数：${code}`}
      description={
        r === undefined
          ? "取数中…"
          : quotas.length === 0
            ? "无映射定额子目"
            : `${quotas.length} 条定额子目`
      }
    >
      {quotas.length > 0 && (
        <ChainOfThoughtSearchResults>
          {quotas.slice(0, 8).map((q, i) => (
            <ChainOfThoughtSearchResult key={i}>
              {[str(q.quota_code), str(q.name)].filter(Boolean).join(" ") ||
                "子目"}
            </ChainOfThoughtSearchResult>
          ))}
        </ChainOfThoughtSearchResults>
      )}
    </ChainOfThoughtStep>
  );
}

/** 定额直取：子目编码 + 工料机条数。 */
function QuotaLookup({ args, r }: { args: Rec; r?: Rec }) {
  const code = str(args.code) ?? "定额直取";
  const item = asRec(r?.item);
  const resources = asArr(r?.resources);
  const name = str(item?.name) ?? str(item?.["名称"]);
  return (
    <ChainOfThoughtStep
      icon={CircleHelpIcon}
      label={`定额直取：${code}`}
      description={
        r === undefined ? "查询中…" : `${name ?? "子目"} · 工料机 ${resources.length} 条`
      }
    />
  );
}
