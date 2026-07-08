"""HITL 基础抽象 —— HITLReason / HumanAction / HumanTask（+ HumanResponse / 辅助枚举 / 常量）。

- ``HITLReason``  为什么要人（5 类触发原因，决定交互形态/停闸策略/落值语义/批量策略）
- ``HumanAction`` 人要做什么（respond/select/approve/edit/reject/escalate 六个标准动作）
- ``HumanTask``   一条人工任务（覆盖所有停人情形，差异只在 payload：candidates/fields/context）
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HITLReason(str, Enum):
    """**为什么需要人**（统一触发原因，决定其余一切）。"""

    MISSING_INFO = "missing_info"            # 信息不完整：缺继续判断的关键字段
    LOW_CONFIDENCE = "low_confidence"        # 判断不确定：多候选接近 / 置信低
    RULE_CONFIRMATION = "rule_confirmation"  # 规则需确认：可解释但需业务人员拍板（换算/费率）
    ANOMALY_REVIEW = "anomaly_review"        # 结果异常：偏离经验范围
    RISKY_ACTION = "risky_action"            # 动作有风险：产生外部影响（写报价表/终稿提交）


class HumanAction(str, Enum):
    """**人的标准动作**（不管场景多复杂，输出都收敛到这几类）。"""

    RESPOND = "respond"    # 补充信息
    SELECT = "select"      # 从候选中选择
    APPROVE = "approve"    # 同意继续
    EDIT = "edit"          # 修改后继续
    REJECT = "reject"      # 驳回当前结果
    ESCALATE = "escalate"  # 转更高权限的人


class HumanTaskType(str, Enum):
    """场景类型（挂在 4 大步骤下）。"""

    # —— 清单匹配 ——
    FILL_MISSING_INFO = "fill_missing_info"
    SELECT_CODE = "select_code"
    # —— 套定额 ——
    SELECT_QUOTA = "select_quota"
    MANUAL_QUOTA_BASIS = "manual_quota_basis"
    CONFIRM_CONVERSION = "confirm_conversion"
    # —— 询价 ——
    FILL_MISSING_PRICE = "fill_missing_price"
    # —— 计算 ——
    FILL_QUANTITY = "fill_quantity"
    SET_RATES = "set_rates"
    SET_PROJECT_PARAMS = "set_project_params"
    REVIEW_ANOMALY = "review_anomaly"
    FINAL_APPROVAL = "final_approval"


class Scope(str, Enum):
    ITEM = "item"        # 逐构件
    PROJECT = "project"  # 全单一次（会话粘性）


Step = Literal["bill_match", "quota", "pricing", "compute"]
STEP_ORDER: list[Step] = ["bill_match", "quota", "pricing", "compute"]

# 交互形态由触发原因派生（前端一套组件按 interaction 渲染）
_INTERACTION: dict[HITLReason, str] = {
    HITLReason.MISSING_INFO: "input",
    HITLReason.LOW_CONFIDENCE: "confirm",
    HITLReason.RULE_CONFIRMATION: "confirm",
    HITLReason.ANOMALY_REVIEW: "review",
    HITLReason.RISKY_ACTION: "review",
}


def interaction_of(reason: HITLReason) -> str:
    """触发原因 → 交互形态（confirm/input/review）。"""
    return _INTERACTION[reason]


class HumanTask(BaseModel):
    """一条人工任务 —— 覆盖所有 HITL 情形，差异只体现在 payload（context/candidates/fields）。"""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    task_id: str
    run_id: str
    item_id: str | None = None

    step: Step
    task_type: HumanTaskType
    reason: HITLReason
    scope: Scope = Scope.ITEM
    batch_key: str | None = None  # 整单聚合键：同 batch_key 的 pending 可一次批量处理

    title: str
    description: str = ""

    context: dict[str, Any] = Field(default_factory=dict)   # 展示上下文
    candidates: list[dict[str, Any]] = Field(default_factory=list)  # confirm 型：候选
    fields: list[dict[str, Any]] = Field(default_factory=list)      # input 型：待填字段
    suggested_answer: dict[str, Any] | None = None                 # 建议值（可预填）

    allowed_actions: list[HumanAction]
    assignee_role: str | None = None  # escalate 目标角色

    status: Literal["pending", "resolved", "rejected", "expired"] = "pending"
    human_response: dict[str, Any] | None = None

    @property
    def interaction(self) -> str:
        return interaction_of(self.reason)


class HumanResponse(BaseModel):
    """人的响应 —— 动作 + 附带数据。"""

    model_config = ConfigDict(extra="forbid")

    action: HumanAction
    data: dict[str, Any] | None = None   # respond / edit：字段值
    selected: str | None = None          # select：选中的编码/子目号
    comment: str | None = None
