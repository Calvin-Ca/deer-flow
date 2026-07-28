"""检查 C 组三道过滤器是否带视角偏向。

动因——过滤器③曾因 quality_score 未实装而退化成「永远保留靠前视角」，
造成甲方/监理被系统性淘汰（已修）。但实测过滤器①（可答性）单独淘汰 44.7%，
是③的 20 倍量级，同类偏向若存在于①，影响远大于③。

而①确实有偏向的**先验理由**：B 组的四视角提示词里，甲方问的是
「对工程安全性、耐久性的影响」，这类问题单条条文常常答不全，
判官如实答 NO 是对的——但结果就是 C 组的视角分布相对 B 组偏移。

这未必是 bug（判官没判错），但必须**知道**它存不存在、多大：
消融比的是「过滤 vs 不过滤」的质量差异，若 C 组同时还少了一整类视角，
那部分差异就归错了因。

用法：
    python scripts/check_group_c_bias.py
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_B = _ROOT / "data/processed/group_b/train.jsonl"
_C = _ROOT / "data/processed/group_c/train.jsonl"
_REJ = {
    "① 可答性":   _ROOT / "data/interim/filtered_out/rejected_answerable.jsonl",
    "② 条款准确性": _ROOT / "data/interim/filtered_out/rejected_clause.jsonl",
    "③ 去重":     _ROOT / "data/interim/filtered_out/rejected_dedup.jsonl",
}
_PERSP = ["施工员", "设计师", "监理", "甲方"]


def _load(path: Path) -> list[dict]:
    """读 jsonl，文件不存在时返回空列表。

    Args:
        path: jsonl 路径

    Returns:
        样本列表
    """
    if not path.exists():
        print(f"⚠️ 缺文件：{path.relative_to(_ROOT)}")
        return []
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


def _persp(rows: list[dict]) -> collections.Counter:
    """统计样本的视角分布。

    Args:
        rows: 样本列表

    Returns:
        视角 → 条数
    """
    return collections.Counter(
        (r.get("meta") or {}).get("perspective", "未知") for r in rows
    )


def main() -> None:
    """打印 B→C 各视角的存活率与每道闸的视角淘汰分布。

    Args:
        无

    Returns:
        None（结果打印到标准输出）
    """
    b, c = _load(_B), _load(_C)
    if not b or not c:
        return
    cb, cc = _persp(b), _persp(c)

    print(f"B 组 {len(b)} 条 → C 组 {len(c)} 条（整体存活 {len(c)/len(b):.1%}）\n")
    print("各视角存活率：")
    print(f"  {'视角':<8}{'B 组':>8}{'C 组':>8}{'存活率':>9}")
    rates = {}
    for p in _PERSP:
        r = cc[p] / cb[p] if cb[p] else 0.0
        rates[p] = r
        print(f"  {p:<8}{cb[p]:>8}{cc[p]:>8}{r:>9.1%}")

    spread = max(rates.values()) - min(rates.values())
    print(f"\n  最高与最低存活率相差 {spread:.1%}", end="  ")
    if spread < 0.10:
        print("→ ✅ 视角基本均衡")
    elif spread < 0.20:
        print("→ ⚠️ 有可见偏向，建议记入 EXPERIMENT.md")
    else:
        print("→ ❌ 偏向显著，C 组少了一整类视角，需处理")

    print("\n各闸淘汰的视角构成（看是谁在被筛掉）：")
    for name, path in _REJ.items():
        rows = _load(path)
        if not rows:
            continue
        cnt = _persp(rows)
        n = len(rows)
        share = "  ".join(f"{p} {cnt[p]:>4} ({cnt[p]/n:>4.0%})" for p in _PERSP)
        print(f"  {name:<12} 共 {n:>5} 条 | {share}")
    print(f"\n  参考：B 组各视角占比均为 25% 左右（{'  '.join(f'{p} {cb[p]/len(b):.0%}' for p in _PERSP)}）")
    print("  某闸若明显偏离 25%，即该闸在按视角筛选。")

    # 条文覆盖：C 组还剩多少条文有样本
    def _clauses(rows: list[dict]) -> set[str]:
        return {cid for r in rows for cid in (r.get("meta") or {}).get("source_clauses", [])}

    bc, ccl = _clauses(b), _clauses(c)
    print(f"\n条文覆盖：B 组 {len(bc)} 条条文 → C 组 {len(ccl)} 条（{len(ccl)/len(bc):.1%}）")
    print(f"  有 {len(bc - ccl)} 条条文在 C 组里一条样本都不剩")


if __name__ == "__main__":
    main()
