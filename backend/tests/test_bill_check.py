"""bill_match 双模匹配工具单测（monkeypatch 掉 MCP，纯逻辑验证）。

核实模式（code 给定）= 原 verify_bill_code 契约不变；选码模式（code 缺省）与 spec 口径闸
为 2026-07-12 合并/整改新增。
"""
from __future__ import annotations

from typing import Any

import app.ce.cost.bill_check as bill_check
from app.ce.cost.bill_check import bill_match, bill_match_tool

# 真值：010502001 矩形柱，规范要求 3 个特征项。
_BILL_TRUTH = {
    "code": "010502001",
    "name": "矩形柱",
    "features": ["混凝土种类", "混凝土强度等级", "柱截面尺寸"],
}
_CANDIDATES = [
    {"code": "010502001", "name": "矩形柱", "score": 0.92},
    {"code": "010502002", "name": "构造柱", "score": 0.61},
]


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


_FEATURE_FULL = "现浇 C30 商品混凝土 矩形柱，柱截面尺寸 400x400，混凝土种类：商品混凝土，混凝土强度等级 C30"


# ═══ 核实模式（code 给定，契约与原 verify_bill_code 一致） ═══
# ── pass：编码存在、召回命中、特征齐 ──
def test_verify_pass_when_code_valid_features_complete(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    r = bill_match(feature=_FEATURE_FULL, code="010502001001", provided_features=["混凝土种类", "混凝土强度等级", "柱截面尺寸"])
    assert r["mode"] == "verify"
    assert r["status"] == "done" and r["verdict"] == "pass"
    assert r["code9"] == "010502001"
    assert r["missing_features"] == []
    assert r["recall"]["hit"] is True and r["recall"]["rank"] == 1


# ── fail：格式坏 / 编码不存在 ──
def test_verify_fail_on_bad_code_format(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    r = bill_match(feature="矩形柱", code="ABC123")
    assert r["verdict"] == "fail"
    assert any(f["type"] == "code_format" for f in r["findings"])


def test_verify_fail_on_code_not_found(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(bill_result={}))
    r = bill_match(feature=_FEATURE_FULL, code="019999999")
    assert r["verdict"] == "fail"
    assert any(f["type"] == "code_not_found" for f in r["findings"])


# ── doubt：缺特征 / 召回未命中 ──
def test_verify_doubt_on_missing_features(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    r = bill_match(feature="矩形柱 C30", code="010502001", provided_features=["混凝土强度等级"])
    assert r["verdict"] == "doubt"
    assert "混凝土种类" in r["missing_features"] and "柱截面尺寸" in r["missing_features"]


def test_verify_doubt_on_recall_miss(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(match_result=[{"code": "010502002", "name": "构造柱"}]))
    r = bill_match(feature=_FEATURE_FULL, code="010502001", provided_features=["混凝土种类", "混凝土强度等级", "柱截面尺寸"])
    assert r["verdict"] == "doubt"
    assert r["recall"]["hit"] is False
    assert any(f["type"] == "recall_miss" for f in r["findings"])


# ── 特征匹配的文本兜底：未给 provided_features 时按描述文本包含判 ──
def test_verify_feature_diff_falls_back_to_description_text(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    r = bill_match(feature=_FEATURE_FULL, code="010502001")
    assert r["missing_features"] == []  # 三个特征项名都出现在描述文本里


# ── blocked：真值服务不可用 ≠ 编码错，verdict 不定罪 ──
def test_verify_blocked_when_truth_service_unavailable(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(bill_status="tool_unavailable"))
    r = bill_match(feature="矩形柱", code="010502001")
    assert r["status"] == "blocked" and r["verdict"] is None


# ── 召回服务不可用只降级 info，不影响 verdict ──
def test_verify_recall_unavailable_degrades_to_info(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(match_status="error"))
    r = bill_match(feature=_FEATURE_FULL, code="010502001", provided_features=["混凝土种类", "混凝土强度等级", "柱截面尺寸"])
    assert r["verdict"] == "pass"
    assert r["recall"]["checked"] is False
    assert any(f["type"] == "recall_unavailable" for f in r["findings"])


# ── 真值特征项为 dict 形态 / 12 位候选码兼容 ──
def test_verify_feature_schema_and_candidate_code_shapes(monkeypatch):
    truth = {"code": "010502001", "name": "矩形柱", "features": [{"name": "混凝土种类"}, {"name": "混凝土强度等级"}]}
    cands = [{"bill_code": "010502001001", "name": "矩形柱"}]
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(bill_result=truth, match_result=cands))
    r = bill_match(feature="商品混凝土 混凝土种类 混凝土强度等级 C30 矩形柱", code="010502001")
    assert r["required_features"] == ["混凝土种类", "混凝土强度等级"]
    assert r["recall"]["hit"] is True  # 12 位候选截前 9 命中


# ═══ 选码模式（code 缺省，2026-07-12 双模合并新增面） ═══
# ── 高置信自动选定 + 顺带缺特征提醒 + exemplar_hints 字段在位 ──
def test_select_auto_when_confident(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    r = bill_match(feature=_FEATURE_FULL)
    assert r["mode"] == "select" and r["status"] == "done"
    assert r["selected_code"] == "010502001"
    assert r["missing_features"] == []  # 特征名都在描述文本里
    assert "exemplar_hints" in r  # few-shot 注入端已内置（库空则为空列表）


# ── 低置信（分差不够）→ need_review 不硬选 ──
def test_select_need_review_on_low_margin(monkeypatch):
    close = [
        {"code": "010502001", "name": "矩形柱", "score": 0.88},
        {"code": "010502002", "name": "构造柱", "score": 0.87},
    ]
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(match_result=close))
    r = bill_match(feature="柱 C30")
    assert r["status"] == "need_review" and r.get("selected_code") is None
    assert any(f["type"] == "low_confidence" for f in r["findings"])


# ── 零召回 → need_review ──
def test_select_need_review_on_no_candidates(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp(match_result=[]))
    r = bill_match(feature="某种自定义新型墙体")
    assert r["status"] == "need_review"
    assert any(f["type"] == "no_candidates" for f in r["findings"])


# ═══ spec 口径闸（两模式共用，默认仅 2013） ═══
def test_unsupported_spec_rejected_both_modes(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    for kwargs in ({"feature": _FEATURE_FULL}, {"feature": _FEATURE_FULL, "code": "010502001"}):
        r = bill_match(spec="2024", **kwargs)
        assert r["status"] == "unsupported_spec"
        assert "2013" in r["message"]


def test_spec_2013_passes_gate(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    r = bill_match(feature=_FEATURE_FULL, spec="2013")
    assert r["status"] == "done"


# ── 工具封装可用（双模同一工具名 bill_match） ──
def test_tool_wrapper_invocable(monkeypatch):
    monkeypatch.setattr(bill_check, "call_mcp_tool", _fake_mcp())
    out = bill_match_tool.invoke({"feature": _FEATURE_FULL, "code": "010502001", "provided_features": ["混凝土种类", "混凝土强度等级", "柱截面尺寸"]})
    assert out["verdict"] == "pass"
    out2 = bill_match_tool.invoke({"feature": _FEATURE_FULL})
    assert out2["mode"] == "select" and out2["selected_code"] == "010502001"
