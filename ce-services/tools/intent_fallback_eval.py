#!/usr/bin/env python3
"""意图混合路由·兜底层评测（AGENT_TODO M1 路由线：确定性 + LLM 兜底）。

评三件事（金标 ``benchmark/routing_eval/intent_fallback_eval.jsonl``，难例/含糊/口语变体集）：
  1. **升级门（确定性，离线）**：每条 ``route().route_confidence`` 是否等于金标 ``expect_confidence``
     ——即「该升级的（low）升、不该升的（high）不动」。据此算**升级率**（low 占比）。
  2. **强信号控制组直配正确率（确定性，离线）**：high 组 ``route().capability`` 应等于金标能力
     （证强信号零延迟直配没判错）。
  3. **LLM 兜底准确率 + 延迟（需 ``--llm``，真调 32b）**：对 low 组逐条调 ``classify_intent``，
     与金标能力比→准确率；计每次调用耗时→均值/P95（对齐 FR-K ≤3s NFR）。无 ``--llm`` 时跳过本项
     （离线只验确定性门，CI 可空跑）。

运行：
  离线（只验确定性升级门 + 升级率）：cd ce-services && uv run python -m tools.intent_fallback_eval
  连 LLM（加验兜底准确率 + 延迟）：      cd ce-services && uv run python -m tools.intent_fallback_eval --llm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from routing.intent_fallback import classify_intent  # noqa: E402
from routing.prerouter import route  # noqa: E402

_EVAL = (Path(__file__).resolve().parents[2]
         / "benchmark" / "routing_eval" / "intent_fallback_eval.jsonl")


def _p95(xs: list[float]) -> float:
    """小样本 P95（最近秩，向上取整）；空列表返回 nan。"""
    if not xs:
        return float("nan")
    s = sorted(xs)
    idx = min(len(s) - 1, int(round(0.95 * (len(s) - 1))))
    return s[idx]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="意图混合路由·兜底层评测")
    parser.add_argument("--llm", action="store_true",
                        help="真调 32b 兜底分类器，加验兜底准确率 + 延迟（需 LLM 可达）")
    args = parser.parse_args()

    rows = [json.loads(ln) for ln in _EVAL.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ── ①+② 确定性升级门（离线，全量）──
    gate_pass = gate_fail = 0
    ctrl_pass = ctrl_fail = 0
    low_rows: list[dict] = []
    for r in rows:
        d = route(r["query"])
        gate_ok = d.route_confidence == r["expect_confidence"]
        gate_pass += gate_ok
        gate_fail += not gate_ok
        if r["expect_confidence"] == "low":
            low_rows.append(r)
        else:  # high 控制组：确定性直配能力应对
            cok = d.capability == r["gold_capability"]
            ctrl_pass += cok
            ctrl_fail += not cok
        tag = "" if gate_ok else f"  ✗门 got={d.route_confidence}≠{r['expect_confidence']}"
        print(f"{'✓' if gate_ok else '✗'} {r['id']:<5} conf={d.route_confidence:<4} "
              f"cap={d.capability:<8} exp={r['expect_confidence']:<4}/{r['gold_capability']:<8}{tag}")

    escalation_rate = len(low_rows) / len(rows) if rows else float("nan")
    print(f"\n── 确定性门（离线）──")
    print(f"升级门正确率：{gate_pass}/{gate_pass + gate_fail}"
          f"（该 low 的 low、该 high 的 high）")
    print(f"强信号控制组直配正确率：{ctrl_pass}/{ctrl_pass + ctrl_fail}")
    print(f"升级率（low 占比）：{escalation_rate:.0%}（{len(low_rows)}/{len(rows)} 条走 LLM 兜底）")

    # ── ③ LLM 兜底准确率 + 延迟（--llm）──
    llm_fail = 0
    if args.llm and low_rows:
        print(f"\n── LLM 兜底（32b，{len(low_rows)} 条 low）──")
        acc_pass = 0
        lats: list[float] = []
        for r in low_rows:
            t0 = time.perf_counter()
            cap = classify_intent(r["query"])
            dt = time.perf_counter() - t0
            lats.append(dt)
            ok = cap == r["gold_capability"]
            acc_pass += ok
            if cap is None:
                llm_fail += 1
            print(f"{'✓' if ok else '✗'} {r['id']:<5} llm={str(cap):<8} gold={r['gold_capability']:<8} "
                  f"{dt * 1000:6.0f}ms  {r['query'][:22]}")
        print(f"\nLLM 兜底准确率：{acc_pass}/{len(low_rows)} = {acc_pass / len(low_rows):.0%}"
              f"（fail-safe None {llm_fail} 次）")
        print(f"延迟：均值 {sum(lats) / len(lats) * 1000:.0f}ms / P95 {_p95(lats) * 1000:.0f}ms"
              f"（对齐 FR-K ≤3s NFR）")
    elif args.llm:
        print("\n（无 low 用例，跳过 LLM 兜底评测）")
    else:
        print("\n（未加 --llm：跳过 LLM 兜底准确率/延迟；离线只验确定性升级门）")

    # 退出码：确定性门有错即失败；LLM 准确率/延迟只报数不作硬门（金标口径待 benchmark 定）
    return 1 if (gate_fail or ctrl_fail) else 0


if __name__ == "__main__":
    raise SystemExit(main())
