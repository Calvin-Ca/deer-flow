"""选码评测 —— 给 CostAgent 选码层（``cost.selection.select_code``）上护栏，量化 Top-1 选码质量。

对标知识层 ``ce-code/tools/eval_bill.py``（评召回 Recall@10），本脚本评**任务层**：知识层把正解召回进
候选后（Recall@10≈60%），LLM 能不能在候选里把正解挑出来（PRD §6 业务红线 Top-1 ≥ 85%）。
端到端可用率 ≈ Recall@k × 候选内 Top-1，两段各管各的指标。

读金标 `match_gold.jsonl`（每行 ``{query, gold:[code...], note?}``，复用 ce-code 同一份）→ 每条：
  ① ``cost_client.bill_match`` 拿候选（量是否 recall 到正解）
  ② ``selection.select_code`` LLM 候选内选码（量是否选中正解、红线行为是否正确）
按**编码精确相等**判命中（清单 9 位码唯一）。三类核心指标：
  - **Top-1（端到端）**：选中码 == 金标 占比——召回缺口 + 选码错误都计入，真实可用下限。
  - **候选内 Top-1（隔离）**：仅在「正解已进候选」子集上算——剔除召回拖累，纯量 LLM 选码能力。
  - **自动定稿准确率（HITL 安全）**：在 agent 不转人工（need_review=false）的用例里选对的占比——
    这是最关键的安全指标：agent 说「无需复核」时必须可信；其中选错码的即「高置信错码」(危险)。

用（服务器，从 ce-services 根，需 :8100 知识服务 + Qwen3 vLLM 在跑）：
  python -m tools.eval_select                                   # 默认 gold + spec=2024 + top_k=10
  python -m tools.eval_select --spec 2024 --top-k 10
  python -m tools.eval_select --knowledge-url http://localhost:8100 --llm-url http://localhost:8099
  python -m tools.eval_select --json results.json              # 另存逐条 + 汇总 JSON 供追溯
"""
from __future__ import annotations

import json
from pathlib import Path

# select_code 的置信阈值（< 此值强制 need_review）；危险判定要用，故从源头取避免漂移。
from cost.selection import CONFIDENCE_THRESHOLD

# 金标默认在 ce-code（与 ce-services 平级）；以本文件定位 ce-services 根再回溯到 ce-code。
SERVICES_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = SERVICES_ROOT.parent / "ce-code" / "data" / "eval_set" / "match_gold.jsonl"


def classify_case(gold: set[str], candidate_codes: list[str], selection: dict,
                  threshold: float = CONFIDENCE_THRESHOLD) -> dict:
    """把单条用例的召回 + 选码结果判成布尔标志（纯函数，便于单测）。

    参数：
        gold —— 可接受的金标编码集合（任一命中即算对）；
        candidate_codes —— bill_match 召回的候选编码（判是否 recall 到正解）；
        selection —— select_code 的返回（code/confidence/need_review/...）；
        threshold —— 置信阈值（危险判定用）。
    返回：
        ``{recalled, picked, correct, need_review, confidence, auto_accept, dangerous, abstained}``：
        - recalled —— 正解是否进了候选（任务层选码的上限，不 recall 不可能选对）；
        - correct —— 选中码 ∈ gold；
        - auto_accept —— agent 会自动定稿（选了码且 need_review=false）；
        - dangerous —— 自动定稿但选错（high-conf 错码，绕过 HITL 的最坏情形）；
        - abstained —— 转人工（code=None 或 need_review=true）。
    """
    picked = selection.get("code")
    picked = str(picked).strip() if picked not in (None, "") else None
    confidence = float(selection.get("confidence", 0.0) or 0.0)
    need_review = bool(selection.get("need_review", False))

    recalled = bool(gold & set(candidate_codes))
    correct = picked is not None and picked in gold
    auto_accept = picked is not None and not need_review
    dangerous = auto_accept and not correct  # 不转人工却选错——会污染下游、绕过复核
    abstained = picked is None or need_review

    return {
        "recalled": recalled, "picked": picked, "correct": correct,
        "need_review": need_review, "confidence": confidence,
        "auto_accept": auto_accept, "dangerous": dangerous, "abstained": abstained,
    }


