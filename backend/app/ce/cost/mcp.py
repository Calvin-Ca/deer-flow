"""MCP primitive invocation helpers for cost workflow nodes."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any


def _run_in_fresh_loop(tool: Any, args: dict[str, Any]) -> Any:
    """在**专属新 event loop** 里跑 MCP 工具的异步路径并干净收尾。

    HTTP-MCP（streamable_http）工具每次调用会开/关一个 anyio task group 会话。用 ``tool.invoke``（sync）
    从"无常驻 loop"的上下文（裸脚本、LangGraph 的 sync-tool 线程）驱动时，会话的异步生成器会在与创建
    时不同的任务里被拆解 → ``cancel scope in a different task`` / ``Task destroyed but pending`` 噪声
    （功能其实成功了，噪声只在清理阶段）。这里自建一个 loop 跑 ``ainvoke``，跑完**先 shutdown_asyncgens
    把 streamable_http 的异步生成器全部关掉、再 close loop**，让清理同步发生在本函数内，而非进程退出时。
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(tool.ainvoke(args))
        loop.run_until_complete(loop.shutdown_asyncgens())  # 排空 streamable_http 异步生成器，杜绝退出期噪声
        return result
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _invoke_tool_clean(tool: Any, args: dict[str, Any]) -> Any:
    """驱动 MCP 工具且不产生 async 拆解噪声（兼容"当前线程已有运行中 loop"的罕见情形）。

    - 当前线程**无**运行中 loop（裸脚本 / LangGraph sync-tool 线程，最常见）→ 直接在本线程建 fresh loop 跑。
    - 当前线程**有**运行中 loop（从 async 上下文直调，罕见）→ offload 到一次性线程里用它自己的 fresh loop 跑，
      避免嵌套 ``run_until_complete``。
    工具无 ``ainvoke``（非 langchain 工具）时回退 ``invoke``。真错误照常向上抛，由 ``call_mcp_tool`` 归一。
    """
    if not hasattr(tool, "ainvoke"):
        return tool.invoke(args)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_in_fresh_loop(tool, args)  # 无运行中 loop：本线程直接跑
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:  # 有运行中 loop：隔离到线程
        return ex.submit(_run_in_fresh_loop, tool, args).result()


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
