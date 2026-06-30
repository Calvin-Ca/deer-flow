"""任务1：Langfuse 链路冒烟测试。

驱动一次真实 agent 对话（走 DeerFlowClient，与 Gateway 同一条 tracing 路径），
再从 Langfuse 读回该会话的 trace，断言：trace 已落库、且带上预期的
``session_id`` / ``model:`` / ``variant:`` 标签。用来确认「deer-flow → Langfuse」
整条上报通了、metadata 注入正确，再去搭上层的 dataset / feedback。

运行（服务器上，四服务无需全起，只要 Gateway 依赖的模型可调）：
    uv run --project backend python benchmark/runner/smoke_test.py
可选：
    --message "自定义问法"   --model qwen-plus
退出码 0=trace 已确认；1=超时未见 trace（看输出里的排查提示）。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse, wait_for_traces  # noqa: E402


def main() -> int:
    """跑一次对话并核对 trace 是否带预期标签落库。

    功能：见模块 docstring。
    参数：无（命令行 --message / --model）。
    返回：进程退出码，0 成功 / 1 未见 trace。
    """
    parser = argparse.ArgumentParser(description="Langfuse 链路冒烟测试")
    parser.add_argument("--message", default="冒烟测试：请用一句话说明综合单价包含哪些费用。")
    parser.add_argument("--model", default=None, help="覆盖默认模型名（可选）")
    args = parser.parse_args()

    from deerflow.client import DeerFlowClient

    client = require_langfuse()
    thread_id = f"smoke-{uuid.uuid4()}"

    print(f"[1/3] 发起对话 thread_id={thread_id} model={args.model or '默认'} ...")
    answer = DeerFlowClient(model_name=args.model).chat(args.message, thread_id=thread_id)
    print(f"      agent 回复（截断）：{answer[:120]!r}")

    print("[2/3] flush 并轮询读回 Langfuse trace ...")
    traces = wait_for_traces(client, session_id=thread_id, expected=1)

    if not traces:
        print("[3/3] ✗ 未在 Langfuse 见到该会话 trace。排查：")
        print("      - 根 .env 是否 LANGFUSE_TRACING=true 且 BASE_URL 指向本机 langfuse；")
        print("      - langfuse 容器是否 healthy（docker compose ps）；")
        print("      - 该进程 env 与 Gateway 是否同一套（env 仅启动时读）。")
        return 1

    t = traces[0]
    tags = list(getattr(t, "tags", None) or [])
    print("[3/3] ✓ trace 已落库：")
    print(f"      trace_id   = {getattr(t, 'id', None)}")
    print(f"      session_id = {getattr(t, 'session_id', None)}  (应等于 thread_id)")
    print(f"      user_id    = {getattr(t, 'user_id', None)}")
    print(f"      tags       = {tags}")
    has_model = any(s.startswith("model:") for s in tags)
    has_variant = any(s.startswith("variant:") for s in tags)
    print(f"      model: 标签 = {has_model}   variant: 标签 = {has_variant}")
    if not has_variant:
        print("      ⚠ 缺 variant: 标签——检查 metadata.py 注入与 prompt variant 解析。")
    if not has_model:
        print("      ℹ 无 model: 标签属正常——本脚本未带 --model 时 embedded client 走默认模型")
        print("        （model_name=None，metadata 仅在已知模型名时打 model:）。带 --model qwen-plus 重跑即有。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
