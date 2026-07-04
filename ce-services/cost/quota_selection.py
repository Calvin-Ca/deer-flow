"""套定额选择层 —— 多定额子目候选内选最匹配子目（**为训练模型预留的挂点**）。

背景（COST_STEP_DISPLAY_PLAN §8 决策 4）：一个清单码可能映射多条定额子目（`quotas` 长度 > 1），
现状 `quota_gate` 对多子目**一律停闸人工确认**。将来用**训练过的套定额模型**在候选内选子目 + 置信门控
（高置信自动过、低置信升人闸），把"套定额"这一步的人闸压下去——与 `selection.select_code`（选码）
同构。**训练模型是贵 + 非确定性的推理副作用**，故落在图内 compute/gate 结构里、由本模块统一收口，
绝不进 langgraph 重放区（COST_STEP_DISPLAY_PLAN §7.4 / §8）。

红线（与 `select_code` 同款，钉在代码兜底）：
1. **只能从候选 `子目号` 里选**——模型越界（造子目）即作废、转人工；
2. **不确定标 need_review**——低置信（< τ）/ 选不出 → 停闸人工确认，只建议不定稿；
3. 算钱/费率仍走确定性库查表，模型只做"选哪条子目"的判断，不生成任何消耗量/价。

接口契约：训练模型就绪后实现一个 ``QuotaSelector``（见 ``register_quota_selector``）注入即可，
**无需改 `quota_gate` / 图结构**。未注入时 ``select_quota`` 返回 need_review（多子目维持现状人工确认）。
"""
from __future__ import annotations

from typing import Any, Callable

from common.config import HITL_TAU_HIGH

# 套定额选择模型钩子（QuotaSelector）：签名 ``(feature, code, quotas) -> raw dict``，
# raw 至少含 ``{子目号, confidence, reason?, alternatives?}``（同 select_code 输出族）。
# None = 未接入模型（默认），此时多子目一律走人工确认闸（行为与接入前完全一致）。
QuotaSelector = Callable[[str | None, str | None, list[dict[str, Any]]], dict[str, Any]]

_SELECTOR: QuotaSelector | None = None


def register_quota_selector(selector: QuotaSelector | None) -> None:
    """注入/卸载套定额选择模型（训练模型就绪后在服务启动时调用一次）。

    参数：selector —— ``(feature, code, quotas) -> {子目号, confidence, ...}``；传 None 卸载（回退人工确认）。
    """
    global _SELECTOR
    _SELECTOR = selector


def _need_review(reason: str) -> dict[str, Any]:
    """构造"转人工确认"结果（不选子目、置信 0）。"""
    return {"子目号": None, "confidence": 0.0, "reason": reason, "need_review": True, "alternatives": []}


def select_quota(
    feature: str | None,
    code: str | None,
    quotas: list[dict[str, Any]],
    *,
    tau: float = HITL_TAU_HIGH,
) -> dict[str, Any]:
    """在多定额子目候选内选最匹配的一项 + 确定性红线兜底（模型未接入 → need_review）。

    参数：
        feature —— 构件/做法描述；code —— 已钉清单编码；quotas —— 候选定额子目
        （``[{子目号, name?, labor_cost?, material_cost?, machine_cost?, ...}]``）；
        tau —— 置信阈值，校准后 confidence < tau → 强制 need_review（缺省沿用编码闸 τ_high）。
    返回：``{子目号, confidence, reason, need_review, alternatives}``：
        - 未注入模型（``_SELECTOR is None``）→ need_review（子目号=None）：多子目维持人工确认，行为不变；
        - 模型选的子目号不在候选（越界/造子目）→ 作废、need_review；
        - confidence < tau → 强制 need_review（只建议不定稿）；
        - 正常 → 选中子目号 + 有效置信 + 候选内 alternatives。
    """
    valid = {str(q.get("子目号")).strip() for q in quotas if q.get("子目号") not in (None, "")}
    if _SELECTOR is None:
        return _need_review("未接入套定额模型，多子目转人工确认")
    if not valid:
        return _need_review("无候选定额子目，转人工")

    raw = _SELECTOR(feature, code, quotas) or {}
    chosen = raw.get("子目号")
    chosen = str(chosen).strip() if chosen not in (None, "") else None
    reason = str(raw.get("reason", "")).strip()
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    alternatives = [a for a in (raw.get("alternatives") or [])
                    if str(a).strip() in valid and str(a).strip() != chosen]

    # 越界子目号（造子目 / 选了候选外的）：作废、转人工，不静默接受（红线 1）。
    if chosen is not None and chosen not in valid:
        return _need_review(f"[红线] 套定额模型选了候选外子目号={chosen!r}（疑造子目），已作废转人工")
    if chosen is None:
        return _need_review(reason or "套定额模型未选出子目，转人工")

    need_review = bool(raw.get("need_review", False)) or confidence < tau
    return {
        "子目号": chosen,
        "confidence": confidence,
        "reason": reason,
        "need_review": need_review,
        "alternatives": alternatives,
    }
