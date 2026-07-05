#!/usr/bin/env python3
"""构件抽取金标评测（M2 §7 评测体系「Agent 原语」行 · listing v0）。

金标：``benchmark/listing_eval/gold.jsonl``——每行
``{id, text, expected[{name, must_include, quantity?|no_quantity?}], forbidden?[{contains, unless_contains}]}``。
判中：某 expected 被命中 ⟺ 抽取结果里存在一条 item.feature **包含其全部 must_include 关键词**；
  金标带 quantity 时命中条目的 quantity 还须相等（验「原文有量才抽量」）；
  金标带 no_quantity 时命中条目**不得带** quantity（验不编量/不串量——首跑实测 32b 会犯）。
负向断言 forbidden：某 item.feature 含全部 ``contains`` 词且不含任何 ``unless_contains`` 词 → 违规
  （治「特征拆成构件」，如砂浆单列成条）。违规计数独立于召回。
指标：**构件召回 ≥85%（G2 门）且 forbidden 违规 = 0** 才过；多抽数不罚只报（v0 宽进）。

运行：
  离线（只校验金标格式，CI 可空跑）：cd ce-services && uv run python -m tools.listing_eval
  连 LLM（真抽取算召回）：            cd ce-services && uv run python -m tools.listing_eval --llm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cost.listing import extract_components  # noqa: E402

_GOLD = (Path(__file__).resolve().parents[2] / "benchmark" / "listing_eval" / "gold.jsonl")


def _load() -> list[dict]:
    rows = [json.loads(ln) for ln in _GOLD.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for r in rows:  # 格式硬校验（离线模式的全部工作）：缺字段立刻炸，金标不带病入库
        assert r.get("id") and r.get("text") and isinstance(r.get("expected"), list), f"金标格式错: {r.get('id')}"
        for e in r["expected"]:
            assert e.get("name") and isinstance(e.get("must_include"), list) and e["must_include"], \
                f"金标 expected 格式错: {r['id']}/{e.get('name')}"
    return rows


def _hit(expected: dict, items: list[dict]) -> bool:
    for it in items:
        feature = it.get("feature") or ""
        if all(kw in feature for kw in expected["must_include"]):
            if "quantity" in expected and it.get("quantity") != float(expected["quantity"]):
                continue  # 关键词全中但量不对（漏抽/错抽 Q）→ 不算命中
            if expected.get("no_quantity") and it.get("quantity") is not None:
                continue  # 不该有量却带了量（编量/串量）→ 不算命中
            return True
    return False


def _violations(row: dict, items: list[dict]) -> list[str]:
    """forbidden 负向断言：返回违规条目描述列表（特征被拆成独立构件等）。"""
    out: list[str] = []
    for rule in row.get("forbidden") or []:
        for it in items:
            feature = it.get("feature") or ""
            if (all(kw in feature for kw in rule["contains"])
                    and not any(kw in feature for kw in rule.get("unless_contains") or [])):
                out.append(f"违规条目「{feature[:30]}」命中 forbidden{rule['contains']}")
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description="构件抽取金标评测（listing v0）")
    parser.add_argument("--llm", action="store_true", help="真调 32b 抽取算召回（缺省只校验金标格式）")
    args = parser.parse_args()

    rows = _load()
    total_expected = sum(len(r["expected"]) for r in rows)
    print(f"金标 {len(rows)} 份说明 / {total_expected} 个期望构件（格式校验通过）")
    if not args.llm:
        print("（未加 --llm：跳过真抽取；召回评测需服务器 32b）")
        return 0

    hits = extra = 0
    all_violations: list[str] = []
    times: list[float] = []
    for r in rows:
        t0 = time.perf_counter()
        env = extract_components(r["text"])
        times.append((time.perf_counter() - t0) * 1000)
        items = env["result"]["items"]
        row_hits = [e["name"] for e in r["expected"] if _hit(e, items)]
        miss = [e["name"] for e in r["expected"] if e["name"] not in row_hits]
        vios = _violations(r, items)
        all_violations += [f"{r['id']}: {v}" for v in vios]
        hits += len(row_hits)
        extra += max(0, len(items) - len(r["expected"]))
        flag = "✓" if not miss and not vios else "✗"
        print(f"{flag} {r['id']}: 命中 {len(row_hits)}/{len(r['expected'])}"
              f"{'  漏: ' + ','.join(miss) if miss else ''}"
              f"{'  违规: ' + str(len(vios)) if vios else ''}  抽出 {len(items)} 件"
              f"  status={env['status']}  {int(times[-1])}ms")
        for it in items:
            print(f"    · {it['feature'][:50]}{'  Q=' + str(it['quantity']) if 'quantity' in it else ''}")

    recall = hits / total_expected if total_expected else 0.0
    print(f"\n构件召回：{hits}/{total_expected} = {recall:.0%}（G2 门 ≥85%）；多抽 {extra} 件（v0 不罚只报）")
    if all_violations:
        print("forbidden 违规（必须清零）：")
        for v in all_violations:
            print(f"  ✗ {v}")
    print(f"延迟：均值 {sum(times) / len(times):.0f}ms / 最大 {max(times):.0f}ms")
    return 0 if recall >= 0.85 and not all_violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
