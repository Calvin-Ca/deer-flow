"""清单匹配评测 —— 给 `/bill/match`（构件→清单候选）上护栏，量化排序质量。

读金标 `match_gold.jsonl`（每行 ``{query, gold:[code...], note?}``）→ 对每条跑
``cost.bill_match.search_bill`` 取候选 → 按**编码精确相等**判命中（清单 9 位码唯一，无规范轨的
「包含关系」），算 **Top-1 / Top-3 / Recall@k / MRR / 平均金标秩**（PRD §6 排序敏感口径）。

PRD §6 红线：Top-1 ≥ 85% / Top-3 ≥ 95%。未达即 `/bill/match` 仅「跑通」、需上 sparse/rerank/KG。

使用（服务器，从 ce-code 根，需 Milvus + 嵌入服务在跑）：
  uv run python -m tools.eval_bill                                  # 默认 gold + top_k=10
  uv run python -m tools.eval_bill --gold data/eval_set/match_gold.jsonl --top-k 10
"""
from __future__ import annotations

import json
from pathlib import Path

from config import COST_BILL_COLLECTION

# click / rich / cost.bill_match 仅在 CLI 执行路径用（见 run_eval / _cli），不在模块顶层 import——
# 让纯指标函数（first_gold_rank/aggregate/_load_gold）只依赖 stdlib+config，本地可单测。

ROOT = Path(__file__).resolve().parent.parent


def first_gold_rank(gold: set[str], candidate_codes: list[str]) -> int | None:
    """候选码序列里**首个**命中金标的 1-based 秩（纯函数，便于单测）。

    参数：
        gold (set[str]): 该用例可接受的金标编码集合（任一命中即算）。
        candidate_codes (list[str]): 召回候选的编码，按相关度降序。
    返回：
        int | None: 首个 code ∈ gold 的位次（从 1 数）；top_k 内无命中→None。
    """
    for i, code in enumerate(candidate_codes, start=1):
        if code in gold:
            return i
    return None


def aggregate(ranks: list[int | None], k: int) -> dict:
    """把每用例的金标秩汇总为指标（纯函数，便于单测）。

    参数：
        ranks (list[int | None]): 各用例首个金标秩（None=top_k 内未命中）。
        k (int): 召回深度（Recall@k 的 k）。
    返回：
        dict: ``{n, top1, top3, recall_at_k, mrr, mean_hit_rank}``——
            top1/top3/recall_at_k 为占比（0~1），mrr 为平均倒数秩，
            mean_hit_rank 为命中用例的平均秩（无命中则 None）。
    """
    n = len(ranks)
    if n == 0:
        return {"n": 0, "top1": 0.0, "top3": 0.0, "recall_at_k": 0.0, "mrr": 0.0, "mean_hit_rank": None}
    top1 = sum(1 for r in ranks if r == 1) / n
    top3 = sum(1 for r in ranks if r is not None and r <= 3) / n
    recall = sum(1 for r in ranks if r is not None and r <= k) / n
    mrr = sum((1.0 / r) for r in ranks if r is not None) / n
    hits = [r for r in ranks if r is not None]
    mean_hit_rank = (sum(hits) / len(hits)) if hits else None
    return {"n": n, "top1": top1, "top3": top3, "recall_at_k": recall, "mrr": mrr,
            "mean_hit_rank": mean_hit_rank}


def _load_gold(path: Path) -> list[dict]:
    """读 match_gold.jsonl（跳过空行）；每条规范化出 ``gold`` 为 set[str]。"""
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        g = obj.get("gold")
        obj["gold"] = set(g if isinstance(g, list) else [g])
        cases.append(obj)
    return cases


def run_eval(gold_path: str, top_k: int, collection: str, rerank: bool = False) -> dict:
    """跑清单匹配评测：逐条召回 + 判命中，打印指标表，返回汇总 dict（无 click 依赖）。

    参数：gold_path —— 金标 jsonl（相对 ce-code 根）；top_k —— 召回深度；collection —— 向量库名；
      rerank —— 是否 cross-encoder 精排（默认 True；传 False 量 dense 基线作对比）。
    返回：``aggregate`` 的汇总 dict（n/top1/top3/recall_at_k/mrr/mean_hit_rank）。
    """
    from rich.console import Console
    from rich.table import Table

    from cost.bill_match import search_bill

    console = Console()
    cases = _load_gold(ROOT / gold_path)
    console.print(f"金标 {len(cases)} 条 · top_k={top_k} · collection={collection} · "
                  f"rerank={'on' if rerank else 'off'}\n")

    table = Table(title="清单匹配评测 · 逐条", show_lines=False)
    for col in ("查询", "金标", "Top-1 召回", "金标秩", "命中"):
        table.add_column(col, overflow="fold")

    ranks: list[int | None] = []
    for c in cases:
        candidates = search_bill(c["query"], top_k, collection_name=collection, rerank=rerank)
        codes = [x["code"] for x in candidates]
        rank = first_gold_rank(c["gold"], codes)
        ranks.append(rank)
        top1 = f"{candidates[0]['code']} {candidates[0]['name']}" if candidates else "—"
        hit_mark = "✓1" if rank == 1 else ("✓" if rank else "✗")
        table.add_row(c["query"][:24], "/".join(sorted(c["gold"])),
                      top1[:26], str(rank) if rank else "—", hit_mark)

    console.print(table)
    m = aggregate(ranks, top_k)
    hit_rank = f"{m['mean_hit_rank']:.2f}" if m["mean_hit_rank"] is not None else "—"
    console.print(
        f"\n[bold]汇总[/bold]  n={m['n']}  "
        f"Top-1=[{'green' if m['top1'] >= 0.85 else 'red'}]{m['top1']:.0%}[/]  "
        f"Top-3=[{'green' if m['top3'] >= 0.95 else 'red'}]{m['top3']:.0%}[/]  "
        f"Recall@{top_k}={m['recall_at_k']:.0%}  MRR={m['mrr']:.3f}  平均命中秩={hit_rank}"
    )
    console.print("[dim]红线：Top-1 ≥ 85% / Top-3 ≥ 95%（PRD §6）。未达→上 sparse/rerank/KG。[/dim]")
    return m


def _cli():
    """构造 click 命令（click 在此 lazy import，模块顶层无依赖）。"""
    import click

    @click.command()
    @click.option("--gold", "gold_path", default="data/eval_set/match_gold.jsonl",
                  help="金标 jsonl 路径（相对 ce-code 根）")
    @click.option("--top-k", default=10, help="召回深度 k")
    @click.option("--collection", default=COST_BILL_COLLECTION, help="清单向量库 collection")
    @click.option("--rerank/--no-rerank", default=False, help="是否 cross-encoder 精排（默认关；实测劣化 dense，--rerank 仅备查）")
    def main(gold_path: str, top_k: int, collection: str, rerank: bool) -> None:
        """跑清单匹配评测并打印指标表。"""
        run_eval(gold_path, top_k, collection, rerank)

    return main


if __name__ == "__main__":
    _cli()()
