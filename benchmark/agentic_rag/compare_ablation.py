"""Agentic RAG track · 消融对比 —— 读多个臂的结果 JSON，横向出 delta 表，坐实 agentic 提升。

每个结果 JSON 由 `run_norm_ablation.py` 落盘（含 mode/variant_label/metrics）。第一个参数作基线，
其余臂逐指标报「值 + 相对基线 delta」。8B 非确定：建议每臂多轮取的结果各传进来看波动，或先各轮平均。

运行：
    python benchmark/agentic_rag/compare_ablation.py results/norm_naive_r1.json results/norm_agentic_r1.json
    （第一个=基线；可传 2+ 个臂）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# 关注指标：名 → (中文标签, 方向)。方向 up=越高越好、down=越低越好，用于给 delta 标好/坏。
_METRICS = [
    ("faithful_rate", "忠实率", "up"),
    ("unfaithful_case_rate", "幻觉引用用例率", "down"),
    ("answer_points_coverage", "答案要点覆盖", "up"),
    ("context_recall", "上下文召回(std)", "up"),
    ("refusal_accuracy", "拒答准确率", "up"),
    ("missed_refuse_rate", "漏拒率", "down"),
    ("false_refuse_rate", "误拒率", "down"),
]


def _fmt(v) -> str:
    """比率→百分比字符串（None→占位）。输入 v 比率或 None；输出字符串。"""
    return "  —  " if v is None else f"{v * 100:5.1f}%"


def _delta(base, cur, direction: str) -> str:
    """算相对基线的 delta 并标好坏（↑好=✓/↓坏=✗）。

    输入：base/cur 基线与当前臂的比率（可 None）；direction up/down 指该指标的好方向。
    输出：带正负号与好坏标记的 delta 字符串。
    """
    if base is None or cur is None:
        return "  —  "
    d = (cur - base) * 100
    good = (d > 0 and direction == "up") or (d < 0 and direction == "down")
    mark = "" if abs(d) < 0.05 else (" ✓" if good else " ✗")
    return f"{d:+5.1f}pp{mark}"


def main() -> int:
    """加载各臂结果、打印 delta 表。返回退出码。"""
    p = argparse.ArgumentParser(description="Agentic RAG 消融 delta 对比（第一个=基线）")
    p.add_argument("results", nargs="+", help="结果 JSON 路径，≥2 个；第一个作基线")
    args = p.parse_args()
    if len(args.results) < 2:
        print("至少传 2 个结果 JSON（基线 + ≥1 个对比臂）")
        return 1

    arms = [json.loads(Path(r).read_text(encoding="utf-8")) for r in args.results]
    base = arms[0]
    bm = base["metrics"]

    def _tag(a: dict) -> str:
        """臂标签=mode+variant+n。输入结果 dict；输出短标签串。"""
        return f"{a['mode']}/{a['variant_label']}(n={a['metrics'].get('n', '?')})"

    print(f"基线：{_tag(base)}  [{base['run_name']}]")
    for a in arms[1:]:
        print(f"对比：{_tag(a)}  [{a['run_name']}]")
    print()

    header = f"{'指标':<16}{'基线':>8}"
    for a in arms[1:]:
        header += f"{'臂'+str(arms.index(a)):>10}{'Δ':>11}"
    print(header)
    print("-" * len(header))

    for key, label, direction in _METRICS:
        row = f"{label:<16}{_fmt(bm.get(key)):>8}"
        for a in arms[1:]:
            cur = a["metrics"].get(key)
            row += f"{_fmt(cur):>10}{_delta(bm.get(key), cur, direction):>13}"
        print(row)

    print("\n读法：轴A（关→开引用回查）看『幻觉引用用例率』↓、『忠实率』↑；轴B（naive→v7 拆解/多跳）看"
          "『答案要点覆盖/上下文召回』↑。拒答类（漏拒/误拒/拒答准确率）两臂红线相同应基本持平，作安全底线看，"
          "非轴B变量。✓=朝好方向、✗=反向。pp=百分点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
