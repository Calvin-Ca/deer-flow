"""规范问答引用忠实性校验单测（纯函数，无服务依赖）。"""
from __future__ import annotations

from app.ce.norm.faithfulness import (
    check_faithfulness,
    evidence_clauses,
    extract_cited_clauses,
    faithfulness_enabled,
    verify_norm_tool,
)

_EVIDENCE = [
    {"standard": "GB50854-2024", "clause": "5.3.4", "node_path": "5.3.4", "text": "..."},
    {"standard": "GB50854-2024", "clause": "5.3.5", "node_path": "5.3.5", "text": "..."},
]


# ── 抽取条款号：不误吞年份/标准号 ──
def test_extract_clauses_only_dotted():
    cited = extract_cited_clauses("依据 GB 50854-2024 第 5.3.4 条，另见 4.1 与 2024 版")
    assert "5.3.4" in cited and "4.1" in cited
    assert "2024" not in cited and "50854" not in cited  # 年份/标准号无内部小数点，不算条款号


def test_extract_dedup_preserves_order():
    assert extract_cited_clauses("5.3.4 ... 5.3.4 ... 5.3.5") == ["5.3.4", "5.3.5"]


# ── 证据条款集：取结构化字段 ──
def test_evidence_clauses_from_structured_fields():
    assert evidence_clauses(_EVIDENCE) == {"5.3.4", "5.3.5"}


# ── 忠实 / 幻觉 / 无引用 ──
def test_faithful_when_all_cited_in_evidence():
    out = check_faithfulness("依据 5.3.4 和 5.3.5 ...", _EVIDENCE)
    assert out["verdict"] == "faithful" and out["unfaithful"] == [] and out["faithful_rate"] == 1.0


def test_unfaithful_flags_hallucinated_citation():
    out = check_faithfulness("依据 5.3.4 和 9.9.9 ...", _EVIDENCE)  # 9.9.9 没检索到 = 幻觉
    assert out["verdict"] == "unfaithful"
    assert out["unfaithful"] == ["9.9.9"] and out["faithful"] == ["5.3.4"]
    assert out["faithful_rate"] == 0.5


def test_no_citation_verdict():
    out = check_faithfulness("这个问题库里没收录，无法给出条文依据。", _EVIDENCE)
    assert out["verdict"] == "no_citation" and out["faithful_rate"] is None


def test_empty_evidence_all_unfaithful():
    out = check_faithfulness("依据 5.3.4 ...", [])
    assert out["verdict"] == "unfaithful" and out["unfaithful"] == ["5.3.4"]


# ── 开关 ──
def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("CE_NORM_FAITHFULNESS_CHECK", raising=False)
    assert faithfulness_enabled() is True


def test_flag_off(monkeypatch):
    for v in ("0", "false", "off", "no"):
        monkeypatch.setenv("CE_NORM_FAITHFULNESS_CHECK", v)
        assert faithfulness_enabled() is False


# ── 工具封装 ──
def test_tool_wrapper():
    out = verify_norm_tool.invoke({"answer": "依据 5.3.4", "evidence": _EVIDENCE})
    assert out["verdict"] == "faithful"
