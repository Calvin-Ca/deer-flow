#!/usr/bin/env python
"""阶段 5 自动评分入口，不调用外部模型。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.scoring import (
    MODEL_IDS,
    aggregate,
    load_clause_ids,
    load_eval_rows,
    load_group_coverage,
    load_model_rows,
    sha256,
    score_row,
    write_summary,
)

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_EVAL = _ROOT / "data/eval/evalset_v1.jsonl"
_DEFAULT_CLAUSES = _ROOT / "data/interim/clauses.jsonl"
_DEFAULT_RESULTS = _ROOT / "results"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="阶段 5 自动评分")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--eval-file", type=Path, default=_DEFAULT_EVAL)
    parser.add_argument("--clauses-file", type=Path, default=_DEFAULT_CLAUSES)
    parser.add_argument("--results-root", type=Path, default=_DEFAULT_RESULTS)
    parser.add_argument("--models", default=",".join(MODEL_IDS))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    models = tuple(item.strip() for item in args.models.split(",") if item.strip())
    unknown = set(models) - set(MODEL_IDS)
    if unknown or not models:
        raise ValueError(f"模型列表非法：{sorted(unknown)}")

    eval_file = args.eval_file.resolve()
    results_dir = (args.results_root / args.run_id).resolve()
    raw_dir = results_dir / "raw"
    output_dir = results_dir / "metrics"
    eval_rows = load_eval_rows(eval_file)
    valid_clause_ids = load_clause_ids(args.clauses_file.resolve())
    summary_rows: list[dict] = []
    expected_ids: set[str] | None = None

    for model_id in models:
        raw_path = raw_dir / f"{model_id}.jsonl"
        model_rows = load_model_rows(raw_path, eval_rows, model_id)
        model_ids = set(model_rows)
        if expected_ids is None:
            expected_ids = model_ids
        elif model_ids != expected_ids:
            missing = sorted(expected_ids - model_ids)
            extra = sorted(model_ids - expected_ids)
            raise ValueError(
                f"{model_id} 结果 id 集合与其他模型不一致；缺少={missing[:5]}，多出={extra[:5]}"
            )
        coverage = None
        if model_id in {"group_a", "group_b", "group_c", "group_d"}:
            coverage = load_group_coverage(
                _ROOT / "data/processed" / model_id / "train.jsonl"
            )
        scored = [
            score_row(eval_rows[sample_id], result, valid_clause_ids, coverage)
            for sample_id, result in sorted(model_rows.items())
        ]
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{model_id}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in scored),
            encoding="utf-8",
        )
        summary_rows.append({"model_id": model_id, **aggregate(scored, "all")})
        for sample_type in sorted({row["type"] for row in scored}):
            subset = [row for row in scored if row["type"] == sample_type]
            summary_rows.append(
                {"model_id": model_id, **aggregate(subset, sample_type)}
            )
        print(
            f"{model_id}: {len(scored)} 条，"
            f"截断={sum(row['truncated'] for row in scored)}，"
            f"空答案={sum(row['empty'] for row in scored)}"
        )

    summary_path = results_dir / "summary.csv"
    write_summary(summary_path, summary_rows)
    metadata = {
        "run_id": args.run_id,
        "eval_file": str(eval_file),
        "eval_sha256": sha256(eval_file),
        "clauses_file": str(args.clauses_file.resolve()),
        "clauses_sha256": sha256(args.clauses_file.resolve()),
        "models": list(models),
        "result_count_per_model": len(expected_ids or ()),
        "scoring_version": "stage5-auto-v1",
    }
    (results_dir / "scoring_manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"✅ 明细：{output_dir}")
    print(f"✅ 汇总：{summary_path}")
    print(f"✅ 清单：{results_dir / 'scoring_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
