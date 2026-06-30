"""Langfuse 评测脚本共享小工具。

本模块只放「脚本之间复用、且不值得进 harness」的薄封装：取已配置的 Langfuse
客户端、轮询读回 trace。客户端口径与后端一致——直接复用
``deerflow.tracing.get_langfuse_client``（host 取 LANGFUSE_BASE_URL），避免脚本
另起一套凭据解析而连错地址。

运行前提（服务器上）：根 .env 已开 LANGFUSE_TRACING=true 并配好
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL，且用
``uv run --project backend python ...`` 执行，使 deerflow / langfuse 可导入。
"""

from __future__ import annotations

import time
from typing import Any


def require_langfuse() -> Any:
    """取已配置的 Langfuse 客户端，未启用则直接报错退出。

    功能：所有评测脚本的统一入口校验——没开 langfuse 就别往下跑。
    参数：无。
    返回：已配置的 ``Langfuse`` 客户端。
    异常：``SystemExit`` —— 当 langfuse 未启用/凭据不全时。
    """
    from deerflow.tracing import get_langfuse_client

    client = get_langfuse_client()
    if client is None:
        raise SystemExit(
            "Langfuse 未启用或凭据不全：请在根 .env 设 LANGFUSE_TRACING=true 及 "
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL 后重试。"
        )
    return client


def wait_for_traces(client: Any, *, session_id: str, expected: int = 1, retries: int = 12, interval: float = 2.0) -> list[Any]:
    """轮询读回某 session 下的 trace（langfuse ingestion 是异步批量，需等落库）。

    功能：发起 run 后 trace 不会立刻可查，先 flush 再带重试地拉取。
    参数：client Langfuse 客户端；session_id 即 thread_id（建 trace 时已写入）；
        expected 期望至少出现的 trace 数；retries 重试次数；interval 每次间隔秒。
    返回：命中的 trace 列表（可能为空表示超时未见）。
    """
    client.flush()
    for _ in range(retries):
        traces = client.api.trace.list(session_id=session_id, limit=50).data
        if len(traces) >= expected:
            return traces
        time.sleep(interval)
    return client.api.trace.list(session_id=session_id, limit=50).data
