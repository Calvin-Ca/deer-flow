"""Human-in-the-Loop 统一机制包 —— 把「人参与决策」建模成可持久化 / 可恢复 / 可审计的 HumanTask。

## 基础抽象（models.py）
- ``HITLReason``  为什么要人（5 类触发原因，决定交互形态/停闸策略/落值语义/批量策略）
- ``HumanAction`` 人要做什么（respond/select/approve/edit/reject/escalate）
- ``HumanTask``   一条人工任务（+ ``HumanResponse``/``HumanTaskType``/``Scope``）

## 架构：四层管道（一文件一层）
    ① policy_engine.py   HITLPolicyEngine  判断"要不要停"+产出 HumanTask（确定性、不碰 LLM）
              ↓
    ② manager.py         HumanTaskManager  生命周期/持久化/审计/批量聚合（寓于 state+checkpointer）
              ↓
    ③ ui.py              HumanTaskUI       HumanTask↔用户载荷（走 ask_clarification 通道）
              ↓
    ④ resume_handler.py  ResumeHandler     校验→落值钉死→审计→续跑

## 业务解释层（领域可插拔）
    registry.py     @task_builder / @response_handler 注册表
    cost_tasks.py   组价领域的 11 builder + 11 handler（import 即注册）
    —— 新增领域 = 加一个 xxx_tasks.py 注册一批 (builder, handler)，四层机制不动。

## 组装 / 兼容
    helpers.py         四层共用的底层状态原语
    pipeline.py        四层单例 + 向后兼容委托函数（evaluate_hitl / apply_human_response / …）
    langgraph_adapter  独立组价图路径的 hitl 节点

红线：Policy Engine 是**确定性代码**（不调 LLM，Qwen3-8B 不当编排器）；政策/策略数不给默认；
      算钱在 compute 侧、在所有输入闸之后（本包只管 HITL，不算钱——算钱见 ``cost.calc_tools``）。
"""
from .langgraph_adapter import apply_hitl_result_node, hitl_gate_node
from .models import (
    HITLReason,
    HumanAction,
    HumanResponse,
    HumanTask,
    HumanTaskType,
    Scope,
    interaction_of,
)
from .pipeline import (
    apply_human_response,
    collect_pending,
    evaluate_hitl,
    group_by_batch,
    parse_clarification_reply,
    policy_engine,
    resume_handler,
    task_manager,
    task_ui,
    to_clarification,
    validate_action,
)
from .policy_engine import HITLPolicyEngine
from .registry import response_handler, task_builder
from .resume_handler import ResumeHandler
from .task_manager import HumanTaskManager
from .task_ui import HumanTaskUI

__all__ = [
    # 基础抽象
    "HITLReason", "HumanAction", "HumanTaskType", "Scope", "HumanTask", "HumanResponse",
    "interaction_of",
    # 四层架构类 + 单例
    "HITLPolicyEngine", "HumanTaskManager", "HumanTaskUI", "ResumeHandler",
    "policy_engine", "task_manager", "task_ui", "resume_handler",
    # 业务注册
    "task_builder", "response_handler",
    # 委托函数（兼容）
    "evaluate_hitl", "collect_pending", "group_by_batch", "apply_human_response",
    "validate_action", "to_clarification", "parse_clarification_reply",
    "hitl_gate_node", "apply_hitl_result_node",
]
