#!/usr/bin/env python3
"""prerouter 对路由金标的能力分流验证（T-A1 收尾 + T9 口径策略回归）。

验三件事（金标 ``benchmark/routing_eval/agent_routing_eval.jsonl``）：
  1. **capability 分对没**：norm-qa→norm、cost-agent/cost-check→cost、price→price；
     cost-check 额外要求 ``needs_context=True``（FR-C 上下文信号识别）。
  2. **clarify 硬判**（金标已按 T9-1/T9-2 新语义更新）：cost 缺版本不反问（clarify=None）、
     缺特征才 feature；norm 缺口径 caliber。``session_sticky`` 组跳过——粘性是**会话级**行为
     （agent prompt 层），prerouter 单轮视角判 caliber 反问是正常的，不计错。
  3. **EH-03 出界**：``out_of_scope`` 组须识别出 ``out_of_scope_region``。

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
# 金标 agent → 期望 capability（cost-check 是 cost 能力 + needs_context 信号）
_AGENT_TO_CAP = {"norm-qa": "norm", "cost-agent": "cost", "cost-check": "cost", "price": "price"}
# clarify 期望：金标 expect_clarify（bool）→ prerouter 层面「是否应触发反问」。
# 会话粘性组跳过硬判（单轮视角无会话态）。
_CLARIFY_SKIP_GROUPS = {"session_sticky"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    rows = [json.loads(ln) for ln in _EVAL.read_text(encoding="utf-8").splitlines() if ln.strip()]
    passed = failed = 0
    for r in rows:
        d = route(r["query"])
        problems: list[str] = []

        # ① 能力分流
        want_cap = _AGENT_TO_CAP.get(r["agent"], r["agent"])
        if d.capability != want_cap:
            problems.append(f"cap={d.capability}≠{want_cap}")
        if r["agent"] == "cost-check" and not d.needs_context:
            problems.append("needs_context=False（FR-C 上下文信号漏判）")

        # ② clarify（新语义硬判；会话粘性组跳过）
        if r.get("group") not in _CLARIFY_SKIP_GROUPS:
            got_clarify = d.clarify is not None
            if got_clarify != bool(r.get("expect_clarify")):
                problems.append(f"clarify={d.clarify}≠expect {r.get('expect_clarify')}")

        # ③ EH-03 出界识别
        if r.get("group") == "out_of_scope" and not d.out_of_scope_region:
            problems.append("out_of_scope_region 未识别")

        ok = not problems
        passed += ok
        failed += not ok
        skip_tag = "（clarify 跳判：会话级）" if r.get("group") in _CLARIFY_SKIP_GROUPS else ""
        print(f"{'✓' if ok else '✗'} {r['id']:<3} cap={d.capability:<8} clarify={str(d.clarify):<8} "
              f"oos={str(d.out_of_scope_region):<5} {skip_tag}{'; '.join(problems)}")
        if not ok:
            print(f"      matched={d.matched}")

    total = passed + failed
    print(f"\n金标回归：{passed}/{total} = {passed/total:.0%}（能力分流 + T9 口径策略 clarify + EH-03 出界）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
