"""cost_task 打分器单测（纯函数，本地可跑：uv run --project backend python -m pytest 本文件）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cost_task_score import (  # noqa: E402
    RunObservation,
    RunScore,
    aggregate_passk,
    aggregate_suite,
    check_policy,
    check_terminal,
    score_run,
)

# 典型 case（照真实 schema）：clean 档，选码 010502001 + 溯源 GB50854/2024。
CLEAN = {
    "id": "T-CLEAN", "difficulty": "clean", "pass_k": 3,
    "terminal_check": {"expected_bill_code": "010502001", "must_cite": [{"std": "GB50854", "version": "2024"}]},
    "policy": {"no_rag_calc": True, "no_region_leak": "shenzhen", "clarify_if_missing_feature": True},
}
MISSING = {
    "id": "T-MISS", "difficulty": "missing_feature", "pass_k": 2,
    "terminal_check": {"expected_bill_code": "010502001", "must_ask": True, "must_cite": [{"std": "GB50854", "version": "2024"}]},
    "policy": {"no_region_leak": "shenzhen", "clarify_if_missing_feature": True},
}
CROSSPROV = {
    "id": "T-XPROV", "difficulty": "cross_province", "pass_k": 2,
    "terminal_check": {"expected_bill_code": "010502001", "must_refuse": True, "refuse_reason": "他省",
                       "must_cite": [{"std": "GB50854", "version": "2024"}]},
    "policy": {"no_region_leak": "shenzhen", "clarify_if_missing_feature": True},
}


# ── 码抽取/归一 + expected_bill_code ──
def test_expected_code_final_match():
    obs = RunObservation(final_code="010502001", answer="GB50854-2024 综合...")
    assert check_terminal(CLEAN, obs)["expected_bill_code"] is True


def test_expected_code_12digit_takes_first9():
    obs = RunObservation(final_code="010502001001")  # 12 位含顺序号
    assert check_terminal(CLEAN, obs)["expected_bill_code"] is True


def test_expected_code_wrong():
    obs = RunObservation(final_code="010401003")
    assert check_terminal(CLEAN, obs)["expected_bill_code"] is False


def test_expected_code_fallback_to_codes_when_no_final():
    obs = RunObservation(codes=["010101001", "010502001"], final_code=None)
    assert check_terminal(CLEAN, obs)["expected_bill_code"] is True


def test_expected_code_no_code_at_all_is_false():
    obs = RunObservation(final_code=None, codes=[])
    assert check_terminal(CLEAN, obs)["expected_bill_code"] is False


# ── must_cite：标准号归一（空格/横杠） ──
def test_must_cite_space_and_dash_normalized():
    obs = RunObservation(final_code="010502001", answer="依据 GB 50854-2024 第5章...")
    assert check_terminal(CLEAN, obs)["must_cite"] is True


def test_must_cite_missing_version_fails():
    obs = RunObservation(final_code="010502001", answer="依据 GB50854 ...")  # 缺版本
    assert check_terminal(CLEAN, obs)["must_cite"] is False


# ── must_ask / must_refuse / must_declare / must_flag ──
def test_must_ask_needs_clarify():
    assert check_terminal(MISSING, RunObservation(did_clarify=True))["must_ask"] is True
    assert check_terminal(MISSING, RunObservation(did_clarify=False))["must_ask"] is False


def test_must_refuse_requires_refusal_and_no_final_code():
    ok = RunObservation(answer="本系统仅覆盖深圳·2013，建议当地造价站", final_code=None)
    assert check_terminal(CROSSPROV, ok)["must_refuse"] is True
    # 嘴上说超范围却还是落了码 → 不算真拒答
    leaked = RunObservation(answer="超范围，但姑且给你 010502001", final_code="010502001")
    assert check_terminal(CROSSPROV, leaked)["must_refuse"] is False


def test_must_declare_caliber():
    case = {"terminal_check": {"expected_bill_code": "010502001", "must_declare_caliber": True}, "policy": {}}
    assert check_terminal(case, RunObservation(final_code="010502001", answer="口径：深圳·2013 ..."))["must_declare_caliber"] is True
    assert check_terminal(case, RunObservation(final_code="010502001", answer="给你组价如下"))["must_declare_caliber"] is False


# ── policy 红线 ──
def test_clarify_vacuous_true_when_not_missing():
    # clean 档无缺特征 → clarify_if_missing_feature 真空满足
    assert check_policy(CLEAN, RunObservation(did_clarify=False))["clarify_if_missing_feature"] is True


def test_clarify_required_when_missing_feature():
    assert check_policy(MISSING, RunObservation(did_clarify=False))["clarify_if_missing_feature"] is False
    assert check_policy(MISSING, RunObservation(did_clarify=True))["clarify_if_missing_feature"] is True


def test_no_region_leak_detects_other_province():
    assert check_policy(CLEAN, RunObservation(answer="参考北京定额..."))["no_region_leak"] is False
    assert check_policy(CLEAN, RunObservation(answer="深圳 2024 消耗量..."))["no_region_leak"] is True


def test_unobservable_policies_are_none_not_pass():
    pol = check_policy(CLEAN, RunObservation())
    assert pol["no_rag_calc"] is None  # 诚实：外部判不了 → 不假装通过


# ── RunScore 属性 ──
def test_task_pass_all_evaluable_true():
    obs = RunObservation(final_code="010502001", answer="GB 50854-2024")
    assert score_run(CLEAN, obs).task_pass is True


def test_task_pass_false_if_any_evaluable_fails():
    obs = RunObservation(final_code="010401003", answer="GB 50854-2024")  # 码错
    assert score_run(CLEAN, obs).task_pass is False


def test_redline_ok_ignores_none_counts_false():
    good = RunScore(terminal={"x": True}, policy={"a": None, "b": True})
    bad = RunScore(terminal={"x": True}, policy={"a": None, "b": False})
    assert good.redline_ok is True and bad.redline_ok is False


# ── pass^k 聚合 ──
def test_passk_all_pass():
    obs = RunObservation(final_code="010502001", answer="GB 50854-2024 深圳2024")
    runs = [score_run(CLEAN, obs) for _ in range(3)]
    res = aggregate_passk(CLEAN, runs)
    assert res.task_passk is True and res.redline_clean is True and res.overall_pass is True


def test_passk_one_run_fails_kills_it():
    good = score_run(CLEAN, RunObservation(final_code="010502001", answer="GB 50854-2024 深圳2024"))
    bad = score_run(CLEAN, RunObservation(final_code="010401003", answer="GB 50854-2024 深圳2024"))
    res = aggregate_passk(CLEAN, [good, good, bad])
    assert res.task_passk is False and res.overall_pass is False


def test_passk_insufficient_runs_not_pass():
    good = score_run(CLEAN, RunObservation(final_code="010502001", answer="GB 50854-2024 深圳2024"))
    res = aggregate_passk(CLEAN, [good])  # pass_k=3 但只跑 1 次
    assert res.task_passk is False


def test_redline_violation_kills_overall_but_task_can_pass():
    # 终态过，但某次红线违规（他省泄漏）→ task_passk True、overall False
    ok = RunObservation(final_code="010502001", answer="GB 50854-2024 深圳2024")
    leak = RunObservation(final_code="010502001", answer="GB 50854-2024 参考北京定额")
    runs = [score_run(CLEAN, ok), score_run(CLEAN, leak)]
    res = aggregate_passk({**CLEAN, "pass_k": 2}, runs)
    assert res.task_passk is True and res.redline_clean is False and res.overall_pass is False


# ── 整套聚合 ──
def test_suite_aggregate_rates_and_coverage():
    ok = RunObservation(final_code="010502001", answer="GB 50854-2024 深圳2024")
    passed = aggregate_passk({**CLEAN, "pass_k": 1}, [score_run(CLEAN, ok)])
    failed = aggregate_passk({**CLEAN, "pass_k": 1},
                             [score_run(CLEAN, RunObservation(final_code="010401003", answer="GB 50854-2024 深圳2024"))])
    rep = aggregate_suite([passed, failed])
    assert rep["n_cases"] == 2
    assert rep["task_success_passk"] == 0.5
    assert rep["overall_pass_rate"] == 0.5
    # CLEAN 有 3 条 policy，其中 no_rag_calc=None → 覆盖率 2/3
    assert abs(rep["policy_evaluable_coverage"] - (2 / 3)) < 1e-9
    assert rep["by_difficulty"]["clean"]["n"] == 2
