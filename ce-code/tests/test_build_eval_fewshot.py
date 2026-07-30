"""3-shot 自动冻结脚本的纯本地回归测试。

运行：
    .venv/bin/python tests/test_build_eval_fewshot.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_eval_fewshot import build_fewshot  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sample(
    sample_id: str,
    sample_type: str,
    clauses: list[str],
    *,
    filters: list[str],
) -> dict:
    return {
        "sample_id": sample_id,
        "conversations": [
            {"from": "human", "value": f"问题 {sample_id}"},
            {"from": "gpt", "value": f"答案 {sample_id}"},
        ],
        "meta": {
            "sample_type": sample_type,
            "source_clauses": clauses,
            "filters_passed": filters,
        },
    }


def test_build_fewshot_is_deterministic_and_excludes_eval_clauses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        eval_file = root / "data/eval/evalset_v1.jsonl"
        eval_manifest = root / "data/eval/manifest.json"
        group_c = root / "data/processed/group_c/train.jsonl"
        group_d = root / "data/processed/group_d/train.jsonl"

        _write_jsonl(
            eval_file,
            [
                {
                    "id": "eval_1",
                    "question": "评测问题一",
                    "gold_clauses": ["GB50010-2010_1.1.1"],
                },
                {
                    "id": "eval_2",
                    "question": "评测问题二",
                    "gold_clauses": ["GB50011-2010_2.2.2"],
                },
            ],
        )
        eval_manifest.write_text(
            json.dumps(
                {
                    "total": 2,
                    "leakage_check": {"checked": True, "leaked": 0},
                    "leakage_checked_against": {"c": "fp-v1", "d": "fp-v1"},
                }
            ),
            encoding="utf-8",
        )
        for path in (group_c.parent / "manifest.json", group_d.parent / "manifest.json"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"clauses_fingerprint": "fp-v1"}),
                encoding="utf-8",
            )

        full_filters = ["answerable", "clause_accurate", "diverse"]
        _write_jsonl(
            group_c,
            [
                _sample(
                    "c_overlap",
                    "single_clause",
                    ["GB50010-2010_1.1.1"],
                    filters=full_filters,
                ),
                _sample(
                    "c_good_1",
                    "single_clause",
                    ["GB50010-2010_9.1.1"],
                    filters=full_filters,
                ),
                _sample(
                    "c_good_2",
                    "single_clause",
                    ["GB50010-2010_9.1.2"],
                    filters=full_filters,
                ),
            ],
        )
        _write_jsonl(
            group_d,
            [
                _sample(
                    "d_cross_1",
                    "cross_clause",
                    ["GB50010-2010_9.2.1", "GB50011-2010_9.2.2"],
                    filters=["cross_clause_verified"],
                ),
                _sample(
                    "d_cross_2",
                    "cross_clause",
                    ["GB50010-2010_9.2.3", "GB50011-2010_9.2.4"],
                    filters=["cross_clause_verified"],
                ),
                _sample(
                    "d_refusal_1",
                    "refusal",
                    ["GB50010-2010_9.3.1"],
                    filters=[],
                ),
                _sample(
                    "d_refusal_2",
                    "refusal",
                    ["GB50010-2010_9.3.2"],
                    filters=[],
                ),
            ],
        )

        kwargs = {
            "eval_file": eval_file,
            "eval_manifest": eval_manifest,
            "group_c": group_c,
            "group_d": group_d,
            "seed": 42,
        }
        first = build_fewshot(**kwargs)
        second = build_fewshot(**kwargs)

        assert first == second
        assert [row["sample_type"] for row in first] == [
            "single_clause",
            "cross_clause",
            "refusal",
        ]
        eval_clauses = {"GB50010-2010_1.1.1", "GB50011-2010_2.2.2"}
        assert all(not (set(row["source_clauses"]) & eval_clauses) for row in first)
        assert all(row["selection_seed"] == 42 for row in first)
        assert [row["candidate_count"] for row in first] == [2, 2, 2]


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
