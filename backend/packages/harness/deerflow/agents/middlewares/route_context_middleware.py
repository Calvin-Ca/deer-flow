"""Inject the ce-services deterministic intent-route decision as a <system-reminder>.

Why (M1 第零跳收权)
-------------------
lead_agent 是全链路唯一无金标、无审计的分诊环节（弱模型自由裁量要不要调组价/规范能力）。
本中间件把 ce-services 的**确定性意图路由**前置到每个回合：调 ``POST /route``
（含低置信 32b 兜底，红线闸永确定性），把 ``RouteDecision`` 的关键字段包成
``<system-reminder><route_decision>…`` 注入在当轮用户消息之前——模型从「自由分诊」
降为「按判定执行」。路由逻辑单一源仍在 ce-services（本文件只消费判定，不复制词表）。

Pattern
-------
复用 ``DynamicContextMiddleware`` 的成熟做法：``before_agent`` 钩子 + ID-swap——
reminder 顶替目标消息的 id（原位替换），用户原文以 ``{id}__routed`` 追加在后，
additional_kwargs 打标防重入（resume 回合不重复注入、不重复调 /route）。

Safety
------
- **默认完全不启用**：仅当环境变量 ``CE_ROUTE_CONTEXT_URL`` 设定时由 agent.py 注册。
- **fail-open**：/route 不可达 / 超时 / 响应非法 → 不注入，行为退回现状（零阻塞）。
- 纯标准库 HTTP（urllib），不给 harness 引新依赖；多模态/空消息跳过。
"""

from __future__ import annotations

import json
import logging
import urllib.request
import uuid
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# reminder 消息自标（识别自己注入的判定，正文匹配不可靠）
_ROUTE_REMINDER_KEY = "route_context_reminder"
# 用户消息已注入标（同一回合 resume 重入 before_agent 时防重复注入/重复调 /route）
_ROUTE_INJECTED_KEY = "route_context_injected"
_SUMMARY_MESSAGE_NAME = "summary"
# 注入 reminder 里透传的判定字段（与 ce-services RouteDecision.as_meta() 对齐的子集）
_DECISION_KEYS = (
    "capability", "compose_full", "clarify", "out_of_scope_region",
    "feature_complete", "caliber_complete", "needs_context",
    "route_confidence", "route_source",
)


def _extract_prior_capability(messages: list) -> str | None:
    """从历史里最近一条路由 reminder 解析上一轮能力（供 /route 会话粘性）；无/解析失败 → None。

    当轮 reminder 尚未注入，故逆序扫到的第一条 route reminder 即上一轮的判定；其正文里有
    一行 compact JSON（``_build_reminder`` 产出），取其中 capability 字段。任何异常按无处理。
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage) and message.additional_kwargs.get(_ROUTE_REMINDER_KEY):
            content = message.content if isinstance(message.content, str) else ""
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        cap = json.loads(line).get("capability")
                    except json.JSONDecodeError:
                        return None
                    return cap if isinstance(cap, str) else None
            return None
    return None


def _message_text(message: HumanMessage) -> str:
    """提取消息纯文本：str 原样；块列表（前端多模态形态 ``[{"type":"text",...}]``）拼接全部 text 块。

    首版只认 ``isinstance(content, str)``，前端消息实为块列表 → 每条都静默早退、中间件形同虚设
    （灰度首日踩坑）。与 DynamicContextMiddleware 对 content 的宽容处理对齐。
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return ""


def _is_real_user_message(message: object) -> bool:
    """Return whether *message* is a genuine user turn (not an injected reminder/summary)."""
    return (
        isinstance(message, HumanMessage)
        and message.name != _SUMMARY_MESSAGE_NAME
        and not message.additional_kwargs.get(_ROUTE_REMINDER_KEY)
        and not message.additional_kwargs.get("dynamic_context_reminder")
    )


