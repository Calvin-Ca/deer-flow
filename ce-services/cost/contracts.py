"""组价 HITL 对外契约单一源（M1 契约线）—— pydantic 模型定义闸 payload / 会话响应的权威形状。

背景：此前同一契约存在三份手写镜像（``gates.py`` 裸 dict 手拼、前端 ``core/cost/types.ts`` 手抄、
``cost.py`` skill 参数约定），改字段要人肉同步三处、漂移只能等 Docker build 的 tsc 才发现。
本模块把形状收敛为**服务端唯一权威**：

  - ``gates.py`` / ``session.py`` 产出的 dict 必须能被这里的模型 ``model_validate``
    （契约测试 ``tests/test_contracts.py`` 把关，CI 挡漂移）；
  - ``tools/export_contracts_schema.py`` 导出 JSON Schema，供前端以 codegen 替换手写
    ``types.ts``（json-schema-to-typescript / openapi-typescript，落地在前端侧下一步）。

设计取向：**描述现状而非重构现状**——不改 gates/session 的产出方式（仍是 dict，轻、无运行时开销），
只用模型「验收」它们；``extra="allow"`` 容忍向后兼容的增量字段（如 list_gate 附加的
``confidence_band`` / ``context.hint``），必填字段缺失/类型错则立刻炸在测试里。
"""
from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _Envelope(BaseModel):
    """宽容基类：允许增量字段（向后兼容），只对声明字段做类型校验。"""

    model_config = ConfigDict(extra="allow")


# ── provenance（HITL 设计 §5.1：来源是字段不是散文）──────────────────────────

class Provenance(_Envelope):
    """原语信封的溯源块；所有字段可缺（best-effort），但出现即须类型正确。"""

    source_type: str | None = None
    source_ref: str | None = None
    confidence: float | None = None
    alternatives: list[dict[str, Any]] | None = None


# ── 三型闸 payload（§5.2/§5.3；前端 gates.tsx 只从这些字段渲染）──────────────

class ConfirmEvidence(_Envelope):
    source_type: str | None = None
    source_ref: str | None = None
    confidence: float | None = None


class ConfirmInterrupt(_Envelope):
    """确认型闸（编码/定额）：proposal + 依据 + 备选，用户 ✓/选备选/改。"""

    gate_type: Literal["confirm"]
    node: str
    title: str
    proposal: dict[str, Any]
    evidence: ConfirmEvidence
    alternatives: list[dict[str, Any]]
    actions: list[str]
    # list_gate 增量：置信分段标签（PRD §4.4 三段式）与低置信提示（context.hint）
    confidence_band: str | None = None
    context: dict[str, Any] | None = None


class InputField(_Envelope):
    """录入闸单字段；type 枚举与前端渲染器一一对应（enum→单选 / number→数字框…）。"""

    key: str
    type: Literal["enum", "text", "number", "month"]
    label: str
    options: list[str] | None = None
    default: Any | None = None
    required: bool | None = None


class InputInterrupt(_Envelope):
    """录入型闸（setup/缺价/费率/参数/缺定额补录）：用户填值或选来源。"""

    gate_type: Literal["input"]
    node: str
    title: str
    fields: list[InputField]
    context: dict[str, Any] | None = None


class ReviewInterrupt(_Envelope):
    """末尾复核闸（§13 总造价 review，始终暂停）。"""

    gate_type: Literal["review"]
    node: str
    title: str
    rollup: dict[str, Any]
    actions: list[str]


CostInterrupt = Union[ConfirmInterrupt, InputInterrupt, ReviewInterrupt]
# 判别式联合校验器：按 gate_type 分派到对应模型（供测试/调用方一步校验任意闸 payload）。
COST_INTERRUPT_ADAPTER: TypeAdapter[CostInterrupt] = TypeAdapter(
    Union[ConfirmInterrupt, InputInterrupt, ReviewInterrupt]
)


# ── 事件与会话响应（session._format → 前端 CostSessionResponse）─────────────

class CostEvent(_Envelope):
    """节点级 provenance 事件（依据卡数据源）；字段随节点类型增减，声明项保类型。"""

    step: str | None = None
    status: str | None = None
    provenance: Provenance | None = None
    result: dict[str, Any] | None = None
    paused: bool | None = None
    auto_pass: bool | None = None
    confidence: float | None = None
    tau: float | None = None
    detail: dict[str, Any] | None = None
    at: str | None = None


class CostSessionResponse(_Envelope):
    """/cost/session/{start,resume,state} 统一响应形状（前端 types.ts 的服务端权威）。"""

    task_id: str
    status: str
    interrupt: CostInterrupt | None = None
    events: list[CostEvent] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    overrides: list[dict[str, Any]] = Field(default_factory=list)
    audit_log: list[dict[str, Any]] = Field(default_factory=list)
    rates: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    rollup: dict[str, Any] | None = None


def validate_interrupt(payload: dict[str, Any]) -> CostInterrupt:
    """校验任意闸 payload → 判别式联合模型（非法即抛 ValidationError，供测试/防漂移）。"""
    return COST_INTERRUPT_ADAPTER.validate_python(payload)


def validate_session_response(payload: dict[str, Any]) -> CostSessionResponse:
    """校验会话响应 dict（session._format 产出）→ 模型；契约测试与将来 CI 的把关入口。"""
    return CostSessionResponse.model_validate(payload)
