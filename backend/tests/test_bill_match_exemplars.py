"""选码主动学习闭环（few-shot）单测——已并入 bill_match_engine（2026-07-12）：store 落盘 + 相似检索 + spec 隔离 + 采集 + 注入。"""
from __future__ import annotations

from app.ce.cost.bill_match_engine import (
    Correction,
    CorrectionStore,
    recall_exemplars,
    format_fewshot,
    record_bill_correction,
    retrieve_exemplars,
    similarity,
)

_C1 = Correction(feature="C30现浇矩形柱截面500×500", correct_code="010502001", model_code="010502002", spec="2024")
_C2 = Correction(feature="MU10烧结多孔砖240厚外墙M7.5砂浆", correct_code="010401004", model_code="010401003", spec="2024")
_C3 = Correction(feature="C30现浇混凝土矩形柱600×600", correct_code="010502001", spec="2013")  # 同类构件，2013 口径


# ── 相似度 ──
def test_similarity_more_similar_ranks_higher():
    q = "C30现浇矩形柱600×600泵送商砼"
    assert similarity(q, _C1.feature) > similarity(q, _C2.feature)


def test_was_correction_flag():
    assert _C1.was_correction is True                    # 人工码≠模型码
    assert Correction(feature="x", correct_code="A", model_code="A").was_correction is False


# ── store 落盘往返 ──
def test_store_roundtrip(tmp_path):
    store = CorrectionStore(tmp_path / "corr.jsonl")
    store.add(_C1)
    store.add(_C2)
    loaded = store.load()
    assert [c.correct_code for c in loaded] == ["010502001", "010401004"]


def test_store_missing_file_empty(tmp_path):
    assert CorrectionStore(tmp_path / "nope.jsonl").load() == []


def test_store_skips_bad_lines(tmp_path):
    p = tmp_path / "corr.jsonl"
    p.write_text('{"feature":"a","correct_code":"010101001"}\n不是json\n', encoding="utf-8")
    assert len(CorrectionStore(p).load()) == 1


# ── 检索 top-k + spec 隔离 ──
def test_retrieve_ranks_by_similarity():
    q = "C30现浇矩形柱截面500×500泵送"
    ex = retrieve_exemplars(q, spec="2024", k=2, corrections=[_C1, _C2])
    assert ex[0].correct_code == "010502001"             # 柱 > 墙


def test_retrieve_spec_isolation():
    q = "C30现浇矩形柱"
    # spec=2024 时不应召回 2013 的 _C3
    ex = retrieve_exemplars(q, spec="2024", k=5, corrections=[_C1, _C3])
    assert all(c.spec in (None, "2024") for c in ex)
    assert _C3 not in ex


def test_retrieve_floor_filters_dissimilar():
    ex = retrieve_exemplars("完全无关的钢结构网架", spec="2024", k=5, corrections=[_C1, _C2])
    assert ex == []                                       # 都不像 → 不注入噪声


def test_retrieve_verified_preferred():
    unv = Correction(feature="C30现浇矩形柱500", correct_code="010502009", spec="2024", verified=False)
    ex = retrieve_exemplars("C30现浇矩形柱500×500", spec="2024", k=1, corrections=[unv, _C1])
    assert ex[0].verified is True                         # verified 优先


# ── 采集 ──
def test_record_infers_model_code_from_candidates(tmp_path):
    store = CorrectionStore(tmp_path / "c.jsonl")
    c = record_bill_correction("C30矩形柱", "010502001",
                               candidates=[{"code": "010502002"}, {"code": "010502001"}],
                               spec="2024", store=store)
    assert c.model_code == "010502002" and c.was_correction is True
    assert store.load()[0].correct_code == "010502001"


def test_record_skips_when_missing(tmp_path):
    store = CorrectionStore(tmp_path / "c.jsonl")
    assert record_bill_correction(None, "010502001", store=store) is None
    assert record_bill_correction("柱", None, store=store) is None
    assert store.load() == []


# ── 注入格式 ──
def test_format_fewshot_marks_correction():
    txt = format_fewshot([_C1, _C2])
    assert "010502001" in txt and "曾误选 010502002" in txt
    assert format_fewshot([]) == ""


# ── 注入入口（读默认 store；库空返回空示例，不炸；原独立工具注册已注销） ──
def test_recall_exemplars_empty_store(monkeypatch, tmp_path):
    monkeypatch.setenv("CE_CORRECTIONS_PATH", str(tmp_path / "empty.jsonl"))
    out = recall_exemplars("C30矩形柱", spec="2024")
    assert out["count"] == 0 and out["fewshot"] == ""
