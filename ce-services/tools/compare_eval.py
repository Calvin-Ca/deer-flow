#!/usr/bin/env python3
"""多方案对比实验（M3 压轴）：P1 单次 / P2 单 agent 自查 / P3 多 agent 对抗复核，同金标同指标。

回答一个问题：**「自查 / 独立对抗复核能否提升构件抽取召回，代价多少」**——
这是「编排的确定性程度应匹配任务开放性」论点的实证数据（架构文档 §6 M3 验收物）。

三方案（refine 只做增量补抽，不删不改；weak_feature/quantity_doubt 类质疑留给评审表人审）：
  P1 单次抽取         extract                                   1 次 LLM
  P2 单 agent 自查     extract → refine(自查，无外部线索)          2 次 LLM（self-refine）
  P3 多 agent 对抗     extract → critic(独立角色) → refine(定向)   2~3 次 LLM
                       （critic 无 missing_item 质疑时跳过 refine——对抗视角还省调用）

指标：构件召回（金标 must_include+quantity 断言）/ forbidden 违规 / LLM 调用数 / 耗时。
金标含 3 份难例（L5~L7：从属句隐蔽构件/工序链/口语工序串）——常规例上三方案预期同分
（天花板），差异主要在难例上观察。

运行：
  离线（金标格式）：cd ce-services && uv run python -m tools.compare_eval
  真跑（32b）：      cd ce-services && uv run python -m tools.compare_eval --llm
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cost.critic import review_extraction  # noqa: E402
from cost.listing import extract_components, refine_missing  # noqa: E402
from tools.listing_eval import _hit, _load, _violations  # noqa: E402


def p1_single(text: str) -> tuple[list[dict[str, Any]], int]:
    """P1 基线：单次抽取。返回 (items, llm_calls)。"""
    env = extract_components(text)
    return env["result"]["items"], 1


def p2_self_refine(text: str) -> tuple[list[dict[str, Any]], int]:
    """P2 单 agent 自查：同一抽取角色对照原文自查漏项、增量补抽一轮。"""
    items = extract_components(text)["result"]["items"]
    if not items:
        return items, 1
    env = refine_missing(text, items)
    return env["result"]["items"], 2


def p3_critic_refine(text: str) -> tuple[list[dict[str, Any]], int]:
    """P3 多 agent 对抗：独立 Critic（不同角色 prompt）复核 → missing_item 质疑作定向线索补抽。

    对抗分工的两个假设（实验待验）：① 独立视角比自查更能发现「自己看不见的漏」；
    ② 无质疑时跳过补抽——对抗复核同时是「要不要再花一调」的守门员。
    """
    items = extract_components(text)["result"]["items"]
    if not items:
        return items, 1
    critic_env = review_extraction(text, items)
    hints = [f"{f['detail']}（原文：{f['source_text']}）"
             for f in (critic_env.get("result") or {}).get("findings") or []
             if f.get("type") == "missing_item"]
    if not hints:
        return items, 2  # Critic 认为无漏项 → 不补抽，省一调
    env = refine_missing(text, items, hints=hints)
    return env["result"]["items"], 3


PIPELINES = [("P1 单次", p1_single), ("P2 自查", p2_self_refine), ("P3 对抗", p3_critic_refine)]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="P1/P2/P3 构件抽取对比实验")
    parser.add_argument("--llm", action="store_true", help="真调 32b（缺省只校验金标格式）")
    args = parser.parse_args()

    rows = _load()
    total = sum(len(r["expected"]) for r in rows)
    hard = [r["id"] for r in rows if "难例" in r.get("note", "")]
    print(f"金标 {len(rows)} 份 / {total} 个期望构件（难例：{'、'.join(hard) or '无'}）")
    if not args.llm:
        print("（未加 --llm：跳过真跑）")
        return 0

    summary: list[dict[str, Any]] = []
    for name, fn in PIPELINES:
        hits = vios = calls = 0
        hard_hits = hard_total = 0
        elapsed = 0.0
        print(f"\n── {name} ──")
        for r in rows:
            t0 = time.perf_counter()
            items, n_calls = fn(r["text"])
            dt = (time.perf_counter() - t0) * 1000
            elapsed += dt
            calls += n_calls
            row_hits = [e["name"] for e in r["expected"] if _hit(e, items)]
            miss = [e["name"] for e in r["expected"] if e["name"] not in row_hits]
            row_vios = _violations(r, items)
            hits += len(row_hits)
            vios += len(row_vios)
            if r["id"] in hard:
                hard_hits += len(row_hits)
                hard_total += len(r["expected"])
            flag = "✓" if not miss and not row_vios else "✗"
            print(f"  {flag} {r['id']}: {len(row_hits)}/{len(r['expected'])}"
                  f"{'  漏: ' + ','.join(miss) if miss else ''}"
                  f"{'  违规: ' + str(len(row_vios)) if row_vios else ''}"
                  f"  抽出 {len(items)} 件  调用 {n_calls}  {int(dt)}ms")
        summary.append({"name": name, "recall": hits / total, "hard_recall":
                        (hard_hits / hard_total) if hard_total else None,
                        "vios": vios, "calls": calls, "ms": elapsed})

    print("\n══ 对比表（面试即用）══")
    print(f"{'方案':<10}{'总召回':>8}{'难例召回':>10}{'违规':>6}{'LLM调用':>9}{'总耗时':>10}")
    for s in summary:
        hard_str = f"{s['hard_recall']:.0%}" if s["hard_recall"] is not None else "—"
        print(f"{s['name']:<10}{s['recall']:>8.0%}{hard_str:>10}{s['vios']:>6}"
              f"{s['calls']:>9}{s['ms']:>9.0f}ms")
    print("\n判读指引：难例召回差值 = 自查/对抗的真实增益；调用与耗时 = 为增益付的价。"
          "若 P2≈P3 则「独立视角」溢价不成立（single-agent-refine 够用）；若 P3>P2 则对抗分工有实证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
