"""检查评测集的金标条文在四组训练数据里各被覆盖了多少。

动因——C 组过滤后有 397 条条文（16.9%）一条样本都不剩。若评测题恰好出自这些
条文，C/D 组是**结构性答不出来**的：模型压根没见过那段内容。这会在结果里
表现成「过滤反而有害」，而真实原因是知识覆盖缺口，不是过滤降低了质量。

这个区分很要紧：本项目的命题是「高质量合成数据 < 数量」，若 C 组因覆盖缺口失分，
就会得出与事实相反的结论。

四组的覆盖差异本身是策略的固有属性（A 组模板复制覆盖全部条文，C 组过滤后必然更少），
不是 bug；但必须量化并在解读结果时扣除，否则归因错误。

用法：
    python scripts/check_eval_coverage.py
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

_ROOT = Path(__file__).parents[1]
_EVAL = _ROOT / "data/eval/evalset_v1.jsonl"
_GROUPS = {g: _ROOT / f"data/processed/group_{g}/train.jsonl" for g in ("a", "b", "c", "d")}


def _covered_clauses(path: Path) -> set[str]:
    """读训练集，返回其中出现过的全部条文 id。

    Args:
        path: train.jsonl 路径

    Returns:
        条文 id 集合；文件不存在时为空集
    """
    if not path.exists():
        return set()
    out: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.update((json.loads(line).get("meta") or {}).get("source_clauses", []))
    return out


def main() -> None:
    """打印各组对评测集金标条文的覆盖率与结构性失分题数。

    Args:
        无

    Returns:
        None（结果打印到标准输出）
    """
    if not _EVAL.exists():
        print(f"缺评测集：{_EVAL}")
        return
    questions = [json.loads(l) for l in _EVAL.open(encoding="utf-8") if l.strip()]

    # 拒答题没有金标条文，本就不考察知识覆盖，单独剔出
    gold_qs = [q for q in questions if q.get("gold_clauses")]
    refusal = len(questions) - len(gold_qs)
    gold_clauses = {c for q in gold_qs for c in q["gold_clauses"]}
    print(f"评测集 {len(questions)} 题（其中 {refusal} 题拒答题无金标条文，不计入覆盖考察）")
    print(f"需考察 {len(gold_qs)} 题，涉及金标条文 {len(gold_clauses)} 条\n")

    print(f"{'组':<4}{'样本数':>8}{'覆盖条文':>10}{'金标条文覆盖':>14}{'全覆盖题':>10}{'零覆盖题':>10}")
    print("─" * 58)
    results = {}
    for g, path in _GROUPS.items():
        if not path.exists():
            print(f"{g:<4}{'（缺文件）':>8}")
            continue
        cov = _covered_clauses(path)
        n_samples = sum(1 for line in path.open(encoding="utf-8") if line.strip())
        hit = gold_clauses & cov
        full = sum(1 for q in gold_qs if set(q["gold_clauses"]) <= cov)
        zero = sum(1 for q in gold_qs if not (set(q["gold_clauses"]) & cov))
        results[g] = (cov, full, zero)
        print(f"{g:<4}{n_samples:>8}{len(cov):>10}"
              f"{len(hit):>9}/{len(gold_clauses):<4}{full:>10}{zero:>10}")

    if "a" not in results or "c" not in results:
        return

    print("\n按题型看 C 组的零覆盖分布（这些题 C/D 组结构性答不出）：")
    cov_c = results["c"][0]
    by_type: collections.Counter = collections.Counter()
    total_by_type: collections.Counter = collections.Counter()
    for q in gold_qs:
        total_by_type[q.get("type", "?")] += 1
        if not (set(q["gold_clauses"]) & cov_c):
            by_type[q.get("type", "?")] += 1
    for t, n in total_by_type.most_common():
        z = by_type[t]
        flag = "  ← 占比高" if n and z / n > 0.2 else ""
        print(f"  {t:<16}{z:>4}/{n:<4}({z/n:>4.0%}){flag}")

    zero_a, zero_c = results["a"][2], results["c"][2]
    print(f"\n结论：A 组 {zero_a} 题零覆盖，C 组 {zero_c} 题零覆盖，"
          f"差 {zero_c - zero_a} 题（占需考察题的 {(zero_c - zero_a)/len(gold_qs):.1%}）")
    print("  这部分差距来自知识覆盖缺口，不是数据质量差异——")
    print("  解读 C/D 组得分时须扣除，否则会把覆盖损失误判成「过滤有害」。")


if __name__ == "__main__":
    main()
