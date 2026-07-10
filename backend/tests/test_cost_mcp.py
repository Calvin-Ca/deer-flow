"""call_mcp_tool 的干净收尾封装单测（fake async 工具，无需真 MCP 服务）。"""
from __future__ import annotations

import asyncio

from app.ce.cost.mcp import _invoke_tool_clean, call_mcp_tool


class FakeTool:
    """模拟 langchain MCP 工具：有 name + async ainvoke + sync invoke。"""

    def __init__(self, name="ce-rag_fake", result=None, raise_exc=None):
        self.name = name
        self._result = result
        self._raise = raise_exc

    async def ainvoke(self, args):
        if self._raise:
            raise self._raise
        return self._result

    def invoke(self, args):
        return self._result


# ── _invoke_tool_clean：两条上下文路径 ──
def test_invoke_clean_async_no_running_loop():
    # 最常见：裸脚本 / sync-tool 线程，无运行中 loop → 本线程 fresh loop 跑 ainvoke
    assert _invoke_tool_clean(FakeTool(result={"ok": 1}), {}) == {"ok": 1}


def test_invoke_clean_from_running_loop_via_bridge():
    # 罕见：从 async 上下文直调 → 投到常驻桥接 loop（另一线程）跑、阻塞取结果，不嵌套/不死锁
    async def main():
        return _invoke_tool_clean(FakeTool(result="in-loop"), {})

    assert asyncio.run(main()) == "in-loop"


def test_invoke_clean_falls_back_to_sync_without_ainvoke():
    class SyncOnly:
        name = "x"

        def invoke(self, args):
            return "sync"

    assert _invoke_tool_clean(SyncOnly(), {}) == "sync"


def test_invoke_clean_drains_leaked_async_generator():
    # 关键：工具内部起了个没关的异步生成器；shutdown_asyncgens 应把它排空、返回不炸。
    class LeakyTool:
        name = "ce-rag_leaky"

        async def ainvoke(self, args):
            async def _gen():
                yield 1
                yield 2

            g = _gen()
            await g.__anext__()          # 用一半就不管了（模拟 streamable_http 会话未闭合）
            return {"leaked": True}

    assert _invoke_tool_clean(LeakyTool(), {}) == {"leaked": True}


# ── call_mcp_tool：ok / error / unavailable ──
def _patch_tools(monkeypatch, tools):
    monkeypatch.setattr("deerflow.mcp.cache.get_cached_mcp_tools", lambda: tools)


def test_call_ok_filters_none_args(monkeypatch):
    _patch_tools(monkeypatch, [FakeTool(result={"code": "010502001"})])
    out = call_mcp_tool("ce-rag_fake", {"a": 1, "b": None})
    assert out["status"] == "ok"
    assert out["arguments"] == {"a": 1}            # None 入参被过滤
    assert out["result"] == {"code": "010502001"}


def test_call_error_wraps_exception(monkeypatch):
    _patch_tools(monkeypatch, [FakeTool(raise_exc=RuntimeError("boom"))])
    out = call_mcp_tool("ce-rag_fake", {})
    assert out["status"] == "error" and "boom" in out["error"]


def test_call_tool_unavailable_lists_ce_tools(monkeypatch):
    _patch_tools(monkeypatch, [FakeTool(name="ce-db_price_query"), FakeTool(name="ce-rag_search_clause")])
    out = call_mcp_tool("ce-rag_missing", {})
    assert out["status"] == "tool_unavailable"
    assert set(out["available_ce_tools"]) == {"ce-db_price_query", "ce-rag_search_clause"}
