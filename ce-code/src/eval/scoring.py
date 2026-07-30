"""阶段 5 自动评分：条款引用、数值、拒答和输出健康指标。

本模块只读评测集、条文库和推理 JSONL；不会修改任何原始结果。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from src.eval.clause import clause_f1, extract, is_hallucinated

MODEL_IDS = ("base", "base_fewshot", "group_a", "group_b", "group_c", "group_d")
_NUMBER_RE = re.compile(
    r"(?<![\w.])[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?%?"
)
_REFUSAL_RE = re.compile(
    r"无法(?:仅凭|确定|回答)|不能(?:仅凭|确定)|未(?:明确|提供)|并未提供|"
    r"信息不足|超出(?:本条|该条|条款)范围|需要.*(?:专业|工程师|现场).*判断|"
    r"建议由.*(?:工程师|专业人员)"
)
_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_no} 是空行")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no} 不是 JSON 对象")
            rows.append(row)
    return rows


def load_eval_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows = _read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_id = row.get("id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"{path} 存在空 id")
        if sample_id in result:
            raise ValueError(f"{path} 重复 id={sample_id}")
        result[sample_id] = row
    return result


def load_model_rows(
    path: Path,
    eval_rows: dict[str, dict[str, Any]],
    model_id: str,
) -> dict[str, dict[str, Any]]:
    rows = _read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_id = row.get("id")
        if sample_id not in eval_rows:
            raise ValueError(f"{path} 含不属于评测集的 id={sample_id!r}")
        if sample_id in result:
            raise ValueError(f"{path} 重复 id={sample_id}")
        if row.get("model_id") != model_id:
            raise ValueError(
                f"{path}:{sample_id} model_id={row.get('model_id')!r}，"
                f"期望 {model_id!r}"
            )
        if not isinstance(row.get("answer"), str):
            raise ValueError(f"{path}:{sample_id} answer 不是字符串")
        result[sample_id] = row
    if not result:
        raise ValueError(f"{path} 没有结果")
    return result


def load_clause_ids(path: Path) -> set[str]:
    rows = _read_jsonl(path)
    ids: set[str] = set()
    for row in rows:
        clause_id = row.get("clause_id")
        if not isinstance(clause_id, str) or not clause_id:
            raise ValueError(f"{path} 存在空 clause_id")
        ids.add(clause_id)
    return ids


def load_group_coverage(path: Path) -> set[str] | None:
    """读取训练集覆盖条文；训练 JSONL 不在本地时返回 None。"""
    if not path.is_file():
        return None
    covered: set[str] = set()
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            meta = row.get("meta") or {}
            clauses = meta.get("source_clauses") or []
            if not isinstance(clauses, list):
                raise ValueError(f"{path}:{line_no} source_clauses 不是数组")
            covered.update(item for item in clauses if isinstance(item, str))
    return covered


def _default_standard(question: str, gold_ids: Iterable[str]) -> str:
    refs = extract(question)
    standards = {f"{ref.std_code}-{ref.year}" if ref.year else ref.std_code for ref in refs}
    if len(standards) == 1:
        return next(iter(standards))
    gold_standards = {
        item.split("_", 1)[0]
        for item in gold_ids
        if "_" in item
    }
    return next(iter(gold_standards)) if len(gold_standards) == 1 else ""


def _decimal(value: str) -> Decimal | None:
    value = value.strip().rstrip("%")
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def numeric_values(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in _NUMBER_RE.findall(text):
        value = _decimal(match)
        if value is not None:
            values.append(value)
    return values


def numeric_score(answer: str, gold_values: list[Any]) -> dict[str, Any]:
    gold = [value for value in (_decimal(str(item)) for item in gold_values) if value is not None]
    predicted = numeric_values(answer)
    matched = sum(1 for value in gold if value in predicted)
    return {
        "gold_count": len(gold),
        "matched_count": matched,
        "exact": bool(gold) and matched == len(gold),
    }


def is_refusal(answer: str) -> bool:
    return bool(_REFUSAL_RE.search(answer))


def is_repetitive(answer: str) -> bool:
    normalized = re.sub(r"\s+", "", answer)
    if len(normalized) < 80:
        return False
    sentences = [item for item in _SPLIT_RE.split(normalized) if len(item) >= 12]
    if any(count >= 3 for count in Counter(sentences).values()):
        return True
    ngram_counts = Counter(normalized[index : index + 24] for index in range(len(normalized) - 23))
    return any(count >= 3 for count in ngram_counts.values())


def score_row(
    eval_row: dict[str, Any],
    result_row: dict[str, Any],
    valid_clause_ids: set[str],
    coverage: set[str] | None,
) -> dict[str, Any]:
    answer = result_row["answer"]
    gold_ids = set(eval_row.get("gold_clauses") or [])
    default_std = _default_standard(eval_row.get("question", ""), gold_ids)
    predicted_refs = extract(answer, default_std=default_std)
    predicted_ids = {ref.clause_id for ref in predicted_refs}
    f1 = clause_f1(predicted_ids, gold_ids) if gold_ids else {
        "precision": None, "recall": None, "f1": None
    }
    numeric = numeric_score(answer, eval_row.get("gold_values") or [])
    refusal_gold = bool(eval_row.get("should_refuse"))
    refusal_pred = is_refusal(answer)
    return {
        "id": eval_row["id"],
        "type": eval_row["type"],
        "model_id": result_row["model_id"],
        "gold_clauses": sorted(gold_ids),
        "predicted_clauses": sorted(predicted_ids),
        "clause_true_positive": len(predicted_ids & gold_ids),
        "clause_predicted_count": len(predicted_ids),
        "clause_gold_count": len(gold_ids),
        "invalid_clauses": sorted(item for item in predicted_ids if is_hallucinated(item, valid_clause_ids)),
        "clause_precision": f1["precision"],
        "clause_recall": f1["recall"],
        "clause_f1": f1["f1"],
        "numeric_gold_count": numeric["gold_count"],
        "numeric_matched_count": numeric["matched_count"],
        "numeric_exact": numeric["exact"] if numeric["gold_count"] else None,
        "refusal_gold": refusal_gold,
        "refusal_pred": refusal_pred,
        "empty": not answer.strip(),
        "truncated": result_row.get("finish_reason") == "length",
        "repetitive": is_repetitive(answer),
        "completion_tokens": result_row.get("completion_tokens"),
        "covered": (
            bool(gold_ids & coverage) if coverage is not None and gold_ids else None
        ),
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def aggregate(rows: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    clause_rows = [row for row in rows if row["clause_f1"] is not None]
    numeric_rows = [row for row in rows if row["numeric_gold_count"] > 0]
    refusal_rows = rows
    refusal_tp = sum(row["refusal_gold"] and row["refusal_pred"] for row in refusal_rows)
    refusal_fn = sum(row["refusal_gold"] and not row["refusal_pred"] for row in refusal_rows)
    false_refusal = sum((not row["refusal_gold"]) and row["refusal_pred"] for row in refusal_rows)
    clause_tp = sum(row["clause_true_positive"] for row in clause_rows)
    clause_predicted = sum(row["clause_predicted_count"] for row in clause_rows)
    clause_gold = sum(row["clause_gold_count"] for row in clause_rows)
    micro_precision = clause_tp / clause_predicted if clause_predicted else None
    micro_recall = clause_tp / clause_gold if clause_gold else None
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if micro_precision is not None and micro_recall is not None and micro_precision + micro_recall
        else None
    )
    return {
        "scope": scope,
        "n": len(rows),
        "clause_n": len(clause_rows),
        "clause_precision": _mean([row["clause_precision"] for row in clause_rows]),
        "clause_recall": _mean([row["clause_recall"] for row in clause_rows]),
        "clause_f1": _mean([row["clause_f1"] for row in clause_rows]),
        "clause_precision_micro": round(micro_precision, 4) if micro_precision is not None else None,
        "clause_recall_micro": round(micro_recall, 4) if micro_recall is not None else None,
        "clause_f1_micro": round(micro_f1, 4) if micro_f1 is not None else None,
        "hard_hallucination_rate": round(
            sum(bool(row["invalid_clauses"]) for row in rows) / len(rows), 4
        ) if rows else None,
        "numeric_n": len(numeric_rows),
        "numeric_exact_accuracy": _mean([float(row["numeric_exact"]) for row in numeric_rows]),
        "numeric_value_accuracy": round(
            sum(row["numeric_matched_count"] for row in numeric_rows)
            / sum(row["numeric_gold_count"] for row in numeric_rows), 4
        ) if numeric_rows else None,
        "refusal_accuracy": _mean([float(row["refusal_gold"] == row["refusal_pred"]) for row in refusal_rows]),
        "refusal_recall": round(refusal_tp / (refusal_tp + refusal_fn), 4) if refusal_tp + refusal_fn else None,
        "false_refusal_rate": round(false_refusal / sum(not row["refusal_gold"] for row in refusal_rows), 4)
        if any(not row["refusal_gold"] for row in refusal_rows) else None,
        "empty_rate": _mean([float(row["empty"]) for row in rows]),
        "truncated_rate": _mean([float(row["truncated"]) for row in rows]),
        "repetitive_rate": _mean([float(row["repetitive"]) for row in rows]),
        "avg_completion_tokens": _mean(
            [float(row["completion_tokens"]) for row in rows if row["completion_tokens"] is not None]
        ),
        "covered_n": sum(row["covered"] is True for row in rows),
    }


SUMMARY_FIELDS = [
    "model_id", "scope", "n", "clause_n", "clause_precision", "clause_recall",
    "clause_f1", "clause_precision_micro", "clause_recall_micro", "clause_f1_micro",
    "hard_hallucination_rate", "numeric_n", "numeric_exact_accuracy",
    "numeric_value_accuracy", "refusal_accuracy", "refusal_recall", "false_refusal_rate",
    "empty_rate", "truncated_rate", "repetitive_rate", "avg_completion_tokens", "covered_n",
]


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in SUMMARY_FIELDS} for row in rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
