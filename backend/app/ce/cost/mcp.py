"""MCP primitive invocation helpers for cost workflow nodes."""

from __future__ import annotations

import json
from typing import Any


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
        raw = tool.invoke(clean_args)
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
