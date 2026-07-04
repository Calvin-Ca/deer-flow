#!/usr/bin/env python3
"""路由线回归收编（M1）：把 5 套既有自测/金标评测收编为 pytest 可发现的 test_*。

与 ``tools/test_backlog.py`` 同约定：**无 pytest 硬依赖**——``__main__`` 直跑亦可
（本地无 pytest：``python tests/test_routing_regression.py``；服务器 CI：``uv run pytest tests/ -q``）。

每个 test_* 只断言对应套件退出码 == 0：套件内部逐条打印明细（✓/✗ + 期望值），
失败时 stdout 已含定位信息，无需在此重复展开。覆盖：
  - routing.prerouter._selftest        —— 能力分流/clarify/出界/置信/override（47 例）
  - routing.intent_fallback._selftest  —— 混合路由升级门/fail-safe/域外（8 例）
  - routing.orchestrator._selftest     —— 编排回路/HITL 点火/域外直答（12 例，注入 stub 零服务零 LLM）
  - tools.prerouter_eval               —— 路由金标回归（benchmark/routing_eval/agent_routing_eval.jsonl）
  - tools.intent_fallback_eval（离线） —— 确定性升级门 + 强信号控制组（--llm 兜底准确率需服务器）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_prerouter_selftest() -> None:
    from routing.prerouter import _selftest

    assert _selftest() == 0


def test_intent_fallback_selftest() -> None:
    from routing.intent_fallback import _selftest

    assert _selftest() == 0


def test_orchestrator_selftest() -> None:
    from routing.orchestrator import _selftest

    assert _selftest() == 0


def test_prerouter_gold() -> None:
    from tools.prerouter_eval import main

    assert main() == 0


def test_intent_fallback_gate_offline() -> None:
    """离线模式（不带 --llm）：argparse 会吃 sys.argv，跑 pytest 时临时清参避免解析报错。"""
    from tools.intent_fallback_eval import main

    argv = sys.argv
    sys.argv = ["intent_fallback_eval"]
    try:
        assert main() == 0
    finally:
        sys.argv = argv


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    failed = 0
    for _name in sorted(k for k in dir() if k.startswith("test_")):
        try:
            globals()[_name]()
            print(f"\n══ ✓ {_name} ══\n")
        except AssertionError:
            failed += 1
            print(f"\n══ ✗ {_name} ══\n")
    print(f"路由回归收编：{'全绿' if not failed else f'{failed} 套失败'}")
    sys.exit(1 if failed else 0)
