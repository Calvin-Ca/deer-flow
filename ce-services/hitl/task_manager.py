"""② Human Task Manager —— 任务生命周期 / 持久化 / 审计 / 批量聚合。

当前实现「寓于 state + checkpointer」（单会话足够）：
- 当前挂起工单存 ``state.pending_human_task``；已办项目级任务存 ``state.resolved_tasks``；
- 审计存 ``state.audit_log``；上报轨迹存 ``state.escalations``。
要跨会话任务看板/多人分派时，把本类换成落 human_task 表 / Store 的实现，四层其余不动。
"""
from __future__ import annotations

from typing import Any

from .helpers import _mark_resolved, _next_role, _resolved
from .models import HumanResponse, HumanTask, HumanTaskType


class HumanTaskManager:
    """② 任务生命周期 / 持久化 / 审计 / 批量聚合。"""

    def pending(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """当前挂起的工单（4 步图路径写在 state.pending_human_task）。"""
        return state.get("pending_human_task")

    def is_resolved(self, state: dict[str, Any], task_type: HumanTaskType) -> bool:
        return _resolved(state, task_type)

    def mark_resolved(self, state: dict[str, Any], task_type: HumanTaskType) -> None:
        _mark_resolved(state, task_type)

    def group_by_batch(self, tasks: list[HumanTask]) -> dict[str, list[HumanTask]]:
        """按 batch_key 聚合待办（同类一次批量处理）；无 batch_key 的各自成组（task_id 为键）。"""
        groups: dict[str, list[HumanTask]] = {}
        for t in tasks:
            key = t.batch_key or t.task_id
            groups.setdefault(key, []).append(t)
        return groups

    def escalate(self, state: dict[str, Any], task: HumanTask, comment: str | None = None) -> None:
        """改派上级、任务保持 pending（不落业务值），记上报轨迹。"""
        task.assignee_role = _next_role(task.assignee_role)
        task.status = "pending"
        state.setdefault("escalations", []).append({
            "task_id": task.task_id, "to": task.assignee_role,
            "item": task.item_id, "comment": comment,
        })

    def audit(self, state: dict[str, Any], task: HumanTask, resp: HumanResponse) -> None:
        state.setdefault("audit_log", []).append({
            "gate": task.task_type.value, "action": resp.action.value, "by": "user",
            "item": task.item_id, "reason": task.reason.value, "comment": resp.comment,
        })
