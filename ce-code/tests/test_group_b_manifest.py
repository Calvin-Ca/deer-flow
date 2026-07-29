"""Group B 累计 manifest 统计的纯本地回归测试。

可直接运行，不需要 pytest：
    .venv/bin/python tests/test_group_b_manifest.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.dataset_stats import scan_training_jsonl  # noqa: E402


def _sample(sample_id: str, clause_id: str) -> dict:
    return {
        "sample_id": sample_id,
        "meta": {"source_clauses": [clause_id]},
    }


def test_dataset_stats_counts_accumulated_file() -> None:
    """续跑追加后统计最终文件总量，而不是本轮新增量。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "train.jsonl"
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_sample("b_1", "GB50010-2010_1.0.1")) + "\n")
            f.write(json.dumps(_sample("b_2", "GB50010-2010_1.0.1")) + "\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_sample("b_3", "GB50010-2010_1.0.2")) + "\n")

        total, clauses = scan_training_jsonl(path)

        assert total == 3
        assert clauses == {"GB50010-2010_1.0.1", "GB50010-2010_1.0.2"}


def test_dataset_stats_rejects_corrupt_jsonl() -> None:
    """数据损坏时不允许继续生成看似正常的 manifest。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "train.jsonl"
        path.write_text('{"sample_id": "ok"}\nnot-json\n', encoding="utf-8")
        try:
            scan_training_jsonl(path)
        except ValueError as exc:
            assert "第 2 行" in str(exc)
        else:
            raise AssertionError("损坏的 JSONL 应触发 ValueError")


if __name__ == "__main__":
    tests = [
        test_dataset_stats_counts_accumulated_file,
        test_dataset_stats_rejects_corrupt_jsonl,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"全部 {len(tests)} 例通过")
