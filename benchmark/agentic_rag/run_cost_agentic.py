"""Agentic RAG track · 端到端组价 runner —— 跑 cost_agentic 用例（FR-2/3/6），pass^k + 红线独立计分。

与 L6_agent/cost_task 的关系（解耦边界）：**判官复用**（import 既有 `cost_task_score` 纯函数）、
**数据独立**（读本 track `data/cost_agentic.jsonl`，不碰 canonical）。组价链无「naive↔agentic」两态可切
（取数+计算恒走确定性引擎/工具编排），故本 runner 不做消融，只出端到端终态成绩，供与 L6 cost_task 同口径看。

运行（服务器，:8100/:8102 知识 + :8099 vLLM 起齐；宿主机嵌入式跑须 config base_url=localhost:8099）：
    uv run --project backend python benchmark/agentic_rag/run_cost_agentic.py --run-name cost_ar_v1 --no-langfuse
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent                          # benchmark/agentic_rag/
_BENCH = _HERE.parent                                            # benchmark/
sys.path.insert(0, str(_BENCH / "_shared"))                      # _lf / _paths
sys.path.insert(0, str(_BENCH / "L6_agent" / "cost_task"))       # 复用判官 cost_task_score

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path
from cost_task_score import RunObservation, aggregate_passk, aggregate_suite, score_run  # noqa: E402

DEFAULT_DATA = _HERE / "data" / "cost_agentic.jsonl"
CLARIFY_TOOL = "ask_clarification"
_CODE_RE = re.compile(r"\b(\d{9})(?:\d{3})?\b")


def _load_cases(data_file: Path, split: str | None, limit: int | None) -> list[dict]:
    """读 jsonl 用例。输入 data_file/split/limit；输出 case dict 列表。"""
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


def _observe(agent_client, query: str, thread_id: str) -> RunObservation:
    """跑一次 agent，抽判定所需外部观测（终态码/工具名/答案/是否反问）。

    输入：agent_client 复用客户端；query 组价诉求；thread_id 本次独立线程。
    输出：RunObservation（codes 出现过的码 / final_code 落定码 / tools / answer / did_clarify）。
    """
    tool_names: list[str] = []
    answer_parts: list[str] = []
    tool_texts: list[str] = []
    for ev in agent_client.stream(query, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        d = ev.data
        if d.get("type") == "ai":
            for tc in d.get("tool_calls", []) or []:
                if tc.get("name"):
                    tool_names.append(tc["name"])
            if isinstance(d.get("content"), str):
                answer_parts.append(d["content"])
        elif d.get("type") == "tool":
            if d.get("name"):
                tool_names.append(d["name"])
            c = d.get("content")
            if isinstance(c, str):
                tool_texts.append(c)
            elif isinstance(c, list):
                tool_texts.extend(x.get("text", "") for x in c if isinstance(x, dict))
    answer = "".join(answer_parts)
    codes_tools = [m.group(1) for t in tool_texts for m in _CODE_RE.finditer(t)]
    codes_ans = [m.group(1) for m in _CODE_RE.finditer(answer)]
    final = codes_ans[-1] if codes_ans else (codes_tools[-1] if codes_tools else None)
    return RunObservation(
        codes=list(dict.fromkeys(codes_tools + codes_ans)),
        final_code=final,
        tools=list(dict.fromkeys(tool_names)),
        answer=answer[:2000],
        did_clarify=CLARIFY_TOOL in tool_names,
    )


def main() -> int:
    """逐条 × pass_k 跑、判定、聚合。返回退出码。"""
    p = argparse.ArgumentParser(description="Agentic RAG 端到端组价（pass^k + 红线独立）")
    p.add_argument("--data", default=str(DEFAULT_DATA), help="数据集路径（默认本 track cost_agentic.jsonl）")
    p.add_argument("--run-name", default=None)
    p.add_argument("--split", default="test")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--no-langfuse", action="store_true")
    args = p.parse_args()

    run_name = args.run_name or f"cost-ar-{uuid.uuid4().hex[:8]}"
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
    agent_client = DeerFlowClient(model_name=args.model)

    results = []
    for i, case in enumerate(cases):
        pass_k = int(case.get("pass_k") or 1)
        goal = case.get("user_goal", "")
        runs = []
        for k in range(pass_k):
            thread_id = f"exp-{run_name}-{nonce}-{case['id']}-r{k}"
            try:
                obs = _observe(agent_client, goal, thread_id)
            except Exception as exc:  # noqa: BLE001
                print(f"    run {k} 跑挂：{type(exc).__name__}: {exc}")
                runs.append(score_run(case, RunObservation()))
                continue
            rs = score_run(case, obs)
            runs.append(rs)
            if lf is not None:
                traces = wait_for_traces(lf, session_id=thread_id, expected=1)
                tid = getattr(traces[0], "id", None) if traces else None
                if tid:
                    lf.api.dataset_run_items.create(run_name=run_name, dataset_item_id=case["id"], trace_id=tid)
                    lf.create_score(name="task_pass", value=float(rs.task_pass), trace_id=tid,
                                    data_type="NUMERIC", comment=f"final_code={obs.final_code}")
        res = aggregate_passk(case, runs)
        results.append(res)
        print(f"[{i + 1}/{len(cases)}] {res.case_id} ({res.difficulty}) "
              f"pass^{res.pass_k}={res.task_passk} redline_clean={res.redline_clean} overall={res.overall_pass}")
    if lf is not None:
        lf.flush()

    rep = aggregate_suite(results)
    print(f"\n===== 聚合（split={args.split}, model={args.model or '默认'}） =====")
    print(f"用例数              = {rep['n_cases']}")
    print(f"任务成功率 pass^k   = {rep['task_success_passk']:.2%}")
    print(f"红线违规率(独立)    = {rep['redline_violation_rate']:.2%}   （门=0）")
    print(f"综合通过率          = {rep['overall_pass_rate']:.2%}")
    print(f"红线 evaluable 覆盖 = {rep['policy_evaluable_coverage']:.2%}")
    print("逐 difficulty：")
    for d, v in rep["by_difficulty"].items():
        print(f"    {d:<16} {v['pass']}/{v['n']}  ({v['pass_rate']:.0%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
