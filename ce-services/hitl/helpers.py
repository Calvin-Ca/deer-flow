"""四层类共用的底层状态原语（不对外）—— 取 item / 已决标记 / 溯源 / 驳回 / 建任务工厂。"""
from __future__ import annotations

from typing import Any

from .models import HITLReason, HumanAction, HumanTask, HumanTaskType, Scope, Step

# escalate 角色链（造价员 → 项目经理 → 总工）
_ROLE_CHAIN = ["estimator", "project_manager", "chief_engineer"]


def _cur_item(state: dict[str, Any]) -> dict[str, Any] | None:
    items = state.get("items") or []
    idx = state.get("current_item", 0)
    return items[idx] if 0 <= idx < len(items) else None


def _item_by_id(state: dict[str, Any], item_id: str | None) -> dict[str, Any] | None:
    if item_id is None:
        return None
    for it in state.get("items") or []:
        if str(it.get("item_id")) == str(item_id):
            return it
    return None


def _resolved(state: dict[str, Any], task_type: HumanTaskType) -> bool:
    return task_type.value in (state.get("resolved_tasks") or [])


def _mark_resolved(state: dict[str, Any], task_type: HumanTaskType) -> None:
    state.setdefault("resolved_tasks", [])
    if task_type.value not in state["resolved_tasks"]:
        state["resolved_tasks"].append(task_type.value)


def _user_prov(ref: str = "用户录入") -> dict[str, Any]:
    return {"source_type": "user_input", "source_ref": ref, "confidence": None}


def _next_role(current: str | None) -> str:
    if current is None:
        return _ROLE_CHAIN[1] if len(_ROLE_CHAIN) > 1 else _ROLE_CHAIN[0]
    try:
        i = _ROLE_CHAIN.index(current)
        return _ROLE_CHAIN[min(i + 1, len(_ROLE_CHAIN) - 1)]
    except ValueError:
        return _ROLE_CHAIN[-1]


def _reject_item_or_project(state: dict[str, Any], item: dict[str, Any] | None, task: HumanTask) -> None:
    """通用驳回：构件级 → 标该构件放弃组价（下游 no_pricing）；项目级 → 标终审驳回。"""
    if item is not None:
        item["status"] = "rejected"
        item["quota_basis"] = item.get("quota_basis")  # 不虚构，保持缺口
    else:
        state["final_approved"] = False
        _mark_resolved(state, task.task_type)


def _mk(
    state: dict[str, Any], item: dict[str, Any] | None, *,
    step: Step, task_type: HumanTaskType, reason: HITLReason, title: str,
    scope: Scope = Scope.ITEM, allowed_actions: list[HumanAction],
    **payload: Any,
) -> HumanTask:
    """构造 HumanTask 的便捷工厂（供 builder 用）。

    task_id 用**确定性**键（run:step:item:type）而非随机 uuid——resume 会重跑 builder，
    确定性 id 保证同一逻辑闸在重跑/重呈现时 id 稳定，便于前端幂等与审计对齐。
    """
    item_id = (item or {}).get("item_id") if scope is Scope.ITEM else None
    return HumanTask(
        task_id=f"{state.get('run_id', '')}:{step}:{item_id or '-'}:{task_type.value}",
        run_id=str(state.get("run_id", "")),
        item_id=item_id,
        step=step, task_type=task_type, reason=reason, scope=scope,
        title=title, allowed_actions=allowed_actions, **payload,
    )
