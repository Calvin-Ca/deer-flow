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


# 参与指纹的字段：凡是**下游会用来造数据**的字段都必须计入。
#
# text —— A/B/C 三组的样本内容直接来自它。
# refs —— D1 的条文配对完全由它决定。实测教训：条文库在 8a6a0aec → 477572a9
#         之间 text 一字未改、只有 282 条的 refs 变了（修 ClauseRef.clause_id
#         多插连字符的 bug，refs 命中率 0% → 99%）。若指纹只含 text，
#         这次改动指纹纹丝不动，D1 的陈旧就永远抓不出来——而 D1 恰恰是唯一
#         依赖 refs 的组。
#
# 反之，构建时间戳一类的字段不计入：它们与"能否复现出同样的数据"无关，
# 计入只会让指纹因无关因素频繁失配，失去语义。
_FIELDS = ("text", "refs")


def clauses_fingerprint(path: Path | None = None) -> str:
    """对条文库的内容取指纹。

    按 clause_id 排序后哈希 _FIELDS 里的字段：行序变动不应改变指纹
    （否则会因无关因素频繁失配），字段取值变动则必须改变它。

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
                # refs 是列表，排序后序列化——顺序变动不改变语义，不应改变指纹
                payload = json.dumps(
                    [sorted(c[f]) if isinstance(c.get(f), list) else c.get(f, "")
                     for f in _FIELDS],
                    ensure_ascii=False, sort_keys=True,
                )
                items.append((c["clause_id"], payload))
    items.sort()
    h = hashlib.md5()
    for cid, payload in items:
        h.update(cid.encode())
        h.update(b"\x00")
        h.update(payload.encode())
        h.update(b"\x00")
    return h.hexdigest()[:12]
