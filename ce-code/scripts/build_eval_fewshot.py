"""从已冻结的 C/D 训练集确定性选择并生成评测 3-shot prompt。

用户已确认的选择口径：
  1. group_c：一条完整通过三重过滤的 single_clause；
  2. group_d：一条 cross_clause；
  3. group_d：一条 refusal；
  4. 三条样本的 source_clauses 均不得与评测集 gold_clauses 重合；
  5. 固定 seed=42，候选按 sample_id 排序后独立确定性抽样。

运行：
    .venv/bin/python scripts/build_eval_fewshot.py

输出：
    configs/prompts/eval_fewshot.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_EVAL_FILE = _ROOT / "data/eval/evalset_v1.jsonl"
_EVAL_MANIFEST = _ROOT / "data/eval/manifest.json"
_GROUP_C = _ROOT / "data/processed/group_c/train.jsonl"
_GROUP_D = _ROOT / "data/processed/group_d/train.jsonl"
_OUTPUT = _ROOT / "configs/prompts/eval_fewshot.json"
_REJECTIONS = _ROOT / "configs/prompts/eval_fewshot_rejections.json"
_SEED = 42

_SELECTIONS = (
    {
        "source_dataset": "group_c",
        "sample_type": "single_clause",
        "required_filters": {"answerable", "clause_accurate", "diverse"},
    },
    {
        "source_dataset": "group_d",
        "sample_type": "cross_clause",
        "required_filters": {"cross_clause_verified"},
    },
    {
        "source_dataset": "group_d",
        "sample_type": "refusal",
        "required_filters": set(),
    },
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"应为 JSON 对象：{path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_question(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _record_path(path: Path) -> str:
    """仓库内记相对路径，测试或显式外部路径则如实记绝对路径。"""
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path.resolve())


def _load_eval_contract(
    eval_file: Path,
    eval_manifest_path: Path,
    group_manifests: dict[str, Path],
) -> tuple[set[str], set[str]]:
    """校验泄漏报告仍对应当前 C/D 数据，并返回评测条文和问题集合。"""
    eval_manifest = _load_json(eval_manifest_path)
    leakage = eval_manifest.get("leakage_check") or {}
    if leakage.get("checked") is not True or leakage.get("leaked") != 0:
        raise ValueError("评测集尚未通过零泄漏检查，禁止生成 few-shot")
    checked_against = eval_manifest.get("leakage_checked_against") or {}

    for group, manifest_path in group_manifests.items():
        manifest = _load_json(manifest_path)
        fingerprint = manifest.get("clauses_fingerprint")
        if not fingerprint:
            raise ValueError(f"{group} manifest 未记录 clauses_fingerprint")
        if checked_against.get(group[-1]) != fingerprint:
            raise ValueError(
                f"泄漏报告中的 {group} 指纹 {checked_against.get(group[-1])!r} "
                f"与训练数据 {fingerprint!r} 不一致"
            )

    gold_clauses: set[str] = set()
    questions: set[str] = set()
    count = 0
    with eval_file.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                raise ValueError(f"{eval_file}:{line_no} 是空行")
            row = json.loads(line)
            gold_clauses.update(row.get("gold_clauses") or [])
            question = row.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"{eval_file}:{line_no} question 为空")
            questions.add(_normalize_question(question))
            count += 1
    if count != eval_manifest.get("total"):
        raise ValueError(
            f"评测文件实际 {count} 题，与 manifest "
            f"total={eval_manifest.get('total')} 不一致"
        )
    return gold_clauses, questions


def _conversation(sample: dict[str, Any]) -> tuple[str, str]:
    conversations = sample.get("conversations")
    if not isinstance(conversations, list):
        raise ValueError("conversations 不是数组")
    user = next(
        (
            item.get("value")
            for item in conversations
            if isinstance(item, dict) and item.get("from") == "human"
        ),
        None,
    )
    assistant = next(
        (
            item.get("value")
            for item in conversations
            if isinstance(item, dict) and item.get("from") == "gpt"
        ),
        None,
    )
    if not isinstance(user, str) or not user.strip():
        raise ValueError("缺少 human 问题")
    if not isinstance(assistant, str) or not assistant.strip():
        raise ValueError("缺少 gpt 答案")
    return user.strip(), assistant.strip()


def load_rejections(path: Path) -> list[dict[str, Any]]:
    """加载人工审查否决项，并拒绝重复或字段不完整的记录。"""
    value = _load_json(path)
    rows = value.get("rejections")
    if not isinstance(rows, list):
        raise ValueError(f"rejections 不是数组：{path}")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: rejection 第 {index} 项不是对象")
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"{path}: rejection 第 {index} 项 sample_id 为空")
        if sample_id in seen:
            raise ValueError(f"{path}: 重复 rejection sample_id={sample_id}")
        seen.add(sample_id)
        if row.get("source_dataset") not in {"group_c", "group_d"}:
            raise ValueError(f"{path}: {sample_id} source_dataset 非法")
        if row.get("sample_type") not in {
            "single_clause",
            "cross_clause",
            "refusal",
        }:
            raise ValueError(f"{path}: {sample_id} sample_type 非法")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError(f"{path}: {sample_id} reason 为空")
        normalized.append(row)
    return normalized


def load_candidates(
    path: Path,
    *,
    sample_type: str,
    required_filters: set[str],
    eval_gold_clauses: set[str],
    eval_questions: set[str],
    rejected_sample_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """加载满足题型、过滤状态和零条文重合要求的候选样本。"""
    if not path.is_file():
        raise FileNotFoundError(
            f"训练文件不存在：{path}\n"
            "train.jsonl 不进 Git，请在四组训练所在的服务器运行本脚本。"
        )
    rejected_sample_ids = rejected_sample_ids or set()
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_no} 是空行")
            sample = json.loads(line)
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id:
                raise ValueError(f"{path}:{line_no} sample_id 为空")
            if sample_id in seen_ids:
                raise ValueError(f"{path}:{line_no} 重复 sample_id={sample_id}")
            seen_ids.add(sample_id)
            if sample_id in rejected_sample_ids:
                continue

            meta = sample.get("meta") or {}
            if meta.get("sample_type") != sample_type:
                continue
            filters = set(meta.get("filters_passed") or [])
            if not required_filters.issubset(filters):
                continue
            source_clauses = meta.get("source_clauses") or []
            if not isinstance(source_clauses, list):
                raise ValueError(f"{path}:{line_no} source_clauses 不是数组")
            if set(source_clauses) & eval_gold_clauses:
                continue
            user, assistant = _conversation(sample)
            if _normalize_question(user) in eval_questions:
                continue
            candidates.append(
                {
                    "sample_id": sample_id,
                    "source_clauses": source_clauses,
                    "user": user,
                    "assistant": assistant,
                }
            )
    return sorted(candidates, key=lambda item: item["sample_id"])


def select_candidate(
    candidates: list[dict[str, Any]],
    *,
    seed: int,
    source_dataset: str,
    sample_type: str,
) -> dict[str, Any]:
    """使用独立派生种子选择一条，避免其他题型候选变化扰动本题型。"""
    if not candidates:
        raise ValueError(f"{source_dataset}/{sample_type} 没有合格候选")
    seed_material = f"{seed}:{source_dataset}:{sample_type}".encode()
    derived_seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
    return random.Random(derived_seed).choice(candidates)


def build_fewshot(
    *,
    eval_file: Path,
    eval_manifest: Path,
    group_c: Path,
    group_d: Path,
    rejections_path: Path,
    seed: int,
) -> list[dict[str, Any]]:
    group_paths = {"group_c": group_c, "group_d": group_d}
    group_manifests = {
        group: path.parent / "manifest.json"
        for group, path in group_paths.items()
    }
    eval_gold_clauses, eval_questions = _load_eval_contract(
        eval_file,
        eval_manifest,
        group_manifests,
    )
    source_hashes = {group: _sha256(path) for group, path in group_paths.items()}
    rejections = load_rejections(rejections_path)
    rejections_sha256 = _sha256(rejections_path)

    frozen: list[dict[str, Any]] = []
    for spec in _SELECTIONS:
        source_dataset = spec["source_dataset"]
        sample_type = spec["sample_type"]
        source_path = group_paths[source_dataset]
        relevant_rejections = [
            row
            for row in rejections
            if row["source_dataset"] == source_dataset
            and row["sample_type"] == sample_type
        ]
        rejected_sample_ids = {
            row["sample_id"]
            for row in relevant_rejections
        }
        candidates = load_candidates(
            source_path,
            sample_type=sample_type,
            required_filters=spec["required_filters"],
            eval_gold_clauses=eval_gold_clauses,
            eval_questions=eval_questions,
            rejected_sample_ids=rejected_sample_ids,
        )
        picked = select_candidate(
            candidates,
            seed=seed,
            source_dataset=source_dataset,
            sample_type=sample_type,
        )
        frozen.append(
            {
                "sample_id": picked["sample_id"],
                "source_dataset": source_dataset,
                "source_file": f"data/processed/{source_dataset}/train.jsonl",
                "source_file_sha256": source_hashes[source_dataset],
                "sample_type": sample_type,
                "source_clauses": picked["source_clauses"],
                "selection_seed": seed,
                "candidate_count": len(candidates),
                "selection_rejections_file": _record_path(rejections_path),
                "selection_rejections_sha256": rejections_sha256,
                "excluded_sample_ids": sorted(rejected_sample_ids),
                "user": picked["user"],
                "assistant": picked["assistant"],
            }
        )
    return frozen


def _write_frozen(path: Path, frozen: list[dict[str, Any]]) -> None:
    content = json.dumps(frozen, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == content:
            print(f"✅ 3-shot 已冻结且内容一致：{path}")
            return
        raise FileExistsError(
            f"已有 3-shot 与本次选择不同，拒绝覆盖：{path}\n"
            "请先保留旧文件并使用新的输出路径，禁止静默改变实验 prompt。"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"✅ 已生成：{path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确定性冻结 base_fewshot 的三条示例")
    parser.add_argument("--eval-file", type=Path, default=_EVAL_FILE)
    parser.add_argument("--eval-manifest", type=Path, default=_EVAL_MANIFEST)
    parser.add_argument("--group-c", type=Path, default=_GROUP_C)
    parser.add_argument("--group-d", type=Path, default=_GROUP_D)
    parser.add_argument("--rejections", type=Path, default=_REJECTIONS)
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    parser.add_argument("--seed", type=int, default=_SEED)
    args = parser.parse_args(argv)

    try:
        frozen = build_fewshot(
            eval_file=args.eval_file.resolve(),
            eval_manifest=args.eval_manifest.resolve(),
            group_c=args.group_c.resolve(),
            group_d=args.group_d.resolve(),
            rejections_path=args.rejections.resolve(),
            seed=args.seed,
        )
        _write_frozen(args.output.resolve(), frozen)
        print(f"选择 seed：{args.seed}")
        for index, item in enumerate(frozen, 1):
            print(
                f"  {index}. {item['source_dataset']}/{item['sample_type']} "
                f"id={item['sample_id']} 候选={item['candidate_count']} "
                f"条文={item['source_clauses']}"
            )
        print(f"prompt SHA-256：{_sha256(args.output.resolve())}")
        return 0
    except (FileNotFoundError, FileExistsError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
