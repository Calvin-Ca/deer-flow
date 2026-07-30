"""阶段 5 自动评分的纯本地回归测试。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.scoring import (  # noqa: E402
    aggregate,
    load_model_rows,
    numeric_score,
    score_row,
)


def test_numeric_score_normalizes_decimal_trailing_zero() -> None:
    result = numeric_score("结果为 1.60×10^4 N/mm²。", ["1.60"])
    assert result["gold_count"] == 1
    assert result["matched_count"] == 1
    assert result["exact"] is True


def test_score_row_clause_hallucination_refusal_and_health() -> None:
    eval_row = {
        "id": "q1",
        "type": "single_clause",
        "question": "请说明GB50010-2010第9.5.4条。",
        "gold_clauses": ["GB50010-2010_9.5.4"],
        "gold_values": [],
        "should_refuse": False,
    }
    result_row = {
        "id": "q1",
        "model_id": "group_a",
        "answer": "依据第9.5.4条，结论见第99.1.1条。",
        "finish_reason": "stop",
        "completion_tokens": 20,
    }
    scored = score_row(
        eval_row,
        result_row,
        {"GB50010-2010_9.5.4"},
        {"GB50010-2010_9.5.4"},
    )
    assert scored["clause_f1"] == 0.6667
    assert scored["invalid_clauses"] == ["GB50010-2010_99.1.1"]
    assert scored["truncated"] is False
    assert scored["covered"] is True

    refusal_eval = {**eval_row, "should_refuse": True}
    refusal_result = {**result_row, "answer": "该条款未明确具体处理方法，建议由专业工程师判断。"}
    refusal_scored = score_row(
        refusal_eval,
        refusal_result,
        {"GB50010-2010_9.5.4"},
        None,
    )
    assert refusal_scored["refusal_pred"] is True


def test_load_model_rows_rejects_duplicate_ids() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "group_a.jsonl"
        row = {"id": "q1", "model_id": "group_a", "answer": "a"}
        path.write_text(
            json.dumps(row) + "\n" + json.dumps(row) + "\n",
            encoding="utf-8",
        )
        try:
            load_model_rows(path, {"q1": {"id": "q1"}}, "group_a")
        except ValueError as exc:
            assert "重复" in str(exc)
        else:
            raise AssertionError("重复结果 id 必须被拒绝")


def test_aggregate_counts_truncation_and_numeric_rows() -> None:
    rows = [
        {
            "clause_f1": 1.0,
            "clause_precision": 1.0,
            "clause_recall": 1.0,
            "clause_true_positive": 1,
            "clause_predicted_count": 1,
            "clause_gold_count": 1,
            "invalid_clauses": [],
            "numeric_gold_count": 1,
            "numeric_matched_count": 1,
            "numeric_exact": True,
            "refusal_gold": False,
            "refusal_pred": False,
            "empty": False,
            "truncated": True,
            "repetitive": False,
            "completion_tokens": 1024,
            "covered": True,
        }
    ]
    summary = aggregate(rows, "all")
    assert summary["n"] == 1
    assert summary["numeric_exact_accuracy"] == 1.0
    assert summary["truncated_rate"] == 1.0
    assert summary["covered_n"] == 1


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"全部 {len(tests)} 例通过")
