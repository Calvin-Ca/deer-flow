"""④ Resume Handler —— 校验动作 → 分派 handler 落值钉死 → 审计 → 续跑。"""
from __future__ import annotations

from typing import Any

from .helpers import _item_by_id, _reject_item_or_project
from .task_manager import HumanTaskManager
from .models import HumanAction, HumanResponse, HumanTask
from .registry import _HANDLERS


class ResumeHandler:
    """④ 落值回 state —— 校验动作 → 分派 handler 落值钉死 → 审计 → 续跑。"""

    def __init__(self, manager: HumanTaskManager):
        self._manager = manager

    def validate_action(self, action: HumanAction, allowed: list[HumanAction]) -> None:
        if action not in allowed:
            raise ValueError(f"动作 {action.value!r} 不在允许列表 {[a.value for a in allowed]}")

    def apply(self, state: dict[str, Any], task: HumanTask, response: HumanResponse) -> dict[str, Any]:
        """把人的响应落回 state（原地修改并返回）。

        - escalate：改派上级、任务保持 pending（不落业务值）；
        - reject：通用驳回（标构件放弃 / 候选皆不对），不进场景 handler；
        - respond/select/approve/edit：进对应 ``@response_handler`` 落值钉死。
        """
        self.validate_action(response.action, task.allowed_actions)
        item = _item_by_id(state, task.item_id)

        if response.action is HumanAction.ESCALATE:
            self._manager.escalate(state, task, response.comment)
            self._manager.audit(state, task, response)
            return state

        if response.action is HumanAction.REJECT:
            _reject_item_or_project(state, item, task)
            task.status = "rejected"
        else:
            handler = _HANDLERS.get(task.task_type)
            if handler is None:
                raise ValueError(f"未注册 task_type={task.task_type.value} 的 response_handler")
            handler(state, item, task, response)
            task.status = "resolved"

        task.human_response = response.model_dump()
        self._manager.audit(state, task, response)
        return state