def aggregate(records: list[dict]) -> dict:
    """把逐条标志汇总为指标（纯函数，便于单测）。

    参数：records —— ``classify_case`` 输出的列表。
    返回：``{n, recall_at_k, top1, top1_given_recalled, auto_accept_acc, review_rate,
            n_auto_accept, n_recalled, n_dangerous}``——top1/recall 等为占比（0~1），
        top1_given_recalled / auto_accept_acc 分母为子集（无样本→None）。
    """
    n = len(records)
    if n == 0:
        return {"n": 0, "recall_at_k": 0.0, "top1": 0.0, "top1_given_recalled": None,
                "auto_accept_acc": None, "review_rate": 0.0,
                "n_auto_accept": 0, "n_recalled": 0, "n_dangerous": 0}

    n_recalled = sum(1 for r in records if r["recalled"])
    n_auto_accept = sum(1 for r in records if r["auto_accept"])
    n_dangerous = sum(1 for r in records if r["dangerous"])

    top1 = sum(1 for r in records if r["correct"]) / n
    # 候选内 Top-1：仅在召回到正解的子集上算（隔离召回拖累，纯量选码）。
    top1_given_recalled = (
        sum(1 for r in records if r["recalled"] and r["correct"]) / n_recalled
        if n_recalled else None
    )
    # 自动定稿准确率：agent 不转人工时的命中率（HITL 安全核心）。
    auto_accept_acc = (
        sum(1 for r in records if r["auto_accept"] and r["correct"]) / n_auto_accept
        if n_auto_accept else None
    )
    review_rate = sum(1 for r in records if r["need_review"]) / n

    return {"n": n, "recall_at_k": n_recalled / n, "top1": top1,
            "top1_given_recalled": top1_given_recalled, "auto_accept_acc": auto_accept_acc,
            "review_rate": review_rate, "n_auto_accept": n_auto_accept,
            "n_recalled": n_recalled, "n_dangerous": n_dangerous}


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


def _verdict(rec: dict) -> str:
    """逐条判定标记：✓1 选对自动定稿 / ✓? 选对但仍转人工 / 弃 转人工 / ✗危 高置信错码。"""
    if rec["correct"]:
        return "✓1" if rec["auto_accept"] else "✓?"
    if rec["dangerous"]:
        return "✗危"
    return "弃"  # 转人工（code=None 或 need_review），未选对但守住红线


def run_eval(gold_path: Path, spec: str, top_k: int,
             knowledge_url: str | None, llm_url: str, model_id: str,
             rerank: bool = False) -> tuple[dict, list[dict]]:
    """跑选码评测：逐条召回 + LLM 选码 + 判命中，打印指标表，返回 (汇总, 逐条明细)。

    参数：gold_path —— 金标 jsonl；spec —— 国标版本（2013/2024）；top_k —— 候选召回深度；
      knowledge_url —— :8100 地址（None=用 config 默认）；llm_url/model_id —— Qwen3 vLLM；
      rerank —— bill_match 是否开 cross-encoder 精排（默认关，与组价端点一致）。
    返回：(aggregate 汇总 dict, 逐条记录 list)；逐条记录含 query/gold/candidates_count/selection/标志。
    """
    from common import cost_client

    cases = _load_gold(gold_path)
    print(f"金标 {len(cases)} 条 · spec={spec} · top_k={top_k} · rerank={'on' if rerank else 'off'} · "
          f"知识服务={knowledge_url or '(config默认)'} · LLM={llm_url}\n")

    # 列宽固定的朴素表（ce-services 不引 rich，保持轻量）。
    header = f"{'查询':<26}{'金标':<12}{'召回':<6}{'选码':<26}{'置信':<8}{'判定':<6}"
    print(header)
    print("-" * 84)

    records: list[dict] = []
    details: list[dict] = []
    for c in cases:
        match_resp = cost_client.bill_match(c["query"], spec, top_k=top_k,
                                            rerank=rerank, base_url=knowledge_url)
        candidates = match_resp.get("candidates", [])
        codes = [str(x.get("code", "")).strip() for x in candidates if x.get("code")]

        selection = select_safe(c["query"], candidates, llm_url, model_id)
        rec = classify_case(c["gold"], codes, selection)
        records.append(rec)

        verdict = _verdict(rec)
        picked = selection.get("code") or "—"
        print(f"{c['query'][:24]:<26}{'/'.join(sorted(c['gold'])):<12}"
              f"{'✓' if rec['recalled'] else '✗':<6}{str(picked)[:24]:<26}"
              f"{rec['confidence']:<8.2f}{verdict:<6}")

        details.append({"query": c["query"], "gold": sorted(c["gold"]),
                        "candidates_count": len(candidates), "selection": selection,
                        **{k: rec[k] for k in ("recalled", "correct", "need_review",
                                               "auto_accept", "dangerous")}})

    m = aggregate(records)
    _print_summary(m, top_k)
    return m, details


