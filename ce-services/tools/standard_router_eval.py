#!/usr/bin/env python3
"""standard_router 对路由金标的 family 选择验证（T-A2 收尾）。

只验 **family 选对没**（计量 50854 / 计价 50500 / 安装 50856），不验版本（版本属 §8 块1/T9-1）。
金标取 ``benchmark/routing_eval/agent_routing_eval.jsonl`` 中 ``agent=norm-qa`` 的用例，
gold 字段取其 family（``gb50854-2024`` → ``gb50854``；``A+B`` 双版取首个的 family）。

边界用例（gold=null，如 A6 gb50016 越界）跳过——family 分类器不负责越界拒答（那是校验闸 T-A3）。

运行：cd ce-services && uv run python -m tools.standard_router_eval
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from norm.standard_router import resolve_standard  # noqa: E402

_EVAL = (Path(__file__).resolve().parents[2]
         / "benchmark" / "routing_eval" / "agent_routing_eval.jsonl")


def _gold_family(gold: str) -> str:
    """gold 串 → family。``gb50854-2013+gb50854-2024`` 取首个；``gb50854-2024`` → ``gb50854``。"""
    first = gold.split("+")[0].strip()
    return first.split("-")[0]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    rows = [json.loads(ln) for ln in _EVAL.read_text(encoding="utf-8").splitlines() if ln.strip()]
    norm_rows = [r for r in rows if r.get("agent") == "norm-qa"]

    passed = failed = skipped = 0
    for r in norm_rows:
        gold = r.get("gold")
        if not gold:  # 边界/越界（gold=null）：family 分类器不负责，跳过
            skipped += 1
            print(f"⊘ {r['id']:<3} 跳过(越界/无gold)  {r['query'][:30]}")
            continue
        want = _gold_family(gold)
        res = resolve_standard(r["query"])  # 金标按「不带 hint」验确定性
        ok = res.family == want
        passed += ok
        failed += not ok
        flag = "✓" if ok else "✗"
        print(f"{flag} {r['id']:<3} got={res.family} want={want:<8} "
              f"intent={res.intent:<11} {r['query'][:30]}")
        if not ok:
            print(f"      matched={res.matched}  note={r.get('note', '')[:60]}")

    total = passed + failed
    rate = passed / total if total else 1.0
    print(f"\nfamily 选择正确率：{passed}/{total} = {rate:.0%}（跳过越界 {skipped} 例）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
