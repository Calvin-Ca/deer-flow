"""四层管道组装 —— 单例（Policy→Manager→UI→Resume）+ 向后兼容的委托函数。

import 本模块会连带 import ``cost_tasks``，触发组价 builder/handler 注册。
其它领域接入时，在这里再 ``from . import xxx_tasks`` 即可。
"""
from __future__ import annotations

from typing import Any

from . import cost_tasks  # noqa: F401  —— import 触发组价 builder/handler 注册
from .models import HumanAction, HumanResponse, HumanTask, Scope, Step
from .policy_engine import HITLPolicyEngine
from .resume_handler import ResumeHandler
from .task_manager import HumanTaskManager
from .task_ui import HumanTaskUI

# —— 四层单例（组成管道）——
policy_engine = HITLPolicyEngine()
task_manager = HumanTaskManager()
task_ui = HumanTaskUI()
resume_handler = ResumeHandler(task_manager)


# —— 向后兼容的模块级委托函数（graph_4step / 测试 / 便捷调用沿用）——
def evaluate_hitl(state: dict[str, Any], *, step: Step | None = None,
                  scope: Scope | None = None) -> HumanTask | None:
    return policy_engine.evaluate(state, step=step, scope=scope)


def collect_pending(state: dict[str, Any]) -> list[HumanTask]:
    return policy_engine.collect_pending(state)


def group_by_batch(tasks: list[HumanTask]) -> dict[str, list[HumanTask]]:
    return task_manager.group_by_batch(tasks)


def apply_human_response(state: dict[str, Any], task: HumanTask,
                         response: HumanResponse) -> dict[str, Any]:
    return resume_handler.apply(state, task, response)


def validate_action(action: HumanAction, allowed: list[HumanAction]) -> None:
    resume_handler.validate_action(action, allowed)


def to_clarification(task: HumanTask) -> dict[str, Any]:
    return task_ui.render(task)


def parse_clarification_reply(task: HumanTask, reply: str) -> HumanResponse:
    return task_ui.parse(task, reply)
