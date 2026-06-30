"""Tests for feedback → Langfuse score push (app.gateway.routers.feedback).

覆盖 ``_push_langfuse_feedback_score`` 的三条路径：启用时按确定性 trace_id 回写
score、未启用时 no-op、任何异常都被吞掉不外抛（best-effort，绝不阻断反馈接口）。
"""

from __future__ import annotations

import deerflow.tracing as tracing
from app.gateway.routers.feedback import _push_langfuse_feedback_score


def test_push_score_calls_create_score_when_enabled(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def create_score(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(tracing, "build_langfuse_trace_id", lambda run_id: f"trace-{run_id}")
    monkeypatch.setattr(tracing, "get_langfuse_client", lambda: FakeClient())

    _push_langfuse_feedback_score("run-9", 1, "great")

    assert captured["trace_id"] == "trace-run-9"
    assert captured["name"] == "user_feedback"
    assert captured["value"] == 1.0
    assert captured["data_type"] == "NUMERIC"
    assert captured["comment"] == "great"


def test_push_score_noop_when_disabled(monkeypatch):
    calls: list = []

    class FakeClient:
        def create_score(self, **kwargs):
            calls.append(kwargs)

    # langfuse 未启用 → build 返回 None → 不应触达 create_score
    monkeypatch.setattr(tracing, "build_langfuse_trace_id", lambda run_id: None)
    monkeypatch.setattr(tracing, "get_langfuse_client", lambda: FakeClient())

    _push_langfuse_feedback_score("run-9", -1, None)

    assert calls == []


def test_push_score_swallows_exceptions(monkeypatch):
    def boom(run_id):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(tracing, "build_langfuse_trace_id", boom)

    # 不得外抛——反馈写库已成功，回写失败只记日志
    _push_langfuse_feedback_score("run-9", 1, None)
