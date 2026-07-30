#!/usr/bin/env python
"""将金标和各模型回答合并导出为逐题 Markdown 评审文件。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.scoring import MODEL_IDS  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="导出逐题模型结果评审文件")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--eval-file", type=Path, default=root / "data/eval/evalset_v1.jsonl")
    parser.add_argument("--results-root", type=Path, default=root / "results")
    parser.add_argument("--models", default=",".join(MODEL_IDS))
    return parser


def safe(text: object) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def main() -> int:
    args = build_parser().parse_args()
    models = tuple(item.strip() for item in args.models.split(",") if item.strip())
    run_dir = (args.results_root / args.run_id).resolve()
    output = args.output or (run_dir / "review.md")

    eval_rows = read_jsonl(args.eval_file.resolve())
    predictions: dict[str, dict[str, dict]] = {}
    for model in models:
        path = run_dir / "raw" / f"{model}.jsonl"
        predictions[model] = {row["id"]: row for row in read_jsonl(path)}

    lines = [
        f"# 评测结果人工评审：{args.run_id}",
        "",
        f"题目数：{len(eval_rows)}；模型数：{len(models)}",
        "",
        "> 请逐题比较金标答案与模型答案，并记录人工结论。原始回答未做改写。",
        "",
    ]

    for index, gold in enumerate(eval_rows, 1):
        sample_id = gold["id"]
        lines.extend([
            "---",
            "",
            f"## {index}. {sample_id}",
            "",
            f"**题型：** `{gold.get('type', '')}`",
            "",
            "### 题目",
            "",
            safe(gold.get("question")),
            "",
            "### 金标答案",
            "",
            safe(gold.get("gold_answer")),
            "",
            f"- 金标条文：`{', '.join(gold.get('gold_clauses') or []) or '无'}`",
            f"- 金标数值：`{', '.join(map(str, gold.get('gold_values') or [])) or '无'}`",
            f"- 是否应拒答：`{gold.get('should_refuse', False)}`",
            "",
        ])

        for model in models:
            row = predictions[model].get(sample_id)
            if row is None:
                answer = "[缺少该题结果]"
                meta = ""
            else:
                answer = safe(row.get("answer")) or "[空答案]"
                meta = (
                    f"finish_reason={row.get('finish_reason', '')}; "
                    f"completion_tokens={row.get('completion_tokens', '')}"
                )
            lines.extend([
                f"### 模型：`{model}`",
                "",
                answer,
                "",
                f"`{meta}`" if meta else "",
                "",
                "**人工结论：** □正确　□部分正确　□错误　□应拒答　□其他：",
                "",
            ])

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 已导出：{output}")
    print(f"题目数：{len(eval_rows)}，模型数：{len(models)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
