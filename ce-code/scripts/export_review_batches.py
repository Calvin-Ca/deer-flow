#!/usr/bin/env python
"""按题型和批次导出盲评材料。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.scoring import MODEL_IDS  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def safe(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="导出按题型分批的盲评材料")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--eval-file", type=Path, default=root / "data/eval/evalset_v1.jsonl")
    parser.add_argument("--results-root", type=Path, default=root / "results")
    parser.add_argument("--models", default=",".join(MODEL_IDS))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.batch_size <= 0:
        raise ValueError("--batch-size 必须大于 0")
    models = tuple(item.strip() for item in args.models.split(",") if item.strip())
    run_dir = (args.results_root / args.run_id).resolve()
    output_dir = (args.output_dir or run_dir / "review_batches").resolve()

    eval_rows = read_jsonl(args.eval_file.resolve())
    predictions = {
        model: {
            row["id"]: row
            for row in read_jsonl(run_dir / "raw" / f"{model}.jsonl")
        }
        for model in models
    }
    labels = {model: f"Answer_{index}" for index, model in enumerate(models, 1)}
    grouped: dict[str, list[dict]] = {}
    for row in eval_rows:
        grouped.setdefault(row.get("type", "unknown"), []).append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "model_map.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    total = 0
    for sample_type in sorted(grouped):
        rows = grouped[sample_type]
        for batch_no, start in enumerate(range(0, len(rows), args.batch_size), 1):
            batch = rows[start : start + args.batch_size]
            lines = [
                f"# LLM 盲评：{args.run_id} / {sample_type} / batch {batch_no:03d}",
                "",
                "> 请对每道题的 Answer_A 等答案独立评分，不要猜测真实模型身份。",
                "> 每项 1～5 分：correctness、completeness、grounding、relevance、overall。",
                "> 计算题额外评价 numerical_reasoning；拒答题额外评价 refusal_appropriateness。",
                "> 输出 JSON 时保留题目 id 和 Answer_X 名称。",
                "",
            ]
            for index, gold in enumerate(batch, start + 1):
                sample_id = gold["id"]
                lines.extend([
                    "---",
                    "",
                    f"## {index}. {sample_id}",
                    "",
                    f"题型：`{sample_type}`",
                    "",
                    "### 题目",
                    "",
                    safe(gold.get("question")),
                    "",
                    "### 金标答案",
                    "",
                    safe(gold.get("gold_answer")),
                    "",
                    f"金标条文：`{', '.join(gold.get('gold_clauses') or []) or '无'}`",
                    f"金标数值：`{', '.join(map(str, gold.get('gold_values') or [])) or '无'}`",
                    f"是否应拒答：`{gold.get('should_refuse', False)}`",
                    "",
                ])
                for model in models:
                    row = predictions[model].get(sample_id)
                    answer = "[缺少该题结果]" if row is None else safe(row.get("answer")) or "[空答案]"
                    lines.extend([f"### {labels[model]}", "", answer, ""])
            path = output_dir / f"{sample_type}_{batch_no:03d}.md"
            path.write_text("\n".join(lines), encoding="utf-8")
            total += len(batch)

    print(f"✅ 已导出 {len(grouped)} 个题型、{total} 道题")
    print(f"✅ 评审文件目录：{output_dir}")
    print(f"✅ 模型映射（评审后再使用）：{output_dir / 'model_map.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
