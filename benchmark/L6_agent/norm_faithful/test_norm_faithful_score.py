"""norm_faithful 打分器单测（backend venv 下跑：复用 app.ce.norm.faithfulness）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from norm_faithful_score import (  # noqa: E402
    NormObs,
    aggregate,
    answer_points_coverage,
    context_recall_std,
    detect_refusal,
    score_case,
)

_EVID = [{"std": "GB50854", "version": "2024", "clause": "5.3.4", "node_path": "5.3.4"}]
_CASE = {
    "id": "QA-x", "question": "矩形柱工程量怎么算",
    "gold_contexts": [{"std": "GB50854", "version": "2024", "clause": "附录E"}],
    "gold_answer_points": ["按设计图示尺寸以体积计算", "柱高按基础顶面至上一层楼板"],
    "expect_refuse": False,
}
_REFUSE_CASE = {"id": "QA-r", "question": "北京定额", "gold_contexts": [], "expect_refuse": True}


# ── 拒答检测 ──
def test_detect_refusal():
    assert detect_refusal("该问题库内无收录，建议查阅…") is True
    assert detect_refusal("依据 5.3.4，按体积计算") is False


# ── 答案要点覆盖（容改写） ──
def test_answer_points_full_cover():
    obs = NormObs(answer="矩形柱按设计图示尺寸以体积计算；柱高按基础顶面至上一层楼板上表面。")
    assert answer_points_coverage(_CASE, obs) == 1.0


def test_answer_points_partial():
    obs = NormObs(answer="按设计图示尺寸以体积计算。")  # 只命中第一点
    assert answer_points_coverage(_CASE, obs) == 0.5


# ── std 级上下文召回 ──
def test_context_recall_std_hit():
    assert context_recall_std(_CASE, NormObs(evidence=_EVID)) == 1.0


def test_context_recall_none_when_evidence_no_std():
    assert context_recall_std(_CASE, NormObs(evidence=[{"clause": "5.3.4"}])) is None


# ── 单用例打分：忠实 / 幻觉 ──
def test_score_faithful_answer():
    obs = NormObs(answer="依据 5.3.4，按体积计算", evidence=_EVID, did_refuse=False)
    s = score_case(_CASE, obs)
    assert s.faithful_rate == 1.0 and s.unfaithful is False and s.refusal_ok is True


def test_score_hallucinated_citation():
    obs = NormObs(answer="依据 9.9.9，按体积计算", evidence=_EVID, did_refuse=False)  # 9.9.9 没检索到
    s = score_case(_CASE, obs)
    assert s.unfaithful is True and s.faithful_rate == 0.0


def test_score_refuse_case_faithfulness_na():
    obs = NormObs(answer="库内无收录，拒答", evidence=[], did_refuse=True)
    s = score_case(_REFUSE_CASE, obs)
    assert s.refusal_ok is True and s.faithful_rate is None  # 拒答用例不算忠实率


def test_missed_refuse_flagged():
    obs = NormObs(answer="随便答了点啥", evidence=[], did_refuse=False)  # 该拒却答
    assert score_case(_REFUSE_CASE, obs).refusal_ok is False


# ── 整套聚合 ──
def test_aggregate_splits_answer_and_refuse():
    faithful = score_case(_CASE, NormObs(answer="依据 5.3.4 按体积计算", evidence=_EVID, did_refuse=False))
    hallucin = score_case(_CASE, NormObs(answer="依据 9.9.9", evidence=_EVID, did_refuse=False))
    missed = score_case(_REFUSE_CASE, NormObs(answer="乱答", did_refuse=False))
    rep = aggregate([faithful, hallucin, missed])
    assert rep["n"] == 3
    assert rep["unfaithful_case_rate"] == 0.5          # 2 应答里 1 个幻觉
    assert rep["missed_refuse_rate"] == 1.0            # 1 拒答用例漏拒
    assert rep["faithful_rate"] == 0.5                 # (1.0 + 0.0)/2
