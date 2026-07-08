"""Tests for RouteContextMiddleware 哑火收编（after_model）.

Verifies that when the deterministic route decision demands a clarification
(clarify=feature/caliber) but the model replies with plain text (no tool calls),
the text is coerced into an ask_clarification tool call — and that the coercion
stays strictly scoped (no reminder / no clarify / prior tool activity / non-question
text all leave the message untouched).
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.route_context_middleware import (
    _ROUTE_REMINDER_KEY,
    RouteContextMiddleware,
    _extract_latest_decision,
    _extract_prior_capability,
)


def _make_middleware() -> RouteContextMiddleware:
    return RouteContextMiddleware(route_url="http://unused.invalid/route")


def _fake_runtime():
    return SimpleNamespace(context={})


def _reminder(clarify: str | None, capability: str = "cost") -> HumanMessage:
    """Build a route reminder exactly the way the middleware injects it."""
    decision = {"capability": capability, "clarify": clarify, "feature_complete": False,
                "caliber_complete": True, "needs_context": False, "compose_full": False,
                "out_of_scope_region": None, "route_confidence": "high", "route_source": "deterministic"}
    return HumanMessage(
        content=RouteContextMiddleware._build_reminder(decision),
        id="reminder-1",
        additional_kwargs={"hide_from_ui": True, _ROUTE_REMINDER_KEY: True},
    )


def _user(text: str = "根据设计说明编制工程量清单") -> HumanMessage:
    return HumanMessage(content=text, id="reminder-1__routed")


# ---------------------------------------------------------------------------
# 收编主路径
# ---------------------------------------------------------------------------


def test_coerces_plain_text_question_into_ask_clarification():
    mw = _make_middleware()
    state = {"messages": [_reminder("feature"), _user(),
                          AIMessage(content="请提供设计说明原文，我才能列出清单项。", id="ai-1")]}

    result = mw.after_model(state, _fake_runtime())

    assert result is not None
    (replacement,) = result["messages"]
    assert isinstance(replacement, AIMessage)
    assert replacement.id == "ai-1"  # 同 id 原位替换
    assert replacement.content == ""  # 问题文本由 ClarificationMiddleware 的 ToolMessage 呈现
    (tc,) = replacement.tool_calls
    assert tc["name"] == "ask_clarification"
    assert tc["args"]["question"] == "请提供设计说明原文，我才能列出清单项。"
    assert tc["args"]["clarification_type"] == "missing_info"


def test_coerces_for_caliber_clarify_too():
    mw = _make_middleware()
    state = {"messages": [_reminder("caliber", capability="norm"), _user("满堂脚手架按什么计算？"),
                          AIMessage(content="请问按哪个地区、哪个清单规范版本作答？", id="ai-1")]}

    result = mw.after_model(state, _fake_runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["name"] == "ask_clarification"


def test_handles_block_content_ai_message():
    """AIMessage content 为块列表（marker.ts / RouteContextMiddleware 两踩的形态）也要能提取。"""
    mw = _make_middleware()
    blocks = [{"type": "text", "text": "请补充构件的混凝土等级？"}]
    state = {"messages": [_reminder("feature"), _user(), AIMessage(content=blocks, id="ai-1")]}

    result = mw.after_model(state, _fake_runtime())

    assert result is not None
    assert result["messages"][0].tool_calls[0]["args"]["question"] == "请补充构件的混凝土等级？"


# ---------------------------------------------------------------------------
# 不收编的边界（一条都不能多管）
# ---------------------------------------------------------------------------


def test_no_coerce_when_clarify_is_null():
    mw = _make_middleware()
    state = {"messages": [_reminder(None), _user(),
                          AIMessage(content="请提供更多信息？", id="ai-1")]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_without_reminder():
    """/route 挂了 fail-open 没注入 → 不干预模型输出。"""
    mw = _make_middleware()
    state = {"messages": [_user(), AIMessage(content="请提供设计说明？", id="ai-1")]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_when_model_already_called_tools():
    mw = _make_middleware()
    last = AIMessage(content="", id="ai-1",
                     tool_calls=[{"name": "ce-rag_search_clause", "args": {}, "id": "tc-1", "type": "tool_call"}])
    state = {"messages": [_reminder("feature"), _user(), last]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_after_tool_activity_this_turn():
    """已有工具往返后的纯文本是干完活的答复，不是哑火。"""
    mw = _make_middleware()
    state = {"messages": [
        _reminder("feature"), _user(),
        AIMessage(content="", id="ai-1",
                  tool_calls=[{"name": "ce-rag_match_bill_item", "args": {}, "id": "tc-1", "type": "tool_call"}]),
        ToolMessage(content="候选清单码…", tool_call_id="tc-1", id="tm-1"),
        AIMessage(content="请从以下候选里确认？", id="ai-2"),
    ]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_when_text_is_not_a_question():
    """陈述句「答案」不硬包成问题——只告警不收编。"""
    mw = _make_middleware()
    state = {"messages": [_reminder("feature"), _user(),
                          AIMessage(content="矩形柱一般套 010502006。", id="ai-1")]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_on_empty_text():
    mw = _make_middleware()
    state = {"messages": [_reminder("feature"), _user(), AIMessage(content="", id="ai-1")]}
    assert mw.after_model(state, _fake_runtime()) is None


# ---------------------------------------------------------------------------
# <think> 推理块剥离（v3 E6 冤案：思维链里的「？」误触发收编、整段被当 question）
# ---------------------------------------------------------------------------


def test_think_block_stripped_from_question():
    mw = _make_middleware()
    text = "<think>用户没给特征，要不要问呢？先问吧。</think>请提供构件的混凝土等级？"
    state = {"messages": [_reminder("feature"), _user(), AIMessage(content=text, id="ai-1")]}

    result = mw.after_model(state, _fake_runtime())

    assert result is not None
    question = result["messages"][0].tool_calls[0]["args"]["question"]
    assert question == "请提供构件的混凝土等级？"
    assert "<think>" not in question


def test_no_coerce_when_content_is_think_only():
    """正文全是推理块（含问号）→ 剥完为空，不收编不硬造问题。"""
    mw = _make_middleware()
    state = {"messages": [_reminder("feature"), _user(),
                          AIMessage(content="<think>该问什么？嗯，先分析一下路由决策。</think>", id="ai-1")]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_on_unclosed_think_block():
    """未闭合 <think>（流截断）→ 自 <think> 起全按推理剥掉。"""
    mw = _make_middleware()
    state = {"messages": [_reminder("feature"), _user(),
                          AIMessage(content="<think>好的，用户说“这个工程开个清单”，需要处理这个请求？", id="ai-1")]}
    assert mw.after_model(state, _fake_runtime()) is None


def test_no_coerce_when_last_message_is_not_ai():
    mw = _make_middleware()
    state = {"messages": [_reminder("feature"), _user()]}
    assert mw.after_model(state, _fake_runtime()) is None


# ---------------------------------------------------------------------------
# 判定解析共用件（重构后 _extract_prior_capability 走同一解析）
# ---------------------------------------------------------------------------


def test_extract_latest_decision_reads_compact_json():
    decision = _extract_latest_decision([_reminder("feature", capability="cost"), _user()])
    assert decision is not None
    assert decision["capability"] == "cost"
    assert decision["clarify"] == "feature"


def test_extract_prior_capability_still_works():
    assert _extract_prior_capability([_reminder(None, capability="norm"), _user()]) == "norm"
    assert _extract_prior_capability([_user()]) is None
