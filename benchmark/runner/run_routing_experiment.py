"""任务2b：在 Langfuse Dataset 上跑路由评测实验并自动打分。

对 ``agent-routing-eval`` 数据集逐条把 query 喂给默认 lead agent（skill-only），
从 agent 的工具调用里程序化判定两项（对标 routing_eval/README 的两率）：
    - clarify_correct：是否「该反问就反问」（命中 ask_clarification 工具）；
    - route_correct ：是否「该调脚本就调」（工具调用/ bash 命中 qa.py/cost.py 等信号）。
经 ``dataset.run_experiment`` 自动建 dataset run、把每条结果与分数挂到 Langfuse，
便于按不同 prompt variant / 模型横向比，沉淀 AGENT_BENCHMARK §0 的判定门。

判定信号是「外部观测启发式」：路由是否发生靠匹配工具名/命令里的 ROUTE_SIGNALS，
先按经验给默认值，跑一轮后照真实 trace 里 agent 的实际调用方式回调本常量。

运行（服务器上，需四服务起齐使 agent 真能调脚本）：
    uv run --project backend python benchmark/runner/run_routing_experiment.py \
        --run-name v2_runbook --model qwen-plus
退出码 0=实验完成。聚合两率见输出 + Langfuse UI 的 dataset run。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402

DATASET_NAME = "agent-routing-eval"

# 「发生了路由（调了 qa.py/cost.py）」的观测信号：命中工具名或 bash 命令文本任一即算。
# 跑首轮后照真实 trace 调整（例如换成具体 MCP 工具名 / 知识服务端点）。
ROUTE_SIGNALS = ("qa.py", "cost.py", "knowledge", ":8100", ":8101")
CLARIFY_TOOL = "ask_clarification"


def _drive_agent(query: str, model_name: str | None) -> dict:
    """把一条 query 喂给默认 lead agent，收集其工具调用与最终回复。

    功能：评测的 task 主体——只观测，不改 agent 行为。
    参数：query 用户问法；model_name 覆盖模型名（None 用默认）。
    返回：dict —— answer 最终文本、tool_names 工具名列表、did_clarify/did_route 两判定、
        thread_id 便于回 Langfuse 查这条 agent 自身的 trace。
    """
    from deerflow.client import DeerFlowClient

    thread_id = f"exp-routing-{uuid.uuid4()}"
    tool_names: list[str] = []
    blobs: list[str] = []  # 工具名 + bash 命令文本，供 ROUTE_SIGNALS 匹配
    answer_parts: list[str] = []

    for ev in DeerFlowClient(model_name=model_name).stream(query, thread_id=thread_id):
        if ev.type != "messages-tuple":
            continue
        d = ev.data
        if d.get("type") == "ai":
            for tc in d.get("tool_calls", []) or []:
                name = tc.get("name") or ""
                tool_names.append(name)
                blobs.append(name)
                blobs.append(json.dumps(tc.get("args", {}), ensure_ascii=False))
            if isinstance(d.get("content"), str):
                answer_parts.append(d["content"])

    haystack = "\n".join(blobs)
    return {
        "answer": "".join(answer_parts)[:500],
        "tool_names": tool_names,
        "did_clarify": CLARIFY_TOOL in tool_names,
        "did_route": any(sig in haystack for sig in ROUTE_SIGNALS),
        "thread_id": thread_id,
    }


def _drive_agent_isolated(query: str, model_name: str | None) -> dict:
    """在全新线程里跑 _drive_agent，隔离事件循环。

    功能：``dataset.run_experiment`` 在自己的事件循环线程里同步调 task，而
        ``DeerFlowClient.stream`` 内部自驱 async + 反复建/拆 MCP（streamable_http）
        会话——两者同线程会撞「cancel scope in a different task」而崩。丢进每条
        独享的新线程（自带干净 loop、跑完即销）即复刻冒烟测试那种无外层 loop 的条件。
    参数：query 用户问法；model_name 覆盖模型名。
    返回：``_drive_agent`` 的结果 dict。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_drive_agent, query, model_name).result()


def _eval_clarify(*, input, output, expected_output=None, **kwargs):
    """评估「该反问就反问」（红线主判据）。

    功能：output.did_clarify 与金标 expect_clarify 比对。
    参数：langfuse evaluator 约定关键字（input/output/expected_output）。
    返回：Evaluation —— value 1.0/0.0，comment 写实际 vs 期望。
    """
    from langfuse import Evaluation

    if not expected_output or expected_output.get("expect_clarify") is None:
        return Evaluation(name="clarify_correct", value=0.0, comment="金标缺 expect_clarify")
    want = bool(expected_output["expect_clarify"])
    got = bool(output.get("did_clarify"))
    return Evaluation(
        name="clarify_correct",
        value=1.0 if got == want else 0.0,
        comment=f"期望反问={want} 实际反问={got}",
    )


def _eval_route(*, input, output, expected_output=None, **kwargs):
    """评估「该调脚本就调」（路由率）。

    功能：output.did_route 与金标 expect_route 比对。
    参数：langfuse evaluator 约定关键字。
    返回：Evaluation —— value 1.0/0.0，comment 含命中的工具名便于排查误判。
    """
    from langfuse import Evaluation

    if not expected_output or expected_output.get("expect_route") is None:
        return Evaluation(name="route_correct", value=0.0, comment="金标缺 expect_route")
    want = bool(expected_output["expect_route"])
    got = bool(output.get("did_route"))
    return Evaluation(
        name="route_correct",
        value=1.0 if got == want else 0.0,
        comment=f"期望调脚本={want} 实际调脚本={got} 工具={output.get('tool_names')}",
    )


def main() -> int:
    """拉取数据集并跑实验。

    功能：见模块 docstring。
    参数：无（命令行 --run-name / --model / --limit）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="在 Langfuse Dataset 上跑路由评测实验")
    parser.add_argument("--run-name", default=None, help="dataset run 名（建议填 prompt variant，便于横向比）")
    parser.add_argument("--model", default=None, help="覆盖模型名，如 qwen-plus")
    args = parser.parse_args()

    require_langfuse()
    from deerflow.client import DeerFlowClient  # noqa: F401  提前触发导入错误更可读
    from langfuse import get_client

    dataset = get_client().get_dataset(DATASET_NAME)

    def task(*, item, **kwargs):
        """run_experiment 的 task：驱动 agent 跑一条用例（隔离线程，避免 loop/MCP 串台）。"""
        query = (item.input or {}).get("query", "")
        return _drive_agent_isolated(query, args.model)

    result = dataset.run_experiment(
        name=args.run_name or f"routing-{uuid.uuid4().hex[:8]}",
        description=f"路由/红线评测 model={args.model or '默认'}",
        task=task,
        evaluators=[_eval_clarify, _eval_route],
        max_concurrency=1,  # Qwen3-8B + 真调脚本，串行更稳，避免抢 GPU/服务
    )

    print("✓ 实验完成。聚合分数见下方与 Langfuse UI 的 dataset run：")
    print(result.format() if hasattr(result, "format") else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
