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
import re
import urllib.request
import uuid
from typing import override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
# 哑火收编的「像在提问」启发（m4-behavior-v2 归因定案，2026-07-06）：判定要求反问而模型
# 只回纯文本时，文本命中任一标记才收编成 ask_clarification——不命中说明模型给的是陈述句
# 「答案」，硬包成问题更糟，只出声告警（哑火可观测，不静默）。
_QUESTION_MARKERS = ("？", "?", "请提供", "请问", "请先", "请补充", "请告知", "需要您", "需要你")
# Qwen3 推理块（v3 实测 E6：<think>…漏进 content，思维链里的「？」触发误收编、整段思维链
# 被当 question 呈给用户）——标记检查与 question 文本都必须先剥掉它；未闭合（流截断）时
# 从 <think> 起全是推理，一并剥到结尾。
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)


def _extract_latest_decision(messages: list) -> dict | None:
    """逆序找最近一条路由 reminder，解析其正文里那行 compact JSON 判定；无/解析失败 → None。

    调用时机决定语义：``before_agent`` 注入前调用取到的是**上一轮**判定（会话粘性用）；
    ``after_model`` 时当轮 reminder 已注入，取到的是**当轮**判定（哑火收编用）。
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage) and message.additional_kwargs.get(_ROUTE_REMINDER_KEY):
            content = message.content if isinstance(message.content, str) else ""
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        decision = json.loads(line)
                    except json.JSONDecodeError:
                        return None
                    return decision if isinstance(decision, dict) else None
            return None
    return None


def _extract_prior_capability(messages: list) -> str | None:
    """从历史里最近一条路由 reminder 解析上一轮能力（供 /route 会话粘性）；无/解析失败 → None。"""
    cap = (_extract_latest_decision(messages) or {}).get("capability")
    return cap if isinstance(cap, str) else None


def _message_text(message) -> str:
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
                "clarify=caliber 时（规范问答缺口径）也必须先 ask_clarification 问「哪个地区、哪个清单规范版本」"
                "再取数——规范侧口径反问与组价侧「版本缺省不问」是两条规则，不要用后者压掉前者；"
                "需要反问时（仅限上述 clarify 非空的情形），一律必须通过 ask_clarification 工具发起"
                "（构件特征、口径、设计说明/图纸原文均同理），禁止在普通回复文本里反问——"
                "纯文本反问不会中断流程、用户答复接不回审查闸，视同未反问；"
                "反之 clarify=null（或缺省）＝判定信息已足够：禁止反问，直接按 capability 调工具执行，"
                "库内查不到就如实说无（need_review/no_source），不许用反问拖延或替代如实拒答；"
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

    # ── 哑火收编（after_model）──────────────────────────────────────────────
    def _coerce_silent_clarify(self, state) -> dict | None:
        """判定要求反问、模型却纯文本应答时，把文本收编成 ask_clarification 工具调用。

        背景（m4-behavior-v2 归因，2026-07-06）：E2/E3/E4 三条「列清单」用例 /route 判定
        完全一致（cost + clarify=feature），8B 对 E4 老实调 ask_clarification、对 E2/E3
        （查询里出现「这个项目」「设计说明」）漂移成纯文本索要材料——同指令不同服从，
        prompt 治不动，按「弱模型不驱动流程」归代码。纯文本反问不进 HITL 闸（不
        interrupt、用户答复接不回状态机），收编后由链尾 ClarificationMiddleware 的
        wrap_tool_call 拦截 interrupt（工具执行时拦截，与 after_model 钩子顺序无关）。

        只在「本轮判定 clarify∈{feature,caliber} + 自 reminder 起零工具活动 + 文本像在
        提问」三条件齐时收编；文本不像提问（陈述句「答案」）不硬转，只告警出声。
        """
        messages = list(state.get("messages", []))
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        last = messages[-1]
        if last.tool_calls:
            return None
        # 本轮 reminder 定位；没注入（/route 挂了 fail-open）则不干预
        idx = next((i for i in reversed(range(len(messages)))
                    if isinstance(messages[i], HumanMessage)
                    and messages[i].additional_kwargs.get(_ROUTE_REMINDER_KEY)), None)
        if idx is None:
            return None
        decision = _extract_latest_decision(messages)
        if not decision or decision.get("clarify") not in ("feature", "caliber"):
            return None
        # 本轮已有工具活动 → 这段文本是干完活的正经答复，不是哑火
        for m in messages[idx + 1:-1]:
            if isinstance(m, ToolMessage) or (isinstance(m, AIMessage) and m.tool_calls):
                return None
        text = _THINK_RE.sub("", _message_text(last)).strip()
        if not text:  # 剥完 <think> 空了 = 全是推理没正文，无可收编（出声归 quantity-drop 同类）
            logger.warning("RouteContextMiddleware: clarify=%s 但模型正文只有 <think> 推理块，无可收编", decision.get("clarify"))
            return None
        if not any(marker in text for marker in _QUESTION_MARKERS):
            logger.warning("RouteContextMiddleware: clarify=%s 但模型未反问也未调工具（哑火，文本非疑问不收编）: %.80s",
                           decision.get("clarify"), text)
            return None
        logger.warning("RouteContextMiddleware: 纯文本反问收编为 ask_clarification（clarify=%s）: %.80s",
                       decision.get("clarify"), text)
        tool_call = {
            "name": "ask_clarification",
            "args": {"question": text, "clarification_type": "missing_info"},
            "id": f"route-clarify-{uuid.uuid4().hex[:8]}",
            "type": "tool_call",
        }
        # 同 id 原位替换（add_messages reducer）：正文清空，问题文本由 ClarificationMiddleware
        # 的 ToolMessage 呈现，避免同一段话渲染两遍
        return {"messages": [AIMessage(content="", tool_calls=[tool_call], id=last.id)]}

    @override
    def before_agent(self, state, runtime: Runtime) -> dict | None:
        return self._inject(state)

    @override
    async def abefore_agent(self, state, runtime: Runtime) -> dict | None:
        return self._inject(state)

    @override
    def after_model(self, state, runtime: Runtime) -> dict | None:
        return self._coerce_silent_clarify(state)

    @override
    async def aafter_model(self, state, runtime: Runtime) -> dict | None:
        return self._coerce_silent_clarify(state)
