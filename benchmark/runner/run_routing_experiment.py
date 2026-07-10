"""任务2b：路由评测——逐条跑 agent、读回 trace、挂分到 Langfuse Dataset Run。

对 ``agent-routing-eval`` 数据集逐条把 query 喂给默认 lead agent（skill-only），
从 agent 的工具调用里程序化判定两项（对标 routing_eval/README 的两率）：
    - clarify_correct：是否「该反问就反问」（命中 ask_clarification 工具）；
    - route_correct ：是否「该调脚本就调」（工具调用/参数命中 ROUTE_SIGNALS）。
每条跑完读回其 Langfuse trace，调 ``dataset_run_items.create`` 关联进同名 dataset
run（UI 里 Datasets→Runs 可横向比 prompt variant），并 ``create_score`` 挂两项分。

为什么不用 ``dataset.run_experiment``：它在自己的事件循环线程里同步调 task，而
``DeerFlowClient.stream`` 内部自驱 async + 持久 MCP（streamable_http）会话，二者
生命周期一打架就崩（cancel scope / Task destroyed）。本脚本改为**主线程、逐条、
不另起 loop**，与冒烟测试完全同构——那条路已验证干净退出。

判定信号是「外部观测启发式」：路由是否发生靠匹配工具名/参数里的 ROUTE_SIGNALS，
先按经验给默认值，跑一轮后照真实 trace 里 agent 的实际调用方式（多半是带 ce-cost
前缀的 MCP 工具名）回调本常量。

运行（服务器上，需四服务起齐使 agent 真能调脚本）：
    uv run --project backend python benchmark/runner/run_routing_experiment.py \
        --run-name v2_runbook --model qwen-plus
退出码 0=完成。两率聚合见终端 + Langfuse UI 的 dataset run。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _paths  # noqa: E402,F401  把 backend/ 补进 sys.path（import app 前置）
from _lf import require_langfuse, wait_for_traces  # noqa: E402

DATASET_NAME = "agent-routing-eval"

# 「发生了路由」= agent 调了正经的知识/算量工具（按工具名判定，可靠：名字在流式
# tool_call 首片里就到齐，不像 args 会分片）。口径见 routing_eval/README：路由 = 调
# 脚本/工具，bash/read_file 自己瞎折腾不算。首轮实跑确认真实工具名为 qa.py / cost.py
# （脚本式工具）与 ce-cost_* / ce-rag_* / ce-db_*（MCP 工具）；新增工具时往这两处加。
ROUTE_TOOL_NAMES = {"qa.py", "cost.py"}
ROUTE_TOOL_PREFIXES = ("ce-cost", "ce-rag", "ce-db")
CLARIFY_TOOL = "ask_clarification"


def _is_route_tool(name: str) -> bool:
    """工具名是否属于「正经路由工具」（精确名或前缀命中）。"""
    return name in ROUTE_TOOL_NAMES or any(name.startswith(p) for p in ROUTE_TOOL_PREFIXES)


def _is_fetch_tool(name: str) -> bool:
    """「真取数」工具——expect_route=False 的违规口径。"""
    return _is_route_tool(name)


def _drive_agent(query: str, model_name: str | None, thread_id: str) -> dict:
    """把一条 query 喂给默认 lead agent，收集其工具调用与最终回复（主线程同步）。

    功能：评测的核心动作——只观测，不改 agent 行为；与冒烟测试同一调用路径。
    参数：query 用户问法；model_name 覆盖模型名（None 用默认）；thread_id 本条会话 id
        （即 langfuse session_id，跑完据此读回 trace）。
    返回：dict —— answer 最终文本、tool_names 工具名列表、did_clarify/did_route 两判定。
    """
    from deerflow.client import DeerFlowClient

    tool_names: list[str] = []  # 只收非空工具名（流式后续分片 name 为空，跳过）
    answer_parts: list[str] = []

    for ev in DeerFlowClient(model_name=model_name).stream(query, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        d = ev.data
        if d.get("type") == "ai":
            for tc in d.get("tool_calls", []) or []:
                name = tc.get("name") or ""
                if name:
                    tool_names.append(name)
            if isinstance(d.get("content"), str):
                answer_parts.append(d["content"])
        elif d.get("type") == "tool" and d.get("name"):
            # 工具结果也计名（m4-behavior-v3 归因，2026-07-06）：哑火收编（after_model 把纯文本
            # 反问转 ask_clarification）不经模型流式 tool_calls 出来，只有 ClarificationMiddleware
            # 的 ToolMessage 可观测——只数 ai tool_calls 会把收编成功误判成「工具=[]」（E6 冤案）。
            tool_names.append(d["name"])

    tool_names = list(dict.fromkeys(tool_names))  # 去重保序（tool_call 与其结果各计一次的重影）
    return {
        "answer": "".join(answer_parts)[:500],
        "tool_names": tool_names,
        "did_clarify": CLARIFY_TOOL in tool_names,
        "did_route": any(_is_route_tool(n) for n in tool_names),
        "did_fetch": any(_is_fetch_tool(n) for n in tool_names),
    }


def _match(expected: object, actual: bool) -> bool | None:
    """期望值与实际行为是否一致；期望缺失时返回 None（该项不计分）。"""
    if expected is None:
        return None
    return bool(expected) == actual


def main() -> int:
    """逐条跑数据集、挂分、聚合两率。

    功能：见模块 docstring。
    参数：无（命令行 --run-name / --model）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="路由评测：逐条跑 agent 并挂分到 Langfuse Dataset Run")
    parser.add_argument("--run-name", default=None, help="dataset run 名（建议填 prompt variant，便于横向比）")
    parser.add_argument("--model", default=None, help="覆盖模型名，如 qwen-plus")
    args = parser.parse_args()

    client = require_langfuse()
    run_name = args.run_name or f"routing-{uuid.uuid4().hex[:8]}"
    dataset = client.get_dataset(DATASET_NAME)

    rows: list[dict] = []
    for i, item in enumerate(dataset.items):
        query = (item.input or {}).get("query", "")
        thread_id = f"exp-{run_name}-{item.id}"
        try:
            out = _drive_agent(query, args.model, thread_id)
        except Exception as exc:  # noqa: BLE001 —— 单条崩不拖垮整轮（v3 实测一条异常废了后续 22 条）
            print(f"[{i + 1}/{len(dataset.items)}] {item.id} 跑挂了，跳过（不挂分）：{type(exc).__name__}: {exc}")
            continue

        exp = item.expected_output or {}
        # expect_route=False（出界/域外）的违规口径按「真取数」判：前门 orchestrate 合规（见 _is_fetch_tool）
        actual_route = out["did_route"] if exp.get("expect_route") else out["did_fetch"]
        route_ok = _match(exp.get("expect_route"), actual_route)
        clarify_ok = _match(exp.get("expect_clarify"), out["did_clarify"])
        # 单轮口径修正（m4-behavior-v1 归因定案）：金标「先反问、答后再路由」的用例
        # （expect_clarify 与 expect_route 同真），agent 正确止步于反问等人时，路由
        # 「未到环节」——原口径记 ✗ 是冤判（v1 里 B2/E1/E2/E5/E6 全属此类），改不计分。
        if exp.get("expect_clarify") and exp.get("expect_route") and out["did_clarify"] and not out["did_route"]:
            route_ok = None

        # 读回本条 trace，关联进 dataset run 并挂分
        traces = wait_for_traces(client, session_id=thread_id, expected=1)
        trace_id = getattr(traces[0], "id", None) if traces else None
        if trace_id:
            client.api.dataset_run_items.create(run_name=run_name, dataset_item_id=item.id, trace_id=trace_id)
            if route_ok is not None:
                client.create_score(name="route_correct", value=1.0 if route_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"期望调脚本={bool(exp.get('expect_route'))} 实际={actual_route} 工具={out['tool_names']}")
            if clarify_ok is not None:
                client.create_score(name="clarify_correct", value=1.0 if clarify_ok else 0.0, trace_id=trace_id, data_type="NUMERIC", comment=f"期望反问={bool(exp.get('expect_clarify'))} 实际={out['did_clarify']}")

        rows.append({"id": item.id, "group": (item.metadata or {}).get("group"), "exp": exp, "out": out, "route_ok": route_ok, "clarify_ok": clarify_ok, "trace_id": trace_id})
        print(f"[{i + 1}/{len(dataset.items)}] {item.id} route_ok={route_ok} clarify_ok={clarify_ok} 工具={out['tool_names']}")

    client.flush()

    # 聚合两率（口径见 routing_eval/README）：
    # 路由率 = expect_route=True 且到达路由环节的用例里真去调脚本的比例
    # （route_ok=None 的「正确止步于反问」条目不入分母，单轮口径修正见上）
    route_set = [r for r in rows if r["exp"].get("expect_route") is True and r["route_ok"] is not None]
    clarify_set = [r for r in rows if r["exp"].get("expect_clarify") is True]
    n_halted = sum(1 for r in rows if r["exp"].get("expect_route") is True and r["route_ok"] is None)
    route_rate = sum(r["out"]["did_route"] for r in route_set) / len(route_set) if route_set else float("nan")
    clarify_rate = sum(r["out"]["did_clarify"] for r in clarify_set) / len(clarify_set) if clarify_set else float("nan")

    print("\n========== 聚合 ==========")
    print(f"run_name        = {run_name}   model = {args.model or '默认'}")
    print(f"路由率           = {route_rate:.2%}  ( {sum(r['out']['did_route'] for r in route_set)}/{len(route_set)} ，另 {n_halted} 条正确止步于反问不计，建议门 ≥0.8 )")
    print(f"红线遵守率(反问) = {clarify_rate:.2%}  ( {sum(r['out']['did_clarify'] for r in clarify_set)}/{len(clarify_set)} ，建议门 ≥0.95 )")
    print("逐条分数已挂到 Langfuse：Datasets → agent-routing-eval → Runs → " + run_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
