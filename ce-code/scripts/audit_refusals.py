#!/usr/bin/env python
"""分层抽查非拒答题中的疑似误拒答。"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="抽查模型疑似误拒答")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="group_d")
    parser.add_argument("--per-type", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--results-root", type=Path, default=root / "results")
    return parser


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def main() -> int:
    args = build_parser().parse_args()
    run_dir = (args.results_root / args.run_id).resolve()
    metrics_path = run_dir / "metrics" / f"{args.model}.jsonl"
    raw_path = run_dir / "raw" / f"{args.model}.jsonl"
    metrics = read_jsonl(metrics_path)
    raw = {row["id"]: row for row in read_jsonl(raw_path)}

    candidates = [
        row for row in metrics
        if not row["refusal_gold"] and row["refusal_pred"]
    ]
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        groups[row["type"]].append(row)

    rng = random.Random(args.seed)
    print(f"模型: {args.model}")
    print(f"疑似误拒答总数: {len(candidates)}")
    print(f"随机种子: {args.seed}，每类抽查: {args.per_type} 条")

    for sample_type in sorted(groups):
        rows = groups[sample_type]
        selected = rng.sample(rows, min(args.per_type, len(rows)))
        print(f"\n{'=' * 24} {sample_type} ({len(rows)} 条) {'=' * 24}")
        for index, metric in enumerate(selected, 1):
            result = raw[metric["id"]]
            print(f"\n[{index}] ID: {metric['id']}")
            print(f"问题: {result.get('question', '')}")
            print(f"回答: {result.get('answer', '')}")
            print(
                "评分: refusal_gold="
                f"{metric['refusal_gold']} refusal_pred={metric['refusal_pred']} "
                f"truncated={metric['truncated']} "
                f"clause_f1={metric['clause_f1']} "
                f"numeric_exact={metric['numeric_exact']}"
            )
            print("人工判定: [正确回答 / 部分回答 / 真实误拒答]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
