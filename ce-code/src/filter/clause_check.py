"""
过滤器 2：条款准确性（本项目技术核心）

判断逻辑：
  1. 从答案中抽取条款号 → 查条文库是否存在（归一化后）
  2. 答案中出现的关键数值与条文原文比对
  - 条款号不存在 → 淘汰
  - 数值与原文明显冲突 → 淘汰

完全自动化，不依赖 LLM。

运行：
  python -m src.filter.clause_check --input data/interim/filtered_answerable.jsonl --output data/interim/filtered_clause.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

from src.eval.clause import extract as _extract_refs


# ── 数值抽取 ─────────────────────────────────────────────────────────────

_NUM_PATTERN = re.compile(
    r"""
    (?:
        \d+(?:\.\d+)?       # 整数或小数
        \s*(?:mm|cm|m|kN|N|MPa|kPa|Pa|%|‰|°)?   # 可选单位
    )
    """,
    re.VERBOSE,
)


def _extract_numbers(text: str) -> set[str]:
    """提取文本中的所有数值字符串（忽略单位）。"""
    nums = set()
    for m in _NUM_PATTERN.finditer(text):
        raw = re.sub(r"[^\d.]", "", m.group(0))
        if raw and raw != ".":
            nums.add(raw)
    return nums


def _numbers_conflict(answer_nums: set[str], clause_nums: set[str]) -> bool:
    """
    答案引用的数值是否与条文冲突。
    策略：答案中出现的"关键数值"（不在条文中的）认为是幻觉数值。
    只有当冲突数值较多时才淘汰（单个数值差异可能是计算值，不淘汰）。
    """
    if not clause_nums:
        return False  # 条文无数值，无法比对
    spurious = answer_nums - clause_nums - {"0", "1", "2", "3", "4"}  # 排除常见系数
    # 超过 2 个数值不在条文中才淘汰（避免过度严苛）
    return len(spurious) > 2


# ── 主过滤逻辑 ──────────────────────────────────────────────────────────

def filter_clause_check(
    input_path: Path,
    output_path: Path,
    rejected_path: Path,
    clauses_path: Path,
) -> tuple[int, int]:
    """返回 (kept, rejected)。"""
    # 构建条文库 index（clause_id 格式：GB50010-2010_8.2.1）
    clause_ids_set: set[str] = set()
    clause_texts: dict[str, str] = {}
    with open(clauses_path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            clause_ids_set.add(c["clause_id"])
            clause_texts[c["clause_id"]] = c["text"]

    samples: list[dict] = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            samples.append(json.loads(line))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)

    kept = 0
    rejected = 0

    with open(output_path, "w", encoding="utf-8") as fout, \
         open(rejected_path, "w", encoding="utf-8") as frej:
        for sample in samples:
            answer = sample["conversations"][1]["value"]
            source_ids = sample["meta"].get("source_clauses", [])

            # ① 抽取答案中的条款引用（返回 ClauseRef 列表）
            cited_refs = _extract_refs(answer)
            cited_ids = {ref.clause_id for ref in cited_refs}

            # ② 检查幻觉条款号（出现在答案中但不存在于条文库）
            phantom_ids = cited_ids - clause_ids_set
            if phantom_ids:
                sample["meta"]["reject_reason"] = "phantom_clause"
                sample["meta"]["phantom_ids"] = list(phantom_ids)
                frej.write(json.dumps(sample, ensure_ascii=False) + "\n")
                rejected += 1
                continue

            # ③ 数值比对：答案数值是否与来源条文数值冲突
            source_texts = [clause_texts.get(cid, "") for cid in source_ids if cid in clause_texts]
            clause_full_text = " ".join(source_texts)
            clause_nums = _extract_numbers(clause_full_text)
            answer_nums = _extract_numbers(answer)

            if _numbers_conflict(answer_nums, clause_nums):
                sample["meta"]["reject_reason"] = "number_conflict"
                frej.write(json.dumps(sample, ensure_ascii=False) + "\n")
                rejected += 1
                continue

            sample["meta"]["filters_passed"] = sample["meta"].get("filters_passed", []) + ["clause_accurate"]
            fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
            kept += 1

    rate = rejected / (kept + rejected) if (kept + rejected) else 0
    print(f"[clause_check] 保留 {kept} / 淘汰 {rejected}（淘汰率 {rate:.1%}）")
    return kept, rejected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rejected", default=None)
    parser.add_argument("--clauses", default=str(_ROOT / "data/interim/clauses.jsonl"))
    args = parser.parse_args()

    out = Path(args.output)
    rej = Path(args.rejected) if args.rejected else out.parent / "rejected_clause.jsonl"
    filter_clause_check(
        input_path=Path(args.input),
        output_path=out,
        rejected_path=rej,
        clauses_path=Path(args.clauses),
    )
