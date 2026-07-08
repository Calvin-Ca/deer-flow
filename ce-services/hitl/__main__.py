"""``python -m hitl`` —— 四层管道全流程自测（Policy→UI→Resume）。"""
from __future__ import annotations

from typing import Any

from . import HumanResponse, policy_engine, resume_handler, task_ui
from .registry import _BUILDERS, _HANDLERS


def _resolve(state: dict[str, Any], resp_dict: dict[str, Any]):
    task = policy_engine.evaluate(state)              # ① Policy Engine
    assert task is not None, "预期还有待办任务"
    clar = task_ui.render(task)                       # ③ UI 渲染（ask_clarification 载荷）
    resume_handler.apply(state, task, HumanResponse(**resp_dict))  # ④ Resume Handler
    print(f"  ✓ {task.task_type.value:20s} reason={task.reason.value:16s} "
          f"interaction={task.interaction:8s} type={clar['clarification_type']:22s} "
          f"action={resp_dict['action']}")
    return task


def main() -> None:
    st: dict[str, Any] = {
        "run_id": "demo-1", "current_item": 0,
        "items": [{
            "item_id": "it-1", "feature": "混凝土柱", "code_name": "现浇混凝土柱",
            "missing_features": [{"key": "混凝土强度等级", "label": "强度等级", "why": "定基价"},
                                 {"key": "柱类型", "label": "柱类型", "why": "选码"}],
        }],
    }
    print("四层管道全流程自测（Policy→UI→Resume）：")
    _resolve(st, {"action": "respond", "data": {"混凝土强度等级": "C30", "柱类型": "矩形柱"}})

    it = st["items"][0]
    it["code_env"] = {"status": "ok", "result": {"code": "010502001", "name": "矩形柱"},
                      "provenance": {"confidence": 0.68,
                                     "alternatives": [{"code": "010502002", "name": "构造柱", "score": 0.66}]}}
    _resolve(st, {"action": "select", "selected": "010502001", "comment": "实为矩形柱"})

    it["quota"] = {"envelope": {"status": "ok", "result": {"quotas": [
        {"子目号": "A5-1", "name": "矩形柱", "labor_cost": 180, "material_cost": 484.8, "machine_cost": 1.5},
        {"子目号": "A5-2", "name": "异形柱", "labor_cost": 210, "material_cost": 500, "machine_cost": 2},
    ]}}}
    _resolve(st, {"action": "select", "selected": "A5-1"})
    _resolve(st, {"action": "respond", "data": {"quantity": 8.5}})
    _resolve(st, {"action": "edit", "data": {"management_fee_rate": 15,
                  "profit_rate": 10, "risk_rate": 1, "fee_base": "labor_machine"}})
    _resolve(st, {"action": "edit", "data": {"tax_rate": 9, "measure_fee": 1200, "fee_levy": 800}})

    st["rollup"] = {"total": 8790.58, "pre_tax_total": 8064.75, "breakdown": {"分部分项费": 6064.75}}
    _resolve(st, {"action": "approve"})

    assert policy_engine.evaluate(st) is None, "应无剩余待办"
    print(f"\n全部解决。final_approved={st['final_approved']} rates={st['rates']}")
    print(f"审计条数={len(st['audit_log'])}  builders={len(_BUILDERS)}  handlers={len(_HANDLERS)}")


if __name__ == "__main__":
    main()
