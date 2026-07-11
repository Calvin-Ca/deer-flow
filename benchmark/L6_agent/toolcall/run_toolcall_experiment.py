"""任务2d：工具调用评测——逐条跑 agent、按 arg_match 判调用对错、挂分到 Langfuse。

对 ``agent-toolcall-eval`` 逐条把 query 喂默认 lead agent，收集其**完整** tool_calls
（name + args），与金标 ``expected_call`` 比：
    - tool_correct ：是否调了期望的工具（按工具名）；
    - call_correct ：工具名对 **且** args 按 ``arg_match`` 口径匹配（端到端调用正确）。
每条跑完读回 trace → ``dataset_run_items.create`` 关联进 dataset run + ``create_score`` 挂分。

为什么能拿到完整 args（不像路由只敢按工具名判）：``DeerFlowClient.stream`` 的
``_serialize_tool_calls`` 发 ``{name, args, id}``，且 values 快照会发**最终完整**的
tool_calls；本脚本按 id「后写覆盖」累积，最终取到的就是完整 args（见 client.py
``_serialize_tool_calls`` / values 分支）。

⚠️ 金标的 ``expected_call.tool`` 必须是**真实 agent 工具名**（qa.py / cost.py /
ce-cost_* 等）。sample 里的 ``query_bill_8100`` 是 AGENT_BENCHMARK 理想名，照搬会恒不
命中——补真金标时按真实工具名写（见 upload_datasets.upload_toolcall 注释）。

前置：先 ``upload_datasets.py --only toolcall`` 灌好数据集。

运行（服务器上，需四服务起齐使 agent 真能调工具）：
    uv run --project backend python benchmark/L6_agent/toolcall/run_toolcall_experiment.py \
        --run-name toolcall_v1 --model qwen-plus
退出码 0=完成。指标见终端 + Langfuse UI 的 dataset run。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))  # _lf / _paths / upload_datasets

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path（import app 前置）
from _lf import require_langfuse, wait_for_traces  # noqa: E402
from upload_datasets import TOOLCALL_DATASET  # noqa: E402


def _collect_tool_calls(agent_client, query: str, thread_id: str) -> dict[str, dict]:
    """把一条 query 喂默认 lead agent，收集其完整 tool_calls（name+args，按 id 去重）。

    功能：评测核心动作——只观测不改 agent 行为；按 tool_call id「后写覆盖」，使流式早期
        的残缺 args 被 values 快照里的完整 args 覆盖，最终拿到完整调用。
    参数：agent_client 整轮复用的 DeerFlowClient（逐条新建会让上一条的持久 MCP 会话被 GC
        在别的 task 里收尾 → anyio cancel scope RuntimeError 噪音）；query 用户问法；
        thread_id 会话 id（= langfuse session_id，跑完据此读回 trace）。
    返回：``{tool_call_id: {"name": str, "args": dict}}``——本轮 agent 发出的全部工具调用。
    """
    calls: dict[str, dict] = {}
    fallback_idx = 0
    for ev in agent_client.stream(query, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        d = ev.data
        if d.get("type") != "ai":
            continue
        for tc in d.get("tool_calls", []) or []:
            name = tc.get("name") or ""
            if not name:
                continue
            tc_id = tc.get("id")
            if not tc_id:  # 个别工具调用无 id，用稳定序号兜底避免互相覆盖
                tc_id = f"_noid_{fallback_idx}"
                fallback_idx += 1
            calls[tc_id] = {"name": name, "args": tc.get("args") or {}}
    return calls


def _args_match(expected: dict, actual: dict, mode: str) -> bool:
    """按 arg_match 口径比对参数。

    功能：exact=完全相等；subset=期望每个键值都在实际里相等（默认，容忍 agent 多带参数）；
        semantic=暂按 subset 近似（未接语义判等）。
    参数：expected 金标 args；actual agent 实发 args；mode exact|subset|semantic。
    返回：是否匹配。
    """
    expected = expected or {}
    actual = actual or {}
    if mode == "exact":
        return expected == actual
    # subset / semantic：期望子集命中（值按字符串归一化比，容忍 int/str 差异）
    return all(str(actual.get(k)).strip() == str(v).strip() for k, v in expected.items())


def main() -> int:
    """逐条跑工具调用评测、挂分、聚合两率。

    功能：见模块 docstring。
    参数：无（命令行 --run-name / --model）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="工具调用评测：逐条跑 agent 按 arg_match 判定并挂分到 Langfuse")
    parser.add_argument("--run-name", default=None, help="dataset run 名（建议带模型/量化档，便于横向比）")
    parser.add_argument("--model", default=None, help="覆盖模型名，如 qwen-plus")
    args = parser.parse_args()

    client = require_langfuse()
    run_name = args.run_name or f"toolcall-{uuid.uuid4().hex[:8]}"
    dataset = client.get_dataset(TOOLCALL_DATASET)

    # 整轮共用一个客户端（见 _collect_tool_calls 注释）。
    from deerflow.client import DeerFlowClient

    agent_client = DeerFlowClient(model_name=args.model)

    rows: list[dict] = []
    for i, item in enumerate(dataset.items):
        query = (item.input or {}).get("query", "")
        thread_id = f"exp-{run_name}-{item.id}"
        calls = _collect_tool_calls(agent_client, query, thread_id)

        exp = item.expected_output or {}
        want_tool = exp.get("tool")
        mode = exp.get("arg_match", "subset")
        names = [c["name"] for c in calls.values()]
        tool_ok = want_tool in names
        # call_ok：在「调了期望工具」的那些调用里，存在一个 args 也匹配的
        call_ok = any(c["name"] == want_tool and _args_match(exp.get("args") or {}, c["args"], mode) for c in calls.values())

        traces = wait_for_traces(client, session_id=thread_id, expected=1)
        trace_id = getattr(traces[0], "id", None) if traces else None
        if trace_id:
            client.api.dataset_run_items.create(run_name=run_name, dataset_item_id=item.id, trace_id=trace_id)
            client.create_score(name="tool_correct", value=1.0 if tool_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"期望工具={want_tool} 实发={names}")
            client.create_score(name="call_correct", value=1.0 if call_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"arg_match={mode} 期望args={exp.get('args')}")

        rows.append({"id": item.id, "tool_ok": tool_ok, "call_ok": call_ok, "names": names, "trace_id": trace_id})
        print(f"[{i + 1}/{len(dataset.items)}] {item.id} tool_ok={tool_ok} call_ok={call_ok} 实发={names}")

    client.flush()

    n = len(rows)
    tool_rate = sum(r["tool_ok"] for r in rows) / n if n else float("nan")
    call_rate = sum(r["call_ok"] for r in rows) / n if n else float("nan")
    print("\n========== 聚合 ==========")
    print(f"run_name = {run_name}   model = {args.model or '默认'}   n = {n}")
    print(f"工具命中率 tool_correct = {tool_rate:.0%}")
    print(f"调用正确率 call_correct = {call_rate:.0%}（工具名对 + args 按 arg_match 命中）")
    print(f"逐条分数已挂到：Datasets → {TOOLCALL_DATASET} → Runs → {run_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
