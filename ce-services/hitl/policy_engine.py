"""① HITL Policy Engine —— 判断"要不要停" + 产出 HumanTask。

只读 state、确定性、便宜、幂等（不取数、不调 LLM）——满足 resume 重跑不漂移。
遍历已注册 builder，命中即产任务；是"要不要人"这件事的唯一决策处（红线：不交 LLM）。
"""
from __future__ import annotations

from typing import Any

from .helpers import _cur_item, _resolved
from .models import HumanTask, STEP_ORDER, Scope, Step
from .registry import _BUILDERS, _STEP_TASKS


class HITLPolicyEngine:
    """① 判断要不要停 + 产出 HumanTask。"""

    def evaluate(self, state: dict[str, Any], *, step: Step | None = None,
                 scope: Scope | None = None) -> HumanTask | None:
        """返回**第一个**待办 HumanTask 或 None。

        - step 给定则只评估该大步骤；不给则按 4 步顺序全评。
        - scope 给定则只评估该作用域（如「计算」步逐件 ITEM 只出工程量、项目收尾 PROJECT 只出费率/税金/终审）。
        """
        steps = [step] if step else STEP_ORDER
        for s in steps:
            for tt in _STEP_TASKS[s]:
                fn, _, sc = _BUILDERS[tt]
                if scope is not None and sc is not scope:
                    continue
                if sc is Scope.PROJECT:
                    if _resolved(state, tt):
                        continue
                    task = fn(state, None)
                else:
                    task = fn(state, _cur_item(state))
                if task is not None:
                    return task
        return None

    def collect_pending(self, state: dict[str, Any]) -> list[HumanTask]:
        """整单模式：一次收集**所有** item / project 的待办任务（供两阶段批量补录）。"""
        tasks: list[HumanTask] = []
        for s in STEP_ORDER:
            for tt in _STEP_TASKS[s]:
                fn, _, sc = _BUILDERS[tt]
                if sc is Scope.PROJECT:
                    if not _resolved(state, tt):
                        t = fn(state, None)
                        if t is not None:
                            tasks.append(t)
                else:
                    for it in state.get("items") or []:
                        t = fn(state, it)
                        if t is not None:
                            tasks.append(t)
        return tasks
