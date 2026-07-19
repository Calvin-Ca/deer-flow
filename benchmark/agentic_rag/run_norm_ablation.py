"""Agentic RAG track · 规范问答消融 runner —— 同一数据集跑「naive ↔ agentic」两态，坐实 agentic delta。

与 L6_agent/norm_faithful 的关系（解耦边界）：
- **判官复用不重造**：忠实率/要点/拒答判定直接 import 既有 `norm_faithful_score`（单一尺子，防两把判官漂移）。
- **数据与编排独立**：数据读本 track 的 `data/norm_agentic.jsonl`（不碰 canonical 金标）；本 runner 只多做
  「消融编排」——把一次运行的 mode/变体标签/聚合指标落成结果 JSON，供 `compare_ablation.py` 出 delta 表。

两条正交消融轴（见 README）：
- **轴 A 引用回查**：`--mode traditional|agentic` 控 `CE_NORM_FAITHFULNESS_CHECK`（关/开 verify_norm 回查）。
- **轴 B 拆解/多跳**：切 `config.yaml` 的 `lead_agent.system_prompt_path`（naive 变体 ↔ v7），本 runner 不改配置，
  只把当前变体名记进结果（`--variant-label`）供归因；两轴须分开跑，否则 delta 归因混淆。

运行（服务器，:8100 ce-rag + :8099 vLLM 起齐；宿主机嵌入式跑须 config base_url=localhost:8099）：
    uv run --project backend python benchmark/agentic_rag/run_norm_ablation.py \
        --mode traditional --variant-label naive   --run-name norm_naive_r1   --no-langfuse
    uv run --project backend python benchmark/agentic_rag/run_norm_ablation.py \
        --mode agentic     --variant-label v7      --run-name norm_agentic_r1 --no-langfuse
每臂跑 2~3 轮（8B 非确定），再 `compare_ablation.py results/*.json` 看 delta。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent                              # benchmark/agentic_rag/
_BENCH = _HERE.parent                                                # benchmark/
sys.path.insert(0, str(_BENCH / "_shared"))                          # _lf / _paths
sys.path.insert(0, str(_BENCH / "L6_agent" / "norm_faithful"))       # 复用判官 norm_faithful_score

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path（import app 前置）
from norm_faithful_score import NormObs, aggregate, detect_refusal, score_case  # noqa: E402

DEFAULT_DATA = _HERE / "data" / "norm_agentic.jsonl"
RESULTS_DIR = _HERE / "results"


def _load_cases(data_file: Path, split: str | None, limit: int | None) -> list[dict]:
    """读 jsonl 用例。

    输入：data_file 数据路径；split 只留该 split（None=全取）；limit 截前 N 条（None=全取）。
    输出：case dict 列表。
    """
    cases: list[dict] = []
    for line in data_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        if split and c.get("split") != split:
            continue
        cases.append(c)
    return cases[:limit] if limit else cases


def _harvest_clauses(data) -> list[dict]:
    """从 ce-rag 工具结果 JSON 里递归捞条文项作检索证据。

    输入：data 任意嵌套的 JSON 结构（dict/list/标量）。
    输出：含 node_path/clause/clause_no 的 dict 列表（检索到的条文证据）。
    """
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
    """跑一次 agent，抽 {答案, ce-rag 检索证据, 是否拒答}（只观测不改行为）。

    输入：agent_client 复用的 DeerFlowClient；question 规范问题；thread_id 本条独立线程。
    输出：NormObs（answer 截断 4000 字 / evidence 条文证据 / did_refuse 拒答判定）。
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
    """驱动一个消融臂：逐条跑 agent → 判定 → 聚合 → 落结果 JSON。返回退出码。"""
    p = argparse.ArgumentParser(description="Agentic RAG 规范问答消融（一次跑一个臂）")
    p.add_argument("--mode", choices=["traditional", "agentic"], required=True,
                   help="轴A：traditional=关引用回查(基线) / agentic=开(CE_NORM_FAITHFULNESS_CHECK)")
    p.add_argument("--variant-label", default="unspecified",
                   help="轴B：记录当前 system_prompt 变体名（如 naive / v7），只入结果不改配置")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="数据集路径（默认本 track norm_agentic.jsonl）")
    p.add_argument("--run-name", default=None)
    p.add_argument("--split", default="test")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--no-langfuse", action="store_true")
    p.add_argument("--out", default=None, help="结果 JSON 路径（默认 results/<run-name>.json）")
    args = p.parse_args()

    # 轴A：引用回查开关须在构建 agent 前设（norm-qa 侧 faithfulness_enabled 读它）。
    os.environ["CE_NORM_FAITHFULNESS_CHECK"] = "1" if args.mode == "agentic" else "0"

    run_name = args.run_name or f"norm-{args.mode}-{args.variant_label}-{uuid.uuid4().hex[:6]}"
    nonce = uuid.uuid4().hex[:6]
    cases = _load_cases(Path(args.data), args.split, args.limit)
    if not cases:
        print(f"无匹配 case（data={args.data} split={args.split}）")
        return 1

    lf = None
    if not args.no_langfuse:
        from _lf import require_langfuse, wait_for_traces
        lf = require_langfuse()

    from deerflow.client import DeerFlowClient
    agent_client = DeerFlowClient(model_name=args.model)  # 整轮共用（逐条新建会 GC 跨 task 收尾 MCP 会话）

    scores = []
    for i, case in enumerate(cases):
        thread_id = f"exp-{run_name}-{nonce}-{case['id']}"
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
        """百分比格式化（None→占位符）。输入 v 比率或 None；输出字符串。"""
        return "—" if v is None else f"{v:.1%}"

    print(f"\n===== 消融臂 mode={args.mode} variant={args.variant_label} "
          f"（split={args.split}, model={args.model or '默认'}, n={rep['n']}） =====")
    print(f"忠实率            = {_pct(rep['faithful_rate'])}   （引用命中检索证据，越高越好）")
    print(f"幻觉引用用例率    = {_pct(rep['unfaithful_case_rate'])}   （门=0，轴A应显著↓）")
    print(f"答案要点覆盖      = {_pct(rep['answer_points_coverage'])}   （轴B拆解应↑）")
    print(f"上下文召回(std)   = {_pct(rep['context_recall'])}")
    print(f"误拒率            = {_pct(rep['false_refuse_rate'])}   （该答却拒）")
    print(f"漏拒率            = {_pct(rep['missed_refuse_rate'])}   （该拒却答，最危险，轴B反思应↓）")
    print(f"拒答准确率        = {_pct(rep['refusal_accuracy'])}")

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = Path(args.out) if args.out else RESULTS_DIR / f"{run_name}.json"
    payload = {
        "run_name": run_name, "mode": args.mode, "variant_label": args.variant_label,
        "model": args.model or "default", "data": args.data, "split": args.split, "metrics": rep,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落 {out_path}（供 compare_ablation.py 出 delta）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
