"""任务2f：norm_faithful（L6-C 规范问答忠实度）评测——traditional↔agentic 对比。

按 MS.md ⑤ baseline-first ablation：**同一 `norm_faithful` 集分别跑传统/agentic 两态**，比忠实率/幻觉率/
答案要点/上下文召回/误拒率 → 归因分解 vs 引用回查各自贡献。判定器同目录 `norm_faithful_score.py`
纯函数、已单测；忠实率复用 `app.ce.norm.faithfulness.check_faithfulness`（agentic RAG 招牌）。

`--mode`：设 `CE_NORM_FAITHFULNESS_CHECK`（agentic=1 开引用回查 / traditional=0 关）+ 给 run 打 variant 标签
供 Langfuse 横向比。**注**：本 flag 直接控确定性引用回查那半；查询分解那半在 norm-qa 提示词里（agentic 默认），
要做纯 prompt 级 A/B 再切 norm-qa 提示词变体（deer-flow variant 机制）。**先跑 traditional 立基线再跑 agentic。**

与 routing/cost_task 同构（主线程逐条、不另起 loop）。运行（服务器，:8100 知识 + :8099 vLLM 起齐）：
    uv run --project backend python benchmark/L6_agent/norm_faithful/run_norm_faithful_experiment.py \
        --mode traditional --run-name norm_base [--limit N] [--no-langfuse]
    uv run --project backend python benchmark/L6_agent/norm_faithful/run_norm_faithful_experiment.py --mode agentic --run-name norm_agentic
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent                     # benchmark/L6_agent/norm_faithful/
_BENCH = _HERE.parents[1]                                   # benchmark/
sys.path.insert(0, str(_BENCH / "_shared"))                 # _lf / _paths
sys.path.insert(0, str(_HERE))                              # norm_faithful_score（同目录）

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path（import app 前置）
from norm_faithful_score import NormObs, aggregate, detect_refusal, score_case  # noqa: E402

DATASET_FILE = _HERE / "norm_faithful.jsonl"
DATASET_NAME = "agent-norm-faithful"


def _load_cases(split: str | None, limit: int | None) -> list[dict]:
    cases = []
    for line in DATASET_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        if split and c.get("split") != split:
            continue
        cases.append(c)
    return cases[:limit] if limit else cases


def _harvest_clauses(data) -> list[dict]:
    """从 ce-rag 工具结果 JSON 里递归捞出条文项（含 node_path/clause/std 的 dict），作检索证据。"""
    out: list[dict] = []
    if isinstance(data, dict):
        if any(k in data for k in ("node_path", "clause", "clause_no")):
            out.append(data)
        for v in data.values():
            out.extend(_harvest_clauses(v))
    elif isinstance(data, list):
        for v in data:
            out.extend(_harvest_clauses(v))
    return out


def _observe(agent_client, question: str, thread_id: str) -> NormObs:
    """跑一次 agent，抽 {答案, 检索证据(ce-rag 工具结果里的条文), 是否拒答}。

    agent_client 整轮复用（逐条新建会让上一条的持久 MCP 会话被 GC 在别的 task 里收尾 →
    anyio cancel scope RuntimeError 噪音）。
    """
    answer_parts: list[str] = []
    evidence: list[dict] = []
    for ev in agent_client.stream(question, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        d = ev.data
        if d.get("type") == "ai" and isinstance(d.get("content"), str):
            answer_parts.append(d["content"])
        elif d.get("type") == "tool" and str(d.get("name") or "").startswith("ce-rag"):
            c = d.get("content")
            texts = [c] if isinstance(c, str) else (
                [x.get("text", "") for x in c if isinstance(x, dict)] if isinstance(c, list) else [])
            for t in texts:
                try:
                    evidence.extend(_harvest_clauses(json.loads(t)))
                except (json.JSONDecodeError, TypeError):
                    pass
    answer = "".join(answer_parts)
    return NormObs(answer=answer[:4000], evidence=evidence, did_refuse=detect_refusal(answer))


def main() -> int:
    p = argparse.ArgumentParser(description="norm_faithful 规范问答忠实度评测（traditional↔agentic 对比）")
    p.add_argument("--mode", choices=["traditional", "agentic"], required=True,
                   help="traditional=关引用回查(基线) / agentic=开(CE_NORM_FAITHFULNESS_CHECK)")
    p.add_argument("--run-name", default=None)
    p.add_argument("--split", default="test")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--no-langfuse", action="store_true")
    args = p.parse_args()

    # mode → 引用回查开关（agent 构建前设，使 norm-qa 侧 faithfulness_enabled 读到）。
    os.environ["CE_NORM_FAITHFULNESS_CHECK"] = "1" if args.mode == "agentic" else "0"

    import uuid
    run_name = args.run_name or f"norm-{args.mode}-{uuid.uuid4().hex[:6]}"
    cases = _load_cases(args.split, args.limit)
    if not cases:
        print(f"无匹配 case（split={args.split}）")
        return 1

    lf = None
    if not args.no_langfuse:
        from _lf import require_langfuse, wait_for_traces
        lf = require_langfuse()

    # 整轮共用一个客户端（见 _observe 注释）。注意须在 CE_NORM_FAITHFULNESS_CHECK 设定之后构建。
    from deerflow.client import DeerFlowClient

    agent_client = DeerFlowClient(model_name=args.model)

    scores = []
    for i, case in enumerate(cases):
        thread_id = f"exp-{run_name}-{case['id']}"
        try:
            obs = _observe(agent_client, case.get("question", ""), thread_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i + 1}/{len(cases)}] {case['id']} 跑挂，跳过：{type(exc).__name__}: {exc}")
            continue
        s = score_case(case, obs)
        scores.append(s)
        print(f"[{i + 1}/{len(cases)}] {case['id']} refuse_ok={s.refusal_ok} "
              f"faithful={s.faithful_rate} unfaithful={s.unfaithful} points={s.answer_points}")
        if lf is not None:
            traces = wait_for_traces(lf, session_id=thread_id, expected=1)
            tid = getattr(traces[0], "id", None) if traces else None
            if tid:
                lf.api.dataset_run_items.create(run_name=run_name, dataset_item_id=case["id"], trace_id=tid)
                if s.faithful_rate is not None:
                    lf.create_score(name="faithfulness", value=s.faithful_rate, trace_id=tid, data_type="NUMERIC")
                lf.create_score(name="refusal_ok", value=float(s.refusal_ok), trace_id=tid, data_type="NUMERIC")
    if lf is not None:
        lf.flush()

    rep = aggregate(scores)

    def _pct(v):
        return "—" if v is None else f"{v:.1%}"

    print(f"\n========== 聚合 · mode={args.mode} （split={args.split}, model={args.model or '默认'}） ==========")
    print(f"用例数            = {rep['n']}")
    print(f"忠实率            = {_pct(rep['faithful_rate'])}   （引用命中检索证据，越高越好）")
    print(f"幻觉引用用例率    = {_pct(rep['unfaithful_case_rate'])}   （门=0，agentic 应显著低于 traditional）")
    print(f"答案要点覆盖      = {_pct(rep['answer_points_coverage'])}")
    print(f"上下文召回(std)   = {_pct(rep['context_recall'])}")
    print(f"误拒率            = {_pct(rep['false_refuse_rate'])}   （该答却拒）")
    print(f"漏拒率            = {_pct(rep['missed_refuse_rate'])}   （该拒却答，最危险）")
    print(f"拒答准确率        = {_pct(rep['refusal_accuracy'])}")
    print(f"\n对比：先 --mode traditional 立基线，再 --mode agentic，比忠实率↑/幻觉率↓；Langfuse 按 run 横向看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
