"""bill_match_engine 匹配引擎单测（monkeypatch 掉 MCP，纯逻辑验证）。

核实模式的完整契约由 test_bill_match_tool.py 覆盖（工具壳契约）；本文件重点测
选码模式（match_bill code=None）、门限选定纯函数、以及 select_bill_node 选定后
的特征缺口检查增益。
"""
from __future__ import annotations

from typing import Any

import app.ce.cost.bill_match_engine as engine
import app.ce.cost.nodes as nodes
from app.ce.cost.bill_match_engine import diff_features, match_bill, select_from_candidates

_BILL_TRUTH = {
    "code": "010401004",
    "name": "多孔砖墙",
    "features": ["砖品种、规格、强度等级", "墙体类型", "砂浆强度等级、配合比"],
}
_CANDIDATES = [
    {"code": "010401004", "name": "多孔砖墙", "score": 0.91},
    {"code": "010401003", "name": "实心砖墙", "score": 0.74},
]
_FEATURE = "190 厚多孔砖内墙，M5 混合砂浆，砖品种、规格、强度等级：MU10 多孔砖，墙体类型：内墙，砂浆强度等级、配合比 M5"


def _fake_mcp(bill_status="ok", bill_result=None, match_status="ok", match_result=None):
    """构造 call_mcp_tool 假实现：按工具名返回可控结果。"""
    def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "ce-db_bill_get":
            if bill_status != "ok":
                return {"status": bill_status, "tool": name, "error": "unavailable"}
            return {"status": "ok", "tool": name, "result": bill_result if bill_result is not None else _BILL_TRUTH}
        if name == "ce-rag_match_bill_item":
            if match_status != "ok":
                return {"status": match_status, "tool": name, "error": "unavailable"}
            return {"status": "ok", "tool": name, "result": {"candidates": match_result if match_result is not None else _CANDIDATES}}
        raise AssertionError(f"unexpected tool {name}")
    return _call


# ── 选码模式：高置信自动选定 + 选定后特征缺口检查 ──
def test_select_mode_auto_selects_and_reports_feature_gap(monkeypatch):
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp())
    r = match_bill(feature=_FEATURE, spec="2013")
    assert r["mode"] == "select" and r["status"] == "done"
    assert r["selected_code"] == "010401004"
    assert r["selection_source"] == "auto_confident_candidate"
    assert r["missing_features"] == []  # 特征项名都出现在描述文本里
    assert r["verdict"] == "pass"


def test_select_mode_flags_missing_features_on_selected_code(monkeypatch):
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp())
    r = match_bill(feature="多孔砖内墙", spec="2013")  # 描述不含任何规范特征项名
    assert r["status"] == "done" and r["selected_code"] == "010401004"
    assert set(r["missing_features"]) == set(_BILL_TRUTH["features"])
    assert r["verdict"] == "doubt"
    assert all(f["type"] == "missing_feature" for f in r["findings"])


# ── 选码模式：低置信 / 零召回 / 服务不可用 ──
def test_select_mode_low_confidence_needs_review(monkeypatch):
    close = [
        {"code": "010401004", "name": "多孔砖墙", "score": 0.71},
        {"code": "010401003", "name": "实心砖墙", "score": 0.69},
    ]
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp(match_result=close))
    r = match_bill(feature="砖墙", spec="2013")
    assert r["status"] == "need_review"
    assert "selected_code" not in r
    assert [c["code"] for c in r["candidates"]] == ["010401004", "010401003"]
    assert any(f["type"] == "low_confidence" for f in r["findings"])


def test_select_mode_empty_recall_needs_review(monkeypatch):
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp(match_result=[]))
    r = match_bill(feature="不知道什么构件", spec="2013")
    assert r["status"] == "need_review" and r["count"] == 0
    assert any(f["type"] == "no_candidates" for f in r["findings"])


def test_select_mode_blocked_when_recall_unavailable(monkeypatch):
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp(match_status="error"))
    r = match_bill(feature=_FEATURE, spec="2013")
    assert r["status"] == "blocked" and "error" in r


# ── 选码模式：真值不可得时选码照常返回、只跳过特征检查（best-effort）──
def test_select_mode_truth_unavailable_skips_feature_gap(monkeypatch):
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp(bill_status="error"))
    r = match_bill(feature=_FEATURE, spec="2013")
    assert r["status"] == "done" and r["selected_code"] == "010401004"
    assert r["required_features"] == [] and r["missing_features"] == []
    assert r["verdict"] == "pass"


# ── 核实模式经统一入口仍可用（完整契约回归见 test_bill_match_tool.py）──
def test_verify_mode_via_match_bill(monkeypatch):
    monkeypatch.setattr(engine, "call_mcp_tool", _fake_mcp())
    r = match_bill(feature=_FEATURE, spec="2013", code="010401004001")
    assert r["mode"] == "verify" and r["status"] == "done"
    assert r["code9"] == "010401004"
    assert r["recall"]["hit"] is True and r["recall"]["rank"] == 1


# ── 门限选定纯函数：分差不足不硬选；无分数不选 ──
def test_select_from_candidates_requires_threshold_and_margin():
    decided = select_from_candidates(_CANDIDATES)
    assert decided["decided"] is True and decided["selected_code"] == "010401004"

    no_margin = select_from_candidates([
        {"code": "010401004", "score": 0.90},
        {"code": "010401003", "score": 0.88},
    ])
    assert no_margin["decided"] is False

    no_score = select_from_candidates([{"code": "010401004"}])
    assert no_score["decided"] is False
    assert select_from_candidates([])["decided"] is False


# ── 特征 diff 纯函数：已填清单优先，文本包含兜底 ──
def test_diff_features_provided_takes_precedence_over_text():
    required = ["墙体类型", "砂浆强度等级"]
    assert diff_features(required, feature="内墙 M5 砂浆强度等级", provided_features=["墙体类型"]) == ["砂浆强度等级"]
    assert diff_features(required, feature="墙体类型：内墙，砂浆强度等级 M5") == []
    assert diff_features([], feature=None) == []


# ── select_bill_node：选定后带出特征缺口（payload 有描述才查，无描述不发真值请求）──
def test_select_bill_node_reports_feature_gap_when_description_present(monkeypatch):
    monkeypatch.setattr(nodes, "call_mcp_tool", _fake_mcp())
    r = nodes.select_bill_node({"candidates": _CANDIDATES, "description": "多孔砖内墙", "spec": "2013"})
    assert r["status"] == "done" and r["selected_code"] == "010401004"
    assert set(r["missing_features"]) == set(_BILL_TRUTH["features"])


def test_select_bill_node_skips_feature_gap_without_description(monkeypatch):
    def _no_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("payload 无描述时不应发起真值请求")
    monkeypatch.setattr(nodes, "call_mcp_tool", _no_call)
    r = nodes.select_bill_node({"candidates": _CANDIDATES})
    assert r["status"] == "done" and r["selected_code"] == "010401004"
    assert "missing_features" not in r
