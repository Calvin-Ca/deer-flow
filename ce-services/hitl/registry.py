"""业务解释层挂载点 —— builder / handler 注册表 + 装饰器。

领域（组价 / 合规 / 风险审批…）通过 ``@task_builder`` / ``@response_handler`` 把自己的
「探测+建任务」和「落值」注册进来；四层机制（Policy/Manager/UI/Resume）读这张表运作，与领域解耦。
"""
from __future__ import annotations

from typing import Any, Callable

from .models import HumanResponse, HumanTask, HumanTaskType, Scope, STEP_ORDER, Step

# builder: (state, item|None) -> HumanTask | None   None=自动过/已决/不适用
Builder = Callable[[dict[str, Any], "dict[str, Any] | None"], "HumanTask | None"]
# handler: (state, item|None, task, response) -> None（原地写回 state）
Handler = Callable[[dict[str, Any], "dict[str, Any] | None", HumanTask, HumanResponse], None]

_BUILDERS: dict[HumanTaskType, tuple[Builder, Step, Scope]] = {}
_HANDLERS: dict[HumanTaskType, Handler] = {}
_STEP_TASKS: dict[Step, list[HumanTaskType]] = {s: [] for s in STEP_ORDER}


def task_builder(task_type: HumanTaskType, step: Step, scope: Scope = Scope.ITEM):
    """注册一个场景 builder（探测是否需要人 + 构造 HumanTask）——供 ①HITLPolicyEngine 消费。"""

    def deco(fn: Builder) -> Builder:
        _BUILDERS[task_type] = (fn, step, scope)
        _STEP_TASKS[step].append(task_type)
        return fn

    return deco


def response_handler(task_type: HumanTaskType):
    """注册一个场景 handler（把人的动作落回 state）——供 ④ResumeHandler 分派。"""

    def deco(fn: Handler) -> Handler:
        _HANDLERS[task_type] = fn
        return fn

    return deco
