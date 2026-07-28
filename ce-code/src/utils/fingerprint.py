"""条文库内容指纹——判断某组训练数据是否派生自当前条文库。

为什么不能只比对 clause_id 集合：条文库的历史 bug 是「正文被条文说明覆盖」
（2026-07-27 修），**条款号完全没变，变的是内容**。旧数据引用的
`GB50010-2010_8.2.1` 在新库里照样存在，集合差为空——按 id 比对会误报「同源」，
而那正是本项目真实发生过的情况（条文库两次大修后 C/D 组仍是旧库产物）。

故改为对条文库的**内容**取指纹，写进各组 manifest；泄漏检查等红线环节
比对指纹而非 id。指纹对不上 = 该组需重建后再查。

用法：
    from src.utils.fingerprint import clauses_fingerprint
    manifest["clauses_fingerprint"] = clauses_fingerprint()
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_CLAUSES = _ROOT / "data/interim/clauses.jsonl"


def clauses_fingerprint(path: Path | None = None) -> str:
    """对条文库的内容取指纹。

    只取 (clause_id, text) 两个字段并按 id 排序后哈希：其余字段（如构建时间戳）
    变动不应改变指纹，而行序变动也不应——否则指纹会因无关因素频繁失配，
    失去"内容是否相同"这个语义。

    Args:
        path: 条文库路径，默认 data/interim/clauses.jsonl

    Returns:
        12 位十六进制指纹；文件不存在时返回空串
    """
    p = path or _CLAUSES
    if not p.exists():
        return ""
    items: list[tuple[str, str]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                c = json.loads(line)
                items.append((c["clause_id"], c.get("text", "")))
    items.sort()
    h = hashlib.md5()
    for cid, text in items:
        h.update(cid.encode())
        h.update(b"\x00")
        h.update(text.encode())
        h.update(b"\x00")
    return h.hexdigest()[:12]
