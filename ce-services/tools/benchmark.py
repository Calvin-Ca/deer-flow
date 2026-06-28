"""多模型 benchmark —— 同一评测集跑多个模型、并排对比，支撑「选模型 / 换模型」决策。

背景：组价/问答各步对 LLM 的依赖（选码 / 生成）当前压在 Qwen3-8B 上，需量化「换更大模型（如
Qwen3-32B-AWQ）值不值」。本脚本不重写评测逻辑，而是**编排多模型**：对注册表里每个模型跑同一套
任务评测（本期 = 选码，复用 ``tools.eval_select``），汇成并排对比表 + 存 JSON 供追溯。

**置信度分布统计**（本脚本新增、对比表核心列）：直接暴露「置信度无区分度」这一治本前置——
若某模型选码 confidence 几乎恒为单一值（distinct=1，如全 0.95），则 ``confidence<τ`` 门控与
HITL「高置信错码须为 0」红线在该模型上结构性失效（见 TODO 主线二）。换模型能否带来置信区分度，
是本 benchmark 要回答的关键问题之一。

任务覆盖路线（“都覆盖”）：
  - ✅ Phase 1（本文件）：选码（eval_select），模型参数化已就绪。
  - ⬜ Phase 2：norm-qa 规范问答（需先建造价规范 QA 评测集：条文召回 + 引用准确率）。
  - ⬜ Phase 3：生成质量（LLM-judge 打分，judge 模型可配）。

模型注册：默认 ``MODELS`` 只含一个（config 默认，通常 Qwen3-8B）以便开箱即跑；对比需在
``--models-file`` 传一个 JSON 列表（每项 ``{name, llm_url, model_id}``），把待对比模型（如
Qwen3-32B-AWQ 的 vLLM endpoint）列进去——**用文件而非长 CLI 串**，避免长命令粘贴到服务器折行。

用（服务器，从 ce-services 根，需 :8100 知识服务 + 各模型 vLLM endpoint 在跑）：
  python -m tools.benchmark --spec 2013 --top-k 10                      # 默认单模型（基线），2013 gold n=91 统计力强
  python -m tools.benchmark --models-file models.json --spec 2013      # 多模型对比（推荐）
  python -m tools.benchmark --models-file models.json --json bench.json # 另存对比 + 逐模型明细

models.json 示例（基线 8B + 待对比 32B-AWQ；llm_url/model_id 按服务器实际 endpoint 填）：
  [
    {"name": "qwen3-8b",       "llm_url": "http://localhost:8099", "model_id": "qwen3-8b"},
    {"name": "qwen3-32b-awq",  "llm_url": "http://localhost:8098", "model_id": "qwen3-32b-awq"}
  ]
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.eval_select import DEFAULT_GOLD, run_eval


def confidence_stats(details: list[dict]) -> dict:
    """从逐条明细算选码置信度分布（暴露「无区分度」）。

    参数：details —— ``run_eval`` 返回的逐条记录（每条含 ``selection.confidence``）。
    返回：``{avg, min, max, distinct, n}``——distinct=不同置信值个数（=1 即全同值、门控/红线失效信号）；
      无样本 → 各值 None / 0。
    """
    confs = [
        float(d["selection"].get("confidence") or 0.0)
        for d in details
        if d.get("selection") is not None
    ]
    if not confs:
        return {"avg": None, "min": None, "max": None, "distinct": 0, "n": 0}
    rounded = {round(c, 4) for c in confs}
    return {
        "avg": round(sum(confs) / len(confs), 4),
        "min": min(confs),
        "max": max(confs),
        "distinct": len(rounded),
        "n": len(confs),
    }


def run_benchmark(models: list[dict], gold_path: Path, spec: str, top_k: int,
                  knowledge_url: str | None, rerank: bool) -> list[dict]:
    """对每个模型跑选码评测，收集汇总 + 置信度分布。

    参数：models —— ``[{name, llm_url, model_id}...]``；gold_path/spec/top_k/knowledge_url/rerank —— 透传 eval_select。
    返回：每模型一行 ``{name, model_id, summary, confidence}``（summary=eval_select.aggregate 输出）。
    单模型失败（endpoint 不可达等）不中断整轮：记 ``error`` 字段、其余照跑（换模型 benchmark 要尽量跑全）。
    """
    rows: list[dict] = []
    for spec_model in models:
        name = spec_model.get("name") or spec_model.get("model_id") or "?"
        print(f"\n{'='*84}\n模型 [{name}] — model_id={spec_model.get('model_id')} "
              f"llm_url={spec_model.get('llm_url')}\n{'='*84}")
        try:
            summary, details = run_eval(
                gold_path, spec, top_k, knowledge_url,
                spec_model["llm_url"], spec_model["model_id"], rerank=rerank,
            )
            rows.append({
                "name": name,
                "model_id": spec_model.get("model_id"),
                "summary": summary,
                "confidence": confidence_stats(details),
                "details": details,
            })
        except Exception as exc:  # noqa: BLE001 —— 一个模型挂掉不该让其余模型评测白跑
            print(f"  ⚠️ 模型 [{name}] 评测失败：{type(exc).__name__}: {exc}")
            rows.append({"name": name, "model_id": spec_model.get("model_id"),
                         "error": f"{type(exc).__name__}: {exc}"})
    return rows


def _pct(x: float | None) -> str:
    """占比格式化（None→—）。"""
    return "—" if x is None else f"{x:.0%}"


def print_comparison(rows: list[dict], spec: str, top_k: int) -> None:
    """打印并排对比表（每模型一行，关键指标 + 置信度分布）。"""
    print(f"\n{'='*100}\n选码 benchmark 对比 · spec={spec} · top_k={top_k}\n{'='*100}")
    header = (f"{'模型':<16}{'n':<5}{'Recall':<8}{'Top-1':<8}{'候选内':<8}"
              f"{'自动定稿':<9}{'转人工':<8}{'高置信错':<9}{'置信avg':<9}{'置信distinct':<12}")
    print(header)
    print("-" * 100)
    for r in rows:
        if r.get("error"):
            print(f"{r['name'][:14]:<16}评测失败：{r['error']}")
            continue
        m = r["summary"]
        c = r["confidence"]
        danger = "✅0" if m["n_dangerous"] == 0 else f"❌{m['n_dangerous']}"
        print(f"{r['name'][:14]:<16}{m['n']:<5}{_pct(m['recall_at_k']):<8}"
              f"{_pct(m['top1']):<8}{_pct(m['top1_given_recalled']):<8}"
              f"{_pct(m['auto_accept_acc']):<9}{_pct(m['review_rate']):<8}"
              f"{danger:<9}{(c['avg'] if c['avg'] is not None else '—'):<9}"
              f"{c['distinct']:<12}")
    print("\n读表：Top-1 端到端（PRD §6 红线 ≥85%）；候选内 Top-1 隔离召回纯量选码；高置信错码须为 0；"
          "\n      置信 distinct=1 → 该模型置信度无区分度（门控 τ 与高置信错码红线在其上结构性失效）。")


def _load_models(models_file: str | None, default_url: str, default_id: str) -> list[dict]:
    """加载模型注册表：给 --models-file 则读 JSON 列表，否则回落单模型（config 默认，通常基线 8B）。"""
    if models_file:
        data = json.loads(Path(models_file).read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("--models-file 须为非空 JSON 列表 [{name,llm_url,model_id}...]")
        for m in data:
            if "llm_url" not in m or "model_id" not in m:
                raise ValueError(f"模型项缺 llm_url/model_id：{m}")
        return data
    return [{"name": default_id, "llm_url": default_url, "model_id": default_id}]


def _cli() -> None:
    """argparse CLI（stdlib，避免长命令——多模型走 --models-file 而非 CLI 串）。"""
    import argparse

    from common.config import KNOWLEDGE_URL, LLM_MODEL_ID, LLM_URL

    p = argparse.ArgumentParser(description="多模型选码 benchmark（Qwen3-8B 基线 vs 候选模型）")
    p.add_argument("--models-file", default=None,
                   help="模型注册 JSON 列表 [{name,llm_url,model_id}...]；不给则只跑 config 默认单模型")
    p.add_argument("--gold", default=None,
                   help="金标 jsonl；不给则按 spec 自动选（2013→match_gold_2013.jsonl n=91 / 否则默认 2024）")
    p.add_argument("--spec", default="2013", help="国标版本 2013/2024（默认 2013：gold n=91 统计力强）")
    p.add_argument("--top-k", type=int, default=10, help="候选召回深度（默认 10）")
    p.add_argument("--knowledge-url", default=KNOWLEDGE_URL, help="知识服务 :8100 地址")
    p.add_argument("--rerank", action="store_true", help="bill_match 开 cross-encoder 精排（默认关）")
    p.add_argument("--json", dest="json_out", default=None, help="另存对比+逐模型明细 JSON 的路径")
    args = p.parse_args()

    # gold 默认：2013 用 91 条集（统计力强），其余回落 eval_select 默认（2024 n=10）。
    if args.gold:
        gold_path = Path(args.gold)
    elif args.spec == "2013":
        gold_path = DEFAULT_GOLD.parent / "match_gold_2013.jsonl"
    else:
        gold_path = DEFAULT_GOLD

    models = _load_models(args.models_file, LLM_URL, LLM_MODEL_ID)
    print(f"benchmark：{len(models)} 个模型 · gold={gold_path.name} · spec={args.spec} · top_k={args.top_k}")

    rows = run_benchmark(models, gold_path, args.spec, args.top_k, args.knowledge_url, args.rerank)
    print_comparison(rows, args.spec, args.top_k)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"spec": args.spec, "top_k": args.top_k, "gold": gold_path.name,
                        "models": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n已写 {args.json_out}")


if __name__ == "__main__":
    _cli()
