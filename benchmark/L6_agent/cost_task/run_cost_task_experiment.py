"""任务2e：cost_task（L6-A 端到端组价）评测——逐条跑 agent × pass_k，程序化判终态 + 红线，聚合 pass^k。

对标 τ-bench：不比答案文本，比 **agent 落定的最终清单码** + 溯源 + 红线行为（判定口径见
同目录 ``cost_task_score.py`` 与 L6_agent/README「三条铁律」）。数据直接读本地同目录
``cost_task.jsonl``（不依赖 Langfuse 上传）；打分器是纯函数、已单测。

与 routing/toolcall 实验同构：**主线程、逐条、不另起 loop**（DeerFlowClient.stream 自驱 async +
持久 MCP，与 dataset.run_experiment 的事件循环打架，见 run_routing_experiment 说明）。可选把逐条
分挂到 Langfuse Dataset Run（``--no-langfuse`` 关闭，只在终端出聚合）。

运行（服务器，需 :8100/:8102 知识 + :8099 vLLM 起齐使 agent 真能取数）：
    uv run --project backend python benchmark/L6_agent/cost_task/run_cost_task_experiment.py \
        --run-name v1 --split test [--limit N] [--model qwen3-8b] [--no-langfuse]
退出码 0=完成；聚合含 任务成功率(pass^k) / 红线违规率(独立) / 逐 difficulty / evaluable 覆盖率。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent               # benchmark/L6_agent/cost_task/
_BENCH = _HERE.parents[1]                              # benchmark/
sys.path.insert(0, str(_BENCH / "_shared"))            # _lf / _paths
sys.path.insert(0, str(_HERE))                         # cost_task_score（同目录）

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path（import app 前置）
from cost_task_score import RunObservation, aggregate_passk, aggregate_suite, score_run  # noqa: E402

DATASET_FILE = _HERE / "cost_task.jsonl"
DATASET_NAME = "agent-cost-task"          # Langfuse dataset 名（--no-langfuse 时不用）
CLARIFY_TOOL = "ask_clarification"


def _load_cases(split: str | None, limit: int | None) -> list[dict]:
    """读本地 jsonl，按 split 过滤、可截断。"""
    cases: list[dict] = []
    for line in DATASET_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        if split and c.get("split") != split:
            continue
        cases.append(c)
    return cases[:limit] if limit else cases


def _observe(agent_client, query: str, thread_id: str) -> RunObservation:
    """跑一次 agent，抽取判定所需的外部观测（只观测、不改行为，与冒烟同一路径）。

    agent_client 整轮复用（逐条新建会让上一条的持久 MCP 会话被 GC 在别的 task 里收尾 →
    anyio cancel scope RuntimeError 噪音）。
    终态码抽取（启发式，首轮实跑后可按真实工具结果结构收紧）：从工具结果文本 + 最终答案里正则
    捞 9 位码；``final_code`` 取最终答案里最后出现的码（agent"落定"的），无则退回工具结果里最后一个。
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
                tool_names.append(d["name"])           # 工具结果也计名（含哑火收编的 ask_clarification）
            c = d.get("content")
            if isinstance(c, str):
                tool_texts.append(c)
            elif isinstance(c, list):
                tool_texts.extend(x.get("text", "") for x in c if isinstance(x, dict))

    answer = "".join(answer_parts)
    import re
    code_re = re.compile(r"\b(\d{9})(?:\d{3})?\b")
    codes_tools = [m.group(1) for t in tool_texts for m in code_re.finditer(t)]
    codes_ans = [m.group(1) for m in code_re.finditer(answer)]
    final = codes_ans[-1] if codes_ans else (codes_tools[-1] if codes_tools else None)
    return RunObservation(
        codes=list(dict.fromkeys(codes_tools + codes_ans)),
        final_code=final,
        tools=list(dict.fromkeys(tool_names)),
        answer=answer[:2000],
        did_clarify=CLARIFY_TOOL in tool_names,
    )


def main() -> int:
    """逐条跑 × pass_k、判定、聚合、（可选）挂 Langfuse。"""
    p = argparse.ArgumentParser(description="cost_task 端到端组价评测（pass^k + 红线独立）")
    p.add_argument("--run-name", default=None, help="dataset run 名（建议带 prompt variant）")
    p.add_argument("--split", default="test", help="只跑该 split（默认 test；dev 调阈值用）")
    p.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    p.add_argument("--model", default=None, help="覆盖模型名，如 qwen3-8b")
    p.add_argument("--no-langfuse", action="store_true", help="不挂 Langfuse，只在终端出聚合")
    args = p.parse_args()

    import uuid
    run_name = args.run_name or f"cost-task-{uuid.uuid4().hex[:8]}"
    # thread_id 掺进程级随机后缀：checkpointer 持久化，--run-name 重名会静默续跑旧对话
    # （routing 实锤 cost_workflow_resume + 60k 撞上限），后缀保证每次进程全新 thread。
    nonce = uuid.uuid4().hex[:6]
    cases = _load_cases(args.split, args.limit)
    if not cases:
        print(f"没有匹配的 case（split={args.split}）")
        return 1

    lf = None
    if not args.no_langfuse:
        from _lf import require_langfuse, wait_for_traces
        lf = require_langfuse()

    # 整轮共用一个客户端（见 _observe 注释）。
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
            except Exception as exc:  # noqa: BLE001 —— 单次崩不拖垮（记为该次 task 不过）
                print(f"    run {k} 跑挂：{type(exc).__name__}: {exc}")
                runs.append(score_run(case, RunObservation()))  # 空观测 → task 不过
                continue
            rs = score_run(case, obs)
            runs.append(rs)
            if lf is not None:
                traces = wait_for_traces(lf, session_id=thread_id, expected=1)
                tid = getattr(traces[0], "id", None) if traces else None
                if tid:
                    lf.api.dataset_run_items.create(run_name=run_name, dataset_item_id=case["id"], trace_id=tid)
                    lf.create_score(name="task_pass", value=float(rs.task_pass), trace_id=tid,
                                    data_type="NUMERIC", comment=f"final_code={obs.final_code} terminal={rs.terminal}")
                    lf.create_score(name="redline_ok", value=float(rs.redline_ok), trace_id=tid,
                                    data_type="NUMERIC", comment=f"policy={rs.policy}")
        res = aggregate_passk(case, runs)
        results.append(res)
        print(f"[{i + 1}/{len(cases)}] {res.case_id} ({res.difficulty}) "
              f"pass^{res.pass_k}={res.task_passk} redline_clean={res.redline_clean} overall={res.overall_pass}")

    if lf is not None:
        lf.flush()

    rep = aggregate_suite(results)
    print("\n========== 聚合（split=%s, model=%s） ==========" % (args.split, args.model or "默认"))
    print(f"用例数              = {rep['n_cases']}")
    print(f"任务成功率 pass^k   = {rep['task_success_passk']:.2%}   （连跑全过，建议门按 difficulty 分档）")
    print(f"红线违规率(独立)    = {rep['redline_violation_rate']:.2%}   （门 = 0）")
    print(f"综合通过率          = {rep['overall_pass_rate']:.2%}   （终态过 且 无红线违规）")
    print(f"红线 evaluable 覆盖 = {rep['policy_evaluable_coverage']:.2%}   （诚实：未判的红线不计入通过）")
    print("逐 difficulty：")
    for d, v in rep["by_difficulty"].items():
        print(f"    {d:<16} {v['pass']}/{v['n']}  ({v['pass_rate']:.0%})")
    if lf is not None:
        print(f"逐条分已挂 Langfuse：Datasets → {DATASET_NAME} → Runs → {run_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
