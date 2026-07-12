"""quota_engine 定额方案推荐引擎单测（monkeypatch 掉 MCP 与 LLM 排序，纯逻辑验证）。"""
from __future__ import annotations

from typing import Any

import app.ce.cost.quota_engine as qe
from app.ce.cost.quota_engine import quota_recommend_tool, rank_schemes, recommend_quota

_SCHEMES_MULTI = [
    {"scheme_id": "S1", "name": "泵送商品混凝土方案", "score": 0.7},
    {"scheme_id": "S2", "name": "非泵送现拌方案", "score": 0.6},
]


def _fake_mcp(status="ok", schemes=None):
    def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "ce-db_price_compose"
        if status != "ok":
            return {"status": status, "tool": name, "error": "unavailable"}
        inner: dict[str, Any] = {"quotas": [{"code": "AA0001"}], "price_status": "ok"}
        if schemes is not None:
            inner["schemes"] = schemes
        return {"status": "ok", "tool": name, "result": inner}
    return _call


# ── spec 口径闸：2024 两侧硬拒 ──
def test_unsupported_spec_rejected():
    r = recommend_quota("010402001", spec="2024", call_tool=_fake_mcp())
    assert r["status"] == "unsupported_spec" and "2013" in r["message"]


# ── 缺编码 → awaiting_input（本引擎不代选码） ──
def test_missing_code_awaits_input():
    r = recommend_quota("", call_tool=_fake_mcp())
    assert r["status"] == "awaiting_input" and r["required_fields"] == ["code"]


# ── 取数失败 → blocked 透传（服务不可用 ≠ 没有方案） ──
def test_blocked_on_service_error():
    r = recommend_quota("010402001", call_tool=_fake_mcp(status="error"))
    assert r["status"] == "blocked" and r["error"] is not None


# ── 0/1 套方案 → 自动采用，不触发 LLM 排序 ──
def test_no_alternatives_done():
    r = recommend_quota("010402001", call_tool=_fake_mcp(), llm_call=lambda *_: (_ for _ in ()).throw(AssertionError("不应调 LLM")))
    assert r["status"] == "done" and r["selection_source"] == "no_alternatives"
    assert r["compose"]["price_status"] == "ok"  # 组价数据原样透传


def test_single_scheme_auto_adopt():
    r = recommend_quota("010402001", call_tool=_fake_mcp(schemes=[_SCHEMES_MULTI[0]]))
    assert r["status"] == "done" and r["selection_source"] == "auto_single_scheme"
    assert r["selected_scheme"]["scheme_id"] == "S1"


# ── 多方案 → need_review + LLM 预排建议（推荐仅供参考，不自动定稿） ──
def test_multi_scheme_need_review_with_recommendation():
    fake_rank = lambda feature, schemes: {"recommended_scheme_id": "S1", "rationale": "特征含泵送"}  # noqa: E731
    r = recommend_quota("010402001", feature="C30 泵送", call_tool=_fake_mcp(schemes=_SCHEMES_MULTI), llm_call=fake_rank)
    assert r["status"] == "need_review" and len(r["schemes"]) == 2
    assert r["recommendation"]["recommended_scheme_id"] == "S1"


# ── LLM 排序 fail-open：异常/无效输出 → 无推荐，候选照常 ──
def test_rank_fail_open_on_llm_error():
    boom = lambda *_: (_ for _ in ()).throw(RuntimeError("model down"))  # noqa: E731
    r = recommend_quota("010402001", call_tool=_fake_mcp(schemes=_SCHEMES_MULTI), llm_call=boom)
    assert r["status"] == "need_review" and "recommendation" not in r


def test_rank_rejects_out_of_candidate_id():
    fake_rank = lambda feature, schemes: qe._parse_rank_output('{"scheme_id": "S9", "rationale": "编的"}', {"S1", "S2"})  # noqa: E731
    assert rank_schemes("f", _SCHEMES_MULTI, llm_call=fake_rank) is None


def test_rank_parse_strips_think_block():
    out = qe._parse_rank_output('<think>S2 也行？</think>{"scheme_id": "S1", "rationale": "泵送匹配"}', {"S1", "S2"})
    assert out == {"recommended_scheme_id": "S1", "rationale": "泵送匹配"}


def test_rank_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CE_QUOTA_RANK_ENABLED", "0")
    assert rank_schemes("f", _SCHEMES_MULTI, llm_call=lambda *_: {"recommended_scheme_id": "S1", "rationale": "x"}) is None


# ── 工具封装可用 ──
def test_tool_wrapper_invocable(monkeypatch):
    monkeypatch.setattr(qe, "call_mcp_tool", _fake_mcp(schemes=[_SCHEMES_MULTI[0]]))
    out = quota_recommend_tool.invoke({"code": "010402001", "feature": "加气砌块墙"})
    assert out["status"] == "done" and out["selected_scheme"]["scheme_id"] == "S1"
