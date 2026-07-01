#!/usr/bin/env python3
"""prerouter 对路由金标的能力分流验证（T-A1 收尾）。

验 **capability 分对没**（norm/cost/price/compound ↔ gold ``agent``）。金标
``benchmark/routing_eval/agent_routing_eval.jsonl``：``agent`` ∈ {norm-qa, cost-agent}。
映射 capability: norm→norm-qa、cost→cost-agent、price/compound 暂无金标用例。

**clarify 按新设计语义报告**（不作硬判分）：金标 ``expect_clarify`` 是**旧·基于版本**口径
（cost 缺版本=要反问）；§8 块1 已收窄为「cost 仅特征缺反问、版本缺默认不问」，故本脚本对 cost
no_version 用例的 clarify 会与旧 expect 分歧——这是**设计演进的正确分歧**，逐条打印供人核，不计错。

运行：cd ce-services && uv run python -m tools.prerouter_eval
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from routing.prerouter import route  # noqa: E402

_EVAL = (Path(__file__).resolve().parents[2]
         / "benchmark" / "routing_eval" / "agent_routing_eval.jsonl")
_CAP_TO_AGENT = {"norm": "norm-qa", "cost": "cost-agent"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    rows = [json.loads(ln) for ln in _EVAL.read_text(encoding="utf-8").splitlines() if ln.strip()]
    passed = failed = 0
    clarify_div = 0
    for r in rows:
        d = route(r["query"])
        got_agent = _CAP_TO_AGENT.get(d.capability, d.capability)
        ok = got_agent == r["agent"]
        passed += ok
        failed += not ok
        flag = "✓" if ok else "✗"
        # clarify 新设计语义 vs 旧 expect（仅打印，不判分）
        new_clarify = d.clarify is not None
        old_expect = bool(r.get("expect_clarify"))
        div = "" if new_clarify == old_expect else f"  [clarify分歧 新={d.clarify} 旧expect={old_expect}]"
        if div:
            clarify_div += 1
        print(f"{flag} {r['id']:<3} cap={d.capability:<8}→{got_agent:<10} want={r['agent']:<10}"
              f" clarify={str(d.clarify):<8}{div}")
        if not ok:
            print(f"      matched={d.matched}")

    total = passed + failed
    print(f"\n能力分流正确率：{passed}/{total} = {passed/total:.0%}"
          f"；clarify 与旧 expect 分歧 {clarify_div} 例（§8块1 设计演进，非错）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
