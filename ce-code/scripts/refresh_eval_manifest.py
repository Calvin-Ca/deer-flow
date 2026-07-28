"""从评测集文件重算 manifest，并记录泄漏检查与替换历史。

为什么需要它——manifest 是**构建时**写的一次性快照，之后每一次对评测集的
后处理（去重、修重复 id、泄漏题替换）都会让它失真，而没有任何机制会提醒。
实测：manifest 记 388 题（single_clause 116 / refusal 40），文件里只有 386
（115 / 39）——「修重复 id」丢了 2 题后 manifest 没重生成，此后一直错着。

这不是无关紧要的元数据：阶段 5 的分题型准确率以题型配额为分母，
manifest 错了，分母就错了，而指标看起来完全正常。

本脚本以**文件为准**重算，并补记两类此前缺失的信息：
  · 泄漏检查状态（何时查的、阈值、结果、四组指纹）
  · 替换历史（哪些题被换过、原因）

用法：
    python scripts/refresh_eval_manifest.py
"""
from __future__ import annotations

import collections
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_ROOT))

_EVAL = _ROOT / "data/eval/evalset_v1.jsonl"
_REPLACED = _ROOT / "data/eval/evalset_v1_replaced.jsonl"
_REPORT = _ROOT / "data/eval/leakage_report.md"
_MANIFEST = _ROOT / "data/eval/manifest.json"


def _leakage_status() -> dict:
    """从泄漏报告里解析检查状态。

    Args:
        无

    Returns:
        含检查时间、阈值、泄漏题数的字典；无报告时标记 checked=False
    """
    if not _REPORT.exists():
        return {"checked": False, "note": "未执行泄漏检查（铁律 3 红线）"}
    text = _REPORT.read_text(encoding="utf-8")
    m_total = re.search(r"\*\*泄漏题数：(\d+) / (\d+)\*\*", text)
    m_time = re.search(r"生成时间：(\S+)", text)
    m_thr = re.search(r"阈值：cosine > ([\d.]+)", text)
    return {
        "checked": True,
        "checked_at": m_time.group(1) if m_time else None,
        "threshold": float(m_thr.group(1)) if m_thr else None,
        "leaked": int(m_total.group(1)) if m_total else None,
        "against": int(m_total.group(2)) if m_total else None,
    }


def _group_fingerprints() -> dict:
    """收集四组训练数据的条文库指纹。

    评测集的泄漏结论只对**特定四组数据**成立；数据一变结论就失效，
    故把当时比对的四组指纹一并记进 manifest。

    Args:
        无

    Returns:
        组名 → 指纹
    """
    out = {}
    for g in "abcd":
        mf = _ROOT / f"data/processed/group_{g}/manifest.json"
        if mf.exists():
            out[g] = json.loads(mf.read_text(encoding="utf-8")).get("clauses_fingerprint")
    return out


def main() -> int:
    """重算并写回评测集 manifest。

    Args:
        无

    Returns:
        退出码：0 成功
    """
    if not _EVAL.exists():
        print(f"缺评测集：{_EVAL}")
        return 1

    items = [json.loads(l) for l in _EVAL.open(encoding="utf-8") if l.strip()]
    counts = collections.Counter(it["type"] for it in items)

    old = json.loads(_MANIFEST.read_text(encoding="utf-8")) if _MANIFEST.exists() else {}
    if old:
        drift = [(k, old.get("type_counts", {}).get(k), counts[k])
                 for k in set(list(counts) + list(old.get("type_counts", {})))
                 if old.get("type_counts", {}).get(k) != counts[k]]
        if drift or old.get("total") != len(items):
            print("旧 manifest 与文件不符（本次修正）：")
            if old.get("total") != len(items):
                print(f"  总数  {old.get('total')} → {len(items)}")
            for k, o, n in sorted(drift):
                print(f"  {k:<16}{o} → {n}")
        else:
            print("旧 manifest 与文件一致，仅补充泄漏/替换信息")

    replaced = []
    if _REPLACED.exists():
        seen = set()
        for line in _REPLACED.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                if r["id"] not in seen:
                    seen.add(r["id"])
                    replaced.append({"id": r["id"], "type": r.get("type"),
                                     "reason": "leakage_check cosine > 0.9"})

    new = dict(old)
    new.update({
        "version": old.get("version", "v1"),
        "total": len(items),
        "type_counts": dict(sorted(counts.items())),
        # 金标覆盖：非拒答题里有多少能进 5.4 数值匹配的分母
        "gold_verified_count": sum(1 for it in items if it.get("gold_verified")),
        "gold_clauses_count": len({c for it in items for c in (it.get("gold_clauses") or [])}),
        "leakage_check": _leakage_status(),
        "leakage_checked_against": _group_fingerprints(),
        "replaced_questions": replaced,
        "manifest_refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _MANIFEST.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n评测集 {len(items)} 题")
    for k, v in sorted(counts.items()):
        print(f"  {k:<16}{v:>4}")
    lk = new["leakage_check"]
    print(f"\n泄漏检查：{'已执行' if lk.get('checked') else '未执行'}", end="")
    if lk.get("checked"):
        print(f"　泄漏 {lk['leaked']}/{lk['against']}　阈值 {lk['threshold']}")
    else:
        print()
    print(f"替换历史：{len(replaced)} 题")
    print(f"比对的四组指纹：{new['leakage_checked_against']}")
    print(f"\n→ {_MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
