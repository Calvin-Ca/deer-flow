"""MCP primitive invocation helpers for cost workflow nodes."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

# ── 进程级常驻"桥接 loop"线程 ──────────────────────────────────────────────────
# HTTP-MCP（streamable_http）工具每次 ainvoke 会用 anyio task group 开/关一个会话；anyio 的 cancel scope
# 要求**进入与退出在同一个 task**。用 tool.invoke(sync) 或"每次换个新 loop + shutdown_asyncgens"驱动时，
# 会话的异步生成器会在异任务里被拆解 → `cancel scope in a different task` / `Task destroyed` / `athrow
# already running` 噪声（功能其实成功，噪声只在清理阶段）。
# 解法：所有 MCP 异步调用都投到**同一个常驻后台 loop**上跑——会话的开/用/关全落在这个 loop 的同一批 task 里，
# 永不跨 task、永不被外部强关。缓存的 MCP 客户端会话也稳定绑在此 loop 上，多次调用一致。
_BRIDGE_LOOP: asyncio.AbstractEventLoop | None = None
_BRIDGE_LOCK = threading.Lock()


def _bridge_loop() -> asyncio.AbstractEventLoop:
    """取（惰性建）常驻桥接 event loop（跑在专属 daemon 线程上，进程存活期间一直运行）。"""
    global _BRIDGE_LOOP
    with _BRIDGE_LOCK:
        if _BRIDGE_LOOP is not None and not _BRIDGE_LOOP.is_closed():
            return _BRIDGE_LOOP
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, name="ce-mcp-bridge-loop", daemon=True).start()
        _BRIDGE_LOOP = loop
        return loop


def _invoke_tool_clean(tool: Any, args: dict[str, Any]) -> Any:
    """在常驻桥接 loop 上跑 MCP 工具的异步路径，避免 async 拆解噪声；阻塞等结果返回。

    工具无 ``ainvoke``（非 langchain 工具）时回退 ``invoke``。真错误照常向上抛，由 ``call_mcp_tool`` 归一。
    注：调用线程会阻塞等桥接 loop 出结果——本项目里 MCP 调用都发生在 sync 上下文（裸脚本 / LangGraph
    的 sync-tool 线程），阻塞的是调用线程、非任何运行中的 loop，安全。
    """
    if not hasattr(tool, "ainvoke"):
        return tool.invoke(args)
    fut = asyncio.run_coroutine_threadsafe(tool.ainvoke(args), _bridge_loop())
    return fut.result()


def _parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {"text": text}


def _content_to_data(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        return _parse_json_text(content)
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif isinstance(item, str):
                texts.append(item)
        if texts:
            return _parse_json_text("\n".join(texts))
        return content
    return {"value": repr(content)}


def normalize_tool_result(result: Any) -> Any:
    """Convert LangChain/MCP tool return shapes into plain JSON-like data."""
    if isinstance(result, tuple) and len(result) == 2:
        content, artifact = result
        if isinstance(artifact, dict):
            structured = artifact.get("structured_content")
            if structured is not None:
                return structured
        return _content_to_data(content)

    content = getattr(result, "content", None)
    artifact = getattr(result, "artifact", None)
    if artifact and isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        if structured is not None:
            return structured
    if content is not None:
        return _content_to_data(content)

    return _content_to_data(result)


def call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke an already-configured MCP primitive by DeerFlow tool name."""
    from deerflow.mcp.cache import get_cached_mcp_tools

    tools = {tool.name: tool for tool in get_cached_mcp_tools()}
    tool = tools.get(name)
    if tool is None:
        available = sorted(tool_name for tool_name in tools if tool_name.startswith(("ce-rag_", "ce-db_")))
        return {
            "status": "tool_unavailable",
            "tool": name,
            "available_ce_tools": available,
        }

    clean_args = {key: value for key, value in arguments.items() if value is not None}
    try:
        raw = _invoke_tool_clean(tool, clean_args)
    except Exception as exc:
        return {
            "status": "error",
            "tool": name,
            "arguments": clean_args,
            "error": str(exc),
        }
    return {
        "status": "ok",
        "tool": name,
        "arguments": clean_args,
        "result": normalize_tool_result(raw),
    }
