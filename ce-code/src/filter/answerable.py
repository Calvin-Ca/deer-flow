"""
过滤器 1：可答性

判断：仅凭条文原文，LLM 能否回答该问题？
- 能答 → 保留
- "信息不足" / 无法确定 → 淘汰

运行（单独可跑，也被 group_c.py 调用）：
  python -m src.filter.answerable --input data/processed/group_b/train.jsonl --output data/interim/filtered_answerable.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT))

from src.utils.llm import call as llm_call, print_cost_summary

_SYSTEM = "你是一位建筑结构工程专家，请严格依据所给条文内容回答问题，不得补充条文之外的信息。"

_TEMPLATE = """以下是规范条文原文：

【条文】
{clause_text}

请判断：仅凭上述条文，能否完整回答以下问题？

【问题】
{question}

若能回答，请直接作答；若条文信息不足以回答该问题，请仅输出：INSUFFICIENT"""


def _get_clause_text(clause_id: str, clause_map: dict[str, str]) -> str:
    return clause_map.get(clause_id, "")


def _is_answerable(response: str) -> bool:
    """INSUFFICIENT 或其变体 → 不可答。"""
    r = response.strip().upper()
    return "INSUFFICIENT" not in r and len(r) > 20


def filter_answerable(
    input_path: Path,
    output_path: Path,
    rejected_path: Path,
    clauses_path: Path,
    model: str = "qwen-max",
    seed: int = 42,
) -> tuple[int, int]:
    """
    返回 (kept, rejected)。
    """
    # 加载条文库（clause_id → text）
    clause_map: dict[str, str] = {}
    with open(clauses_path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            clause_map[c["clause_id"]] = c["text"]

    samples: list[dict] = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            samples.append(json.loads(line))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)

    kept = 0
    rejected = 0

    try:
        from tqdm import tqdm
        iterator = tqdm(samples, desc="answerable filter")
    except ImportError:
        iterator = samples  # type: ignore

    with open(output_path, "w", encoding="utf-8") as fout, \
         open(rejected_path, "w", encoding="utf-8") as frej:
        for sample in iterator:
            question = sample["conversations"][0]["value"]
            clause_ids = sample["meta"].get("source_clauses", [])
            clause_text = "\n\n".join(
                clause_map.get(cid, "") for cid in clause_ids if cid in clause_map
            ).strip()
            if not clause_text:
                # 条文找不到 → 直接淘汰
                sample["meta"]["reject_reason"] = "clause_not_found"
                frej.write(json.dumps(sample, ensure_ascii=False) + "\n")
                rejected += 1
                continue

            prompt = _TEMPLATE.format(clause_text=clause_text, question=question)
            try:
                response = llm_call(
                    prompt,
                    system=_SYSTEM,
                    model=model,
                    max_tokens=1024,
                    temperature=0.0,
                    seed=seed,
                    sample_id=sample["sample_id"],
                )
            except Exception:
                # API 失败 → 保守保留，不淘汰
                fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
                kept += 1
                continue

            if _is_answerable(response):
                sample["meta"]["filters_passed"] = sample["meta"].get("filters_passed", []) + ["answerable"]
                fout.write(json.dumps(sample, ensure_ascii=False) + "\n")
                kept += 1
            else:
                sample["meta"]["reject_reason"] = "not_answerable"
                frej.write(json.dumps(sample, ensure_ascii=False) + "\n")
                rejected += 1

    print(f"[answerable] 保留 {kept} / 淘汰 {rejected}（淘汰率 {rejected/(kept+rejected):.1%}）")
    print_cost_summary()
    return kept, rejected


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rejected", default=None)
    parser.add_argument("--clauses", default=str(_ROOT / "data/interim/clauses.jsonl"))
    args = parser.parse_args()

    out = Path(args.output)
    rej = Path(args.rejected) if args.rejected else out.parent / "rejected_answerable.jsonl"
    filter_answerable(
        input_path=Path(args.input),
        output_path=out,
        rejected_path=rej,
        clauses_path=Path(args.clauses),
    )
