#!/usr/bin/env python3
"""评测工具纯函数回归（M4）：eval_select 的 classify/aggregate/confidence_buckets。

这些纯函数是 τ 调参与红线判定的度量仪——度量仪本身错了，调参就是盲调。双模式同约定。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.eval_select import aggregate, classify_case, confidence_buckets  # noqa: E402


def _rec(confidence: float, correct: bool) -> dict:
    return {"recalled": True, "picked": "x", "correct": correct, "need_review": False,
            "confidence": confidence, "auto_accept": True, "dangerous": not correct,
            "abstained": False}


def test_classify_case_flags():
    out = classify_case({"010101001"}, ["010101001", "010101002"],
                        {"code": "010101001", "confidence": 0.9, "need_review": False})
    assert out["recalled"] and out["correct"] and out["auto_accept"] and not out["dangerous"]
    bad = classify_case({"010101001"}, ["010101001", "010101002"],
                        {"code": "010101002", "confidence": 0.9, "need_review": False})
    assert bad["dangerous"]  # 自动定稿且选错＝高置信错码（最危险形态）


def test_aggregate_dangerous_count():
    m = aggregate([_rec(0.9, True), _rec(0.9, False), _rec(0.1, True)])
    assert m["n"] == 3 and m["n_dangerous"] == 1  # fixture 简化 dangerous=not correct → 仅第 2 条
    assert m["top1"] == 2 / 3 and m["n_auto_accept"] == 3


def test_confidence_buckets_partition_and_acc():
    records = [_rec(0.05, False), _rec(0.30, True), _rec(0.30, False),
               _rec(0.70, True), _rec(0.80, True), _rec(0.99, True), _rec(0.75, False)]
    buckets = confidence_buckets(records)
    assert [b["n"] for b in buckets] == [1, 2, 0, 1, 3]      # 边界：0.75 落最后桶（自动过区）
    assert sum(b["n"] for b in buckets) == len(records)       # 全量覆盖不漏不重
    last = buckets[-1]
    assert last["lo"] == 0.75 and last["n_correct"] == 2
    assert abs(last["acc"] - 2 / 3) < 1e-9                    # 自动过区准确率＝G2 门直读
    assert buckets[2]["acc"] is None                          # 空桶 → None 不除零


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    failed = 0
    for _name in sorted(k for k in dir() if k.startswith("test_")):
        try:
            globals()[_name]()
            print(f"✓ {_name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"✗ {_name}  {type(exc).__name__}: {exc}")
    print(f"\n评测度量仪回归：{'全绿' if not failed else f'{failed} 败'}")
    sys.exit(1 if failed else 0)
