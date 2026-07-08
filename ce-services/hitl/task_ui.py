"""③ Human Task UI —— 呈现 & 回填：HumanTask ↔ 用户交互载荷。

当前实现走 harness 的 ``ask_clarification`` 通道（turn 边界式，文本问答）：
``render`` 把 HumanTask 压成 ask_clarification 入参、``parse`` 把用户自由文本还原成 HumanResponse。
要富前端（点选/表单/整单批量）时另实现一个同签名的 UI 类替换即可，四层其余不动。
"""
from __future__ import annotations

import re
from typing import Any

from .models import HITLReason, HumanAction, HumanResponse, HumanTask

# HITLReason → ask_clarification 的 clarification_type（harness ClarificationMiddleware 认这几个）
_REASON_TO_CTYPE: dict[HITLReason, str] = {
    HITLReason.MISSING_INFO: "missing_info",
    HITLReason.LOW_CONFIDENCE: "ambiguous_requirement",
    HITLReason.RULE_CONFIRMATION: "approach_choice",
    HITLReason.ANOMALY_REVIEW: "risk_confirmation",
    HITLReason.RISKY_ACTION: "risk_confirmation",
}

_APPROVE_WORDS = {"approve", "确认", "同意", "ok", "好", "可以", "通过"}
_REJECT_WORDS = {"reject", "驳回", "不对", "都不对", "放弃", "取消"}
_ESCALATE_WORDS = {"escalate", "上报", "转项目经理", "上级"}


def _fmt_candidate(c: dict[str, Any]) -> str:
    code = c.get("code") or c.get("子目号")
    name = c.get("name")
    score = c.get("score")
    tail = f"（{name}）" if name else ""
    if score is not None:
        tail += f" 置信 {score}"
    return f"{code}{tail}".strip()


def _fmt_field(f: dict[str, Any]) -> str:
    label = f.get("label") or f["key"]
    req = "必填" if f.get("required") else "可选"
    opts = f.get("options")
    suffix = f"；可选值 {opts}" if opts else ""
    return f"{label}（{f['key']}，{req}）{suffix}"


def _fmt_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    return "；".join(f"{k}={v}" for k, v in context.items() if v not in (None, "", [], {}))


def _coerce(v: str, field: dict[str, Any]) -> Any:
    if field.get("type") == "number":
        try:
            return float(v)
        except (ValueError, TypeError):
            return None
    return v


def _parse_fields(text: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """把自由文本解析成 fields 值：单字段直取；多字段按「key=v，label：v」尽力解析。"""
    keys = {f["key"]: f for f in fields}
    labels = {(f.get("label") or f["key"]): f["key"] for f in fields}
    if len(fields) == 1 and not re.search(r"[=:：]", text):
        return {fields[0]["key"]: _coerce(text.strip(), fields[0])}
    data: dict[str, Any] = {}
    for part in re.split(r"[，,；;]\s*", text.strip()):
        kv = re.split(r"[=:：]\s*", part, maxsplit=1)
        if len(kv) == 2:
            k, v = kv[0].strip(), kv[1].strip()
            key = k if k in keys else labels.get(k)
            if key:
                data[key] = _coerce(v, keys[key])
    return data


class HumanTaskUI:
    """③ 呈现 & 回填 —— HumanTask ↔ 用户交互载荷（ask_clarification 通道）。"""

    def render(self, task: HumanTask) -> dict[str, Any]:
        """HumanTask → ask_clarification 入参 ``{question, clarification_type, context, options}``。

        候选（confirm 型）→ 编号 options；待填字段（input 型）→ 提示要填哪些；复核型无 options。
        让现成 ClarificationMiddleware 原样渲染 + goto=END——不改 harness。
        """
        if task.candidates:
            options = [_fmt_candidate(c) for c in task.candidates]
        elif task.fields:
            options = [_fmt_field(f) for f in task.fields]
        else:
            options = []
        ctx = task.description or _fmt_context(task.context)
        return {
            "question": task.title,
            "clarification_type": _REASON_TO_CTYPE.get(task.reason, "missing_info"),
            "context": ctx,
            "options": options,
        }

    def parse(self, task: HumanTask, reply: str) -> HumanResponse:
        """用户自由文本回复 → HumanResponse（对齐传入的**当前** task）。

        当前 task 由调用方 ``policy_engine.evaluate(state)`` 确定性重建（无需在消息里藏 task_id）。
        规则：关键词命中 approve/reject/escalate（且被 allowed）；候选场景纯数字 N→select 第 N 个；
        待填字段场景→respond 解析字段；兜底 respond 原文。所有动作用 allowed_actions 兜底。
        """
        text = (reply or "").strip()
        low = text.lower()
        allowed = set(task.allowed_actions)

        if HumanAction.REJECT in allowed and low in _REJECT_WORDS:
            return HumanResponse(action=HumanAction.REJECT, comment=text)
        if HumanAction.APPROVE in allowed and low in _APPROVE_WORDS:
            return HumanResponse(action=HumanAction.APPROVE, comment=text)
        if HumanAction.ESCALATE in allowed and low in _ESCALATE_WORDS:
            return HumanResponse(action=HumanAction.ESCALATE, comment=text)

        if task.candidates and HumanAction.SELECT in allowed and text.isdigit():
            i = int(text) - 1
            if 0 <= i < len(task.candidates):
                cand = task.candidates[i]
                code = cand.get("code") or cand.get("子目号")
                return HumanResponse(action=HumanAction.SELECT, selected=str(code), comment=text)

        if task.fields and HumanAction.RESPOND in allowed:
            return HumanResponse(action=HumanAction.RESPOND, data=_parse_fields(text, task.fields))

        if HumanAction.APPROVE in allowed:  # 复核型（无候选无字段）默认按同意
            return HumanResponse(action=HumanAction.APPROVE, comment=text)
        if HumanAction.RESPOND in allowed:
            return HumanResponse(action=HumanAction.RESPOND, data={"_raw": text}, comment=text)
        return HumanResponse(action=next(iter(allowed)), comment=text)
