#!/usr/bin/env python3
"""Critic 复核金标评测（M3 管线④ · v0）。

金标：``benchmark/critic_eval/gold.jsonl``——每行
``{id, text, draft_items, expected_findings[{type, must_include, item_index?}]}``。
判中：某 expected 命中 ⟺ 存在 finding 同 type、且 detail+source_text 合并文本含全部
must_include 词、且 item_index 相符（金标给了才比）。
负向断言两层（Critic 误报比漏报更伤信任）：
  - 负向样本：expected 为空的行产出任何质疑 → false_positive；
  - 行级 forbidden ``[{type?, contains}]``：命中的质疑 → false_positive（07-05 首跑实测：
    32b 把已在草表的砌块墙判成漏项——正向样本的错误质疑曾是盲区，与 listing 金标同款教训）。
指标：**质疑查全率 ≥80% 且 false_positive 合计 = 0** 才过。

运行：
  离线（金标格式校验）：cd ce-services && uv run python -m tools.critic_eval
  连 LLM（真复核）：    cd ce-services && uv run python -m tools.critic_eval --llm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cost.critic import FINDING_TYPES, review_extraction  # noqa: E402

_GOLD = (Path(__file__).resolve().parents[2] / "benchmark" / "critic_eval" / "gold.jsonl")


def _load() -> list[dict]:
    rows = [json.loads(ln) for ln in _GOLD.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for r in rows:
        assert r.get("id") and r.get("text") and isinstance(r.get("draft_items"), list), \
            f"金标格式错: {r.get('id')}"
        for e in r.get("expected_findings", []):
            assert e.get("type") in FINDING_TYPES and e.get("must_include"), \
                f"金标 expected 格式错: {r['id']}"
        for rule in r.get("forbidden") or []:
            assert rule.get("contains"), f"金标 forbidden 格式错: {r['id']}"
    return rows


def _forbidden_hits(row: dict, findings: list[dict]) -> list[str]:
    """行级 forbidden：type 匹配（缺省不限）且 detail+source 含全部 contains 词 → 违规描述列表。"""
    out: list[str] = []
    for rule in row.get("forbidden") or []:
        for f in findings:
            if rule.get("type") and f.get("type") != rule["type"]:
                continue
            blob = f"{f.get('detail', '')} {f.get('source_text', '')}"
            if all(kw in blob for kw in rule["contains"]):
                out.append(f"[{f.get('type')}] {f.get('detail', '')[:40]}")
    return out


def _hit(expected: dict, findings: list[dict]) -> bool:
    for f in findings:
        if f.get("type") != expected["type"]:
            continue
        blob = f"{f.get('detail', '')} {f.get('source_text', '')}"
        if not all(kw in blob for kw in expected["must_include"]):
            continue
        if "item_index" in expected and f.get("item_index") != expected["item_index"]:
            continue
        return True
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="Critic 复核金标评测")
    parser.add_argument("--llm", action="store_true", help="真调 32b 复核（缺省只校验金标格式）")
    args = parser.parse_args()

    rows = _load()
    total = sum(len(r.get("expected_findings", [])) for r in rows)
    print(f"金标 {len(rows)} 份 / {total} 条期望质疑（格式校验通过）")
    if not args.llm:
        print("（未加 --llm：跳过真复核）")
        return 0

    hits = false_pos = 0
    times: list[float] = []
    for r in rows:
        t0 = time.perf_counter()
        env = review_extraction(r["text"], r["draft_items"])
        times.append((time.perf_counter() - t0) * 1000)
        findings = env["result"]["findings"]
        expected = r.get("expected_findings", [])
        row_hits = [e for e in expected if _hit(e, findings)]
        miss = [f"{e['type']}:{','.join(e['must_include'])}" for e in expected if e not in row_hits]
        fp = len(findings) if not expected else 0  # 负向样本产出任何质疑=误报
        forb = _forbidden_hits(r, findings)        # 正向样本的已知错误质疑模式（假漏项等）
        fp += len(forb)
        hits += len(row_hits)
        false_pos += fp
        flag = "✓" if not miss and not fp else "✗"
        if forb:
            print(f"  ✗ forbidden 命中: {'; '.join(forb)}")
        print(f"{flag} {r['id']}: 命中 {len(row_hits)}/{len(expected)}"
              f"{'  漏: ' + ';'.join(miss) if miss else ''}"
              f"{'  误报: ' + str(fp) if fp else ''}  产出 {len(findings)} 条"
              f"  status={env['status']}  {int(times[-1])}ms")
        for f in findings:
            print(f"    · [{f['type']}]{'#' + str(f['item_index']) if 'item_index' in f else ''} "
                  f"{f['detail'][:60]}")

    recall = hits / total if total else 1.0
    print(f"\n质疑查全率：{hits}/{total} = {recall:.0%}（门 ≥80%）；负向样本误报 {false_pos}（须=0）")
    print(f"延迟：均值 {sum(times) / len(times):.0f}ms / 最大 {max(times):.0f}ms")
    return 0 if recall >= 0.80 and false_pos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
