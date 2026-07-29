"""训练 JSONL 的严格累计统计。

该模块只依赖标准库，数据脚本和回归测试都可在未安装模型 SDK 的环境中使用。
"""
from __future__ import annotations

import json
from pathlib import Path


def scan_training_jsonl(path: Path) -> tuple[int, set[str]]:
    """统计最终训练文件的样本数与来源条款，并拒绝损坏或错 schema 的行。

    Args:
        path: 训练数据 JSONL 路径

    Returns:
        (有效样本总数, 覆盖的来源条款 ID 集合)

    Raises:
        ValueError: 某个非空行不是合法训练样本
    """
    total = 0
    source_clauses: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} 第 {line_no} 行不是合法 JSON") from exc
            if not isinstance(sample, dict):
                raise ValueError(f"{path} 第 {line_no} 行不是 JSON 对象")

            meta = sample.get("meta") or {}
            if not isinstance(meta, dict):
                raise ValueError(f"{path} 第 {line_no} 行的 meta 不是 JSON 对象")
            clause_ids = meta.get("source_clauses") or []
            if not isinstance(clause_ids, list) or not all(
                isinstance(clause_id, str) for clause_id in clause_ids
            ):
                raise ValueError(f"{path} 第 {line_no} 行的 meta.source_clauses 不是字符串列表")

            total += 1
            source_clauses.update(clause_ids)
    return total, source_clauses