def select_safe(query: str, candidates: list[dict], llm_url: str, model_id: str) -> dict:
    """调 select_code，LLM/网络异常不中断整轮评测——记成「转人工」记录降级（reason 标注错误）。"""
    from cost.selection import select_code
    try:
        return select_code(query, candidates, llm_url, model_id)
    except Exception as exc:  # noqa: BLE001 —— 评测要跑完全部用例，单条 LLM 故障降级不抛
        return {"code": None, "confidence": 0.0,
                "reason": f"[评测降级] 选码调用异常：{type(exc).__name__}: {exc}",
                "need_review": True, "alternatives": [], "_error": True}


def _print_summary(m: dict, top_k: int) -> None:
    """打印汇总指标 + PRD §6 红线对照。"""
    def pct(x: float | None) -> str:
        return "—" if x is None else f"{x:.0%}"

    top1_ok = m["top1"] >= 0.85
    print("\n汇总")
    print(f"  样本数 n={m['n']}  召回率 Recall@{top_k}={pct(m['recall_at_k'])} "
          f"({m['n_recalled']}/{m['n']})")
    print(f"  Top-1（端到端）={pct(m['top1'])}{'  ✅达红线' if top1_ok else '  ❌未达红线(≥85%)'}")
    print(f"  候选内 Top-1（隔离召回）={pct(m['top1_given_recalled'])} "
          f"——分母=召回到正解的 {m['n_recalled']} 条，纯量 LLM 选码能力")
    print(f"  自动定稿准确率={pct(m['auto_accept_acc'])} "
          f"(自动定稿 {m['n_auto_accept']} 条)  转人工率={pct(m['review_rate'])}")
    danger_tag = "✅无" if m["n_dangerous"] == 0 else f"❌{m['n_dangerous']} 条"
    print(f"  高置信错码（危险·绕过 HITL）={danger_tag}")
    print("\n红线：Top-1 ≥ 85%（PRD §6 业务层）。端到端未达多因 Recall 缺口——看「候选内 Top-1」"
          "定位是召回问题还是选码问题；「高置信错码」必须为 0（HITL 安全）。")


def _cli() -> None:
    """argparse CLI（ce-services 不引 click，用 stdlib）。"""
    import argparse

    from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL

    p = argparse.ArgumentParser(description="CostAgent 选码评测（任务层 Top-1）")
    p.add_argument("--gold", default=str(DEFAULT_GOLD), help="金标 jsonl 路径（默认 ce-code/data/eval_set/match_gold.jsonl）")
    p.add_argument("--spec", default="2024", help="国标版本 2013/2024（默认 2024）")
    p.add_argument("--top-k", type=int, default=10, help="候选召回深度（默认 10）")
    p.add_argument("--knowledge-url", default=KNOWLEDGE_URL, help="知识服务 :8100 地址")
    p.add_argument("--llm-url", default=LLM_URL, help="Qwen3 vLLM 地址")
    p.add_argument("--model-id", default=LLM_MODEL_ID, help="模型 id")
    p.add_argument("--rerank", action="store_true", help="bill_match 开 cross-encoder 精排（默认关）")
    p.add_argument("--json", dest="json_out", default=None, help="另存逐条+汇总 JSON 的路径")
    args = p.parse_args()

    m, details = run_eval(Path(args.gold), args.spec, args.top_k, args.knowledge_url,
                          args.llm_url, args.model_id, rerank=args.rerank)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": m, "details": details}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n已写 {args.json_out}")


if __name__ == "__main__":
    _cli()
