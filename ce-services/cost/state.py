"""HITL 任务状态文档 —— langgraph 图的 state schema（§5.4）+ 纯函数 helper。

设计 §5.4：按 ``task_id`` 持久化的状态文档，承载已确认编码 / override / 选定价 / 审计轨迹，
经 langgraph checkpointer 落盘、HITL 可跨会话恢复（原则 4）。

state 的列表通道（events / audit_log / overrides）用 ``operator.add`` reducer：节点只返回**新增**条目、
框架累加，避免节点间互相覆盖历史。``items`` 不加 reducer（本期单构件，整体覆盖即可）。

本模块 helper 全为**纯函数**（不依赖 langgraph / 服务 / LLM），可本地直接单测——
钉值（lock_value）/ 审计（audit_entry）/ override 记录都是确定性数据变换（原则 3「override 优于重算」）。
"""
from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

# 两级汇总分组默认名（单构件/未分组时归一到此，退化为单组树；见 pricing.rollup_hierarchy）。
# 造价层级：单项工程 > 单位工程 > 分部分项 > 清单项——item 缺分组标签时落此默认，整单仍出层级结构。
DEFAULT_SINGLE_WORK = "默认单项工程"
DEFAULT_UNIT_WORK = "默认单位工程"


class CostTaskState(TypedDict, total=False):
    """组价 HITL 任务状态（§5.4）。

    字段：
        task_id —— 任务唯一标识（thread_id）。
        spec_version / region / period —— 国标版本 / 地区 / 计价期号（setup 闸采集）。
        price_source —— 信息价来源策略（local / online / manual，setup 一次性选定，§7）。
        rates —— 综合单价费率块（管理费/利润/风险/取费基数，§8 费率闸采集；给定则末步算综合单价）。
        params —— 项目级费用块（措施/其他/规费/税金率，§10⑪/§12 参数闸采集）。
        rollup —— 末尾汇总结果（§13：分部分项 + 措施 + 其他 + 规费 + 税金 → 总造价）。
        feature —— 入口首个构件/做法描述（单构件主描述；多构件各描述在 items[].feature）。
        current_item —— 多构件外层循环的当前在办构件下标（per-item 闸只动 items[current_item]；advance 推进）。
        needs_rematch —— 特征澄清后是否需回 list_match 重匹配重门控的回环标记（list_match 起始清零）。
        items —— §5.4 items：每项含 feature/clarify_rounds/code/missing_features/quota/quota_basis/materials/quantity/unit_price，
          确认后 locked（clarify_rounds=该件澄清回环轮次，达 MAX_CLARIFY_ROUNDS 不再反问；missing_features=待澄清缺失特征；
          quantity=工程量 Q，用户录入，综合合价 = 综合单价 × Q，§2 步骤 9）。多构件各件独立计澄清预算。
          可选 single_work/unit_work —— 两级汇总分组标签（单项工程/单位工程名，缺则归 DEFAULT_*，退化单组树）。
        overrides —— 用户覆盖轨迹（§5.4）。
        audit_log —— 审计时间线（哪步谁改了什么）。
        events —— 逐节点 provenance 事件（前端依据卡数据源，无论是否暂停每节点都发）。
        critic —— Critic 对抗复核信封（M3 管线④：批量列清单点火时对抽取草表的质疑清单，
          随身携带供前端评审表标注；图节点不消费。status=need_review＝复核未执行≠绿灯）。
        status —— running / awaiting_input / done / blocked。
    """

    task_id: str
    spec_version: str
    region: str
    period: str | None
    price_source: str
    rates: dict[str, Any] | None
    params: dict[str, Any] | None
    rollup: dict[str, Any] | None
    feature: str
    current_item: int
    needs_rematch: bool
    items: list[dict[str, Any]]
    overrides: Annotated[list[dict[str, Any]], operator.add]
    audit_log: Annotated[list[dict[str, Any]], operator.add]
    events: Annotated[list[dict[str, Any]], operator.add]
    critic: dict[str, Any] | None
    status: str


def _now() -> str:
    """返回 UTC ISO8601 时间戳（审计用）。"""
    return datetime.now(timezone.utc).isoformat()


def lock_value(value: Any, provenance: dict[str, Any], by: str = "model") -> dict[str, Any]:
    """把一个字段值钉成「权威已确认」结构（§5.4 ``{value, locked, provenance}``）。

    参数：value —— 确认/覆盖后的值（编码 / 子目号 / 单价）；provenance —— 该值来源信封的 provenance 块；
      by —— 来源主体（model=自动过 / user=人工确认或覆盖）。
    返回：``{"value", "locked": True, "provenance", "by", "at"}``——locked=True 表示下游只读、
      确定性重算、不再触发 LLM 匹配（设计原则 3）。
    """
    return {"value": value, "locked": True, "provenance": provenance, "by": by, "at": _now()}


def audit_entry(node: str, action: str, detail: dict[str, Any], by: str = "model") -> dict[str, Any]:
    """构造一条审计记录（§5.4 audit_log 元素）。

    参数：node —— 节点名；action —— 动作（auto_pass / approve / select_alternative /
      manual_override / input）；detail —— 结构化细节；by —— model / user。
    返回：``{node, action, detail, by, at}``。
    """
    return {"node": node, "action": action, "detail": detail, "by": by, "at": _now()}


def override_entry(node: str, item_idx: int, value: Any, by: str = "user") -> dict[str, Any]:
    """构造一条 override 记录（§5.4 overrides 元素，支撑造价文件交付审计）。

    参数：node —— 被覆盖的节点；item_idx —— items 下标；value —— 用户给定值；by —— 来源主体。
    返回：``{node, item, by, value, at}``。
    """
    return {"node": node, "item": item_idx, "by": by, "value": value, "at": _now()}


def gate_event(node: str, *, paused: bool, confidence: float | None = None, tau: float | None = None,
               provenance: dict[str, Any] | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造一条「门控决策事件」——让停闸/自动采纳都进依据时间线，自动过附「为什么没问你」。

    参数：node —— 门控节点名；paused —— 是否停闸等人（False=自动采纳）；confidence —— 决策置信度
      （编码闸有，余 None）；tau —— 自动过的置信阈值（仅编码闸：confidence≥tau 故自动采纳）；
      provenance —— 来源信封 provenance 块；detail —— 落值细节（如 ``{code}`` / ``{quantity}``）。
    返回：``{step, status(auto_pass/paused), auto_pass, confidence, tau, provenance, result, paused, at}``——
      前端据 ``auto_pass`` 标「自动采纳」，据 ``tau``/``confidence`` 显示「置信 X ≥ 阈值 τ」。
    """
    return {
        "step": node,
        "status": "paused" if paused else "auto_pass",
        "auto_pass": not paused,
        "confidence": confidence,
        "tau": tau,
        "provenance": provenance,
        "result": detail,
        "paused": paused,
        "at": _now(),
    }


def provenance_event(envelope: dict[str, Any], *, paused: bool) -> dict[str, Any]:
    """从原语信封派生一条「节点 provenance 事件」（前端依据卡数据源）。

    参数：envelope —— §5.1 信封；paused —— 该节点是否触发了暂停闸。
    返回：``{step, status, provenance, paused, at}``——只搬运信封里的结构化来源，不口述（原则 2）。
    """
    return {
        "step": envelope.get("step"),
        "status": envelope.get("status"),
        "provenance": envelope.get("provenance"),
        "result": envelope.get("result"),
        "paused": paused,
        "at": _now(),
    }
