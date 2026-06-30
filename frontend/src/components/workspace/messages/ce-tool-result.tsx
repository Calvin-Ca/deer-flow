/**
 * 造价领域 MCP 工具的中间过程渲染（ce-task_* / ce-cost_*）。
 *
 * 背景：deer-flow 原生 toolCall 流的泛型分支只画一行 label、不渲染工具结果（见 message-group.tsx
 * 的 `else` 分支）。把 norm_qa / cost_compose 等任务层能力做成 MCP 工具后，工具名/入参/结果天然
 * 结构化，这里按**稳定的工具名**派发，把领域结果（cited_clauses / 选码+置信度 / 候选 / 取数）渲染成
 * 折叠中间过程里的依据条目——让造价用户在对话里就看见「凭什么这么答」，而不是埋在 bash stdout 里。
 *
 * 设计：领域逻辑全部收在本文件，上游 `message-group.tsx` 只加一个 `isCeTool(name)` 委派分支，
 * 不把造价业务字段塞进通用组件。结果形状做防御性读取（MCP 序列化/加载中/异常透传都不崩）。
 */
import {
  CalculatorIcon,
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

/** 本组件能渲染的造价领域 MCP 工具前缀。 */
export function isCeTool(name: string): boolean {
  return name.startsWith("ce-task_") || name.startsWith("ce-cost_");
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
  const tool = name.replace(/^ce-(task|cost)_/, "");

  switch (tool) {
    case "norm_qa":
      return <NormQa args={args} r={r} />;
    case "cost_compose":
      return <CostCompose args={args} r={r} />;
    case "bill_match":
      return <BillMatch args={args} r={r} />;
    case "price_compose":
      return <PriceCompose args={args} r={r} />;
    case "quota_lookup":
      return <QuotaLookup args={args} r={r} />;
    default:
      return (
        <ChainOfThoughtStep icon={DatabaseIcon} label={`造价工具：${tool}`} />
      );
  }
}

/** 规范问答：展示命中条文数 + cited_clauses（标准号 + 条文号）。 */
function NormQa({ args, r }: { args: Rec; r?: Rec }) {
  const query = str(args.query) ?? "规范问答";
  const retrieved = num(asRec(r?.meta)?.retrieved);
  const cited = asArr(r?.cited_clauses)
    .map(asRec)
    .filter((c): c is Rec => !!c);
  const desc =
    r === undefined
      ? "检索中…"
      : retrieved === 0 || cited.length === 0
        ? "未检索到相关条文"
        : `命中 ${retrieved ?? cited.length} 条，引用 ${cited.length} 条`;
  return (
    <ChainOfThoughtStep
      icon={ScrollTextIcon}
      label={`规范问答：${query}`}
      description={desc}
    >
      {cited.length > 0 && (
        <div className="space-y-1.5">
          {cited.slice(0, 6).map((c, i) => {
            const head =
              [str(c.standard), str(c.clause)].filter(Boolean).join(" ") ||
              "条文";
            const text = str(c.text);
            const contextual = str(c.relevance) === "contextual";
            return (
              <div key={i} className="text-xs leading-snug">
                <span className="font-medium">{head}</span>
                {contextual && (
                  <span className="text-muted-foreground"> · 背景参考</span>
                )}
                {text && (
                  <span className="text-muted-foreground"> — {clip(text)}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </ChainOfThoughtStep>
  );
}

/** 组价选码：选中码 + 置信度 + 是否转人工 + 取数状态。 */
function CostCompose({ args, r }: { args: Rec; r?: Rec }) {
  const description = str(args.description) ?? "组价选码";
  const code = str(r?.code);
  const sel = asRec(r?.selection);
  const confidence = num(sel?.confidence);
  const needReview = sel?.need_review === true;
  const priceStatus = str(r?.price_status);
  const count = num(r?.candidates_count);
  const desc =
    r === undefined
      ? "选码中…"
      : code
        ? `选码 ${code}`
        : "未选出码（转人工）";
  return (
    <ChainOfThoughtStep
      icon={CalculatorIcon}
      label={`组价选码：${description}`}
      description={desc}
    >
      <ChainOfThoughtSearchResults>
        {count != null && (
          <ChainOfThoughtSearchResult>
            候选 {count}
          </ChainOfThoughtSearchResult>
        )}
        {confidence != null && (
          <ChainOfThoughtSearchResult>
            置信 {confidence.toFixed(2)}
          </ChainOfThoughtSearchResult>
        )}
        {needReview && (
          <ChainOfThoughtSearchResult>需人工复核</ChainOfThoughtSearchResult>
        )}
        {priceStatus && (
          <ChainOfThoughtSearchResult>
            取数 {priceStatus}
          </ChainOfThoughtSearchResult>
        )}
      </ChainOfThoughtSearchResults>
    </ChainOfThoughtStep>
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