class RouteContextMiddleware(AgentMiddleware):
    """Prepend the deterministic route decision to the current user message.

    Per agent invocation (``before_agent``): locate the latest real user message,
    POST its text to the ce-services ``/route`` endpoint, and inject the returned
    decision as a hidden ``<system-reminder>`` HumanMessage right before it via
    the ID-swap technique. Any failure skips injection (fail-open).
    """

    def __init__(self, route_url: str, timeout_seconds: float = 2.0):
        super().__init__()
        self._route_url = route_url
        self._timeout = timeout_seconds

    # ── /route 调用（纯标准库，fail-open）─────────────────────────────────
    def _fetch_decision(self, query: str, prior_capability: str | None = None) -> dict | None:
        body: dict = {"query": query, "use_llm_fallback": True}
        if prior_capability:
            body["prior_capability"] = prior_capability  # 会话粘性：承接句沿用上一轮能力（EH-05 扩展）
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self._route_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — 任何异常都不阻塞对话（fail-open）
            logger.warning("RouteContextMiddleware: /route unreachable or invalid, skip injection (fail-open): %s", exc)
            return None
        if isinstance(data, dict) and data.get("capability"):
            return data
        logger.warning("RouteContextMiddleware: /route response missing capability, skip injection")
        return None

    @staticmethod
    def _build_reminder(decision: dict) -> str:
        compact = {k: decision.get(k) for k in _DECISION_KEYS}
        return "\n".join(
            [
                "<system-reminder>",
                "<route_decision>",
                "以下是服务端确定性意图路由对本轮用户消息的判定（结构化、可审计）。"
                "请按 capability 与形态字段选择动作，不要自行重判能力归属；"
                "out_of_scope_region 非空＝他省口径出界，必须体面告知、不得调取数工具；"
                "clarify=feature 时先 ask_clarification 补构件特征（只问特征，不问版本）；"
                "capability=out_of_domain＝与造价无关：只说明你的能力范围（规范问答/构件组价/深圳信息价查询），"
                "不要回答问题本身，严禁编造域外内容（天气/新闻/代码等）。",
                json.dumps(compact, ensure_ascii=False),
                "</route_decision>",
                "</system-reminder>",
            ]
        )

    # ── 注入（ID-swap，同 DynamicContextMiddleware）───────────────────────
    def _inject(self, state) -> dict | None:
        messages = list(state.get("messages", []))
        idx = next((i for i in reversed(range(len(messages))) if _is_real_user_message(messages[i])), None)
        if idx is None:
            logger.debug("RouteContextMiddleware: no real user message found, skip")
            return None
        target = messages[idx]
        if target.additional_kwargs.get(_ROUTE_INJECTED_KEY):
            logger.debug("RouteContextMiddleware: current turn already injected, skip")
            return None
        text = _message_text(target).strip()
        if not text:
            # 纯图片/未知形态：路由无从判，跳过（fail-open）——但要出声，别再静默失联
            logger.info("RouteContextMiddleware: no extractable text in user message (content type=%s), skip",
                        type(target.content).__name__)
            return None

        decision = self._fetch_decision(text, _extract_prior_capability(messages))
        if decision is None:
            return None

        stable_id = target.id or str(uuid.uuid4())
        reminder_msg = HumanMessage(
            content=self._build_reminder(decision),
            id=stable_id,
            additional_kwargs={"hide_from_ui": True, _ROUTE_REMINDER_KEY: True},
        )
        user_msg = HumanMessage(
            content=target.content,
            id=f"{stable_id}__routed",
            name=target.name,
            additional_kwargs={**target.additional_kwargs, _ROUTE_INJECTED_KEY: True},
        )
        logger.info(
            "RouteContextMiddleware: injected route decision cap=%s conf=%s src=%s into msg id=%r",
            decision.get("capability"), decision.get("route_confidence"),
            decision.get("route_source"), target.id,
        )
        return {"messages": [reminder_msg, user_msg]}

    @override
    def before_agent(self, state, runtime: Runtime) -> dict | None:
        return self._inject(state)

    @override
    async def abefore_agent(self, state, runtime: Runtime) -> dict | None:
        return self._inject(state)
