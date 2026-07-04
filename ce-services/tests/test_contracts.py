#!/usr/bin/env python3
"""契约单一源测试（M1 契约线）：``gates.py`` 实际产出的 payload 必须能过 ``cost/contracts.py`` 校验。

目的＝挡漂移：谁改了 gates 的字段形状而没同步契约模型（进而没同步前端 codegen），在这里立刻红，
不必等 Docker build 的 tsc。与 tools/test_backlog.py 同约定：无 pytest 硬依赖，__main__ 直跑亦可。

刻意只依赖 ``cost.gates`` + ``cost.contracts``（纯函数，无 langgraph/服务）：
``session._format`` 的响应级校验需 langgraph，归服务器侧补（见 test_session_response_shape 的注）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost import gates  # noqa: E402
from cost.contracts import (  # noqa: E402
    ConfirmInterrupt,
    InputInterrupt,
    ReviewInterrupt,
    validate_interrupt,
    validate_session_response,
)

# 原语信封样例（选码 list_match 形状，字段与 provenance.list_match 一致）
_ENV = {
    "step": "list_match",
    "status": "ok",
    "result": {"code": "010503002001", "name": "矩形梁", "unit": "m3"},
    "provenance": {
        "source_type": "spec_clause",
        "source_ref": "GB50500-2013 §附录E",
        "confidence": 0.72,
        "alternatives": [{"code": "010503001", "name": "基础梁", "confidence": 0.61}],
    },
}


def test_confirm_payload_conforms() -> None:
    """gates.confirm_payload（编码/定额确认闸）→ ConfirmInterrupt。"""
    payload = gates.confirm_payload("list_coding", _ENV, "请确认清单编码")
    model = validate_interrupt(payload)
    assert isinstance(model, ConfirmInterrupt)
    assert model.actions == gates.CONFIRM_ACTIONS
    # list_gate 增量字段（confidence_band / context.hint）也须能过（extra 容忍 + 声明字段校验）
    payload["confidence_band"] = "mid"
    payload.setdefault("context", {})["hint"] = "置信低于 τ_low，建议补充特征"
    assert isinstance(validate_interrupt(payload), ConfirmInterrupt)


def test_input_payload_conforms() -> None:
    """gates.input_payload（费率录入闸字段集，graph.rates_gate 同款）→ InputInterrupt。"""
    payload = gates.input_payload(
        "rates",
        "请录入综合单价费率",
        [
            {"key": "management_fee_rate", "type": "number", "label": "管理费率（%）", "required": True},
            {"key": "profit_rate", "type": "number", "label": "利润率（%）", "required": True},
            {"key": "risk_rate", "type": "number", "label": "风险费率（%）", "default": 0},
            {"key": "fee_base", "type": "enum", "label": "取费基数",
             "options": ["labor", "labor_machine", "lmm"], "required": True},
        ],
    )
    model = validate_interrupt(payload)
    assert isinstance(model, InputInterrupt)
    assert {f.key for f in model.fields} == {"management_fee_rate", "profit_rate", "risk_rate", "fee_base"}


def test_quota_missing_payload_conforms() -> None:
    """gates.quota_missing_payload（缺定额补录闸，含 partial 重问态）→ InputInterrupt。"""
    empty_env = {"result": {"quotas": []}, "provenance": {"source_ref": None}}
    for partial in (False, True):
        payload = gates.quota_missing_payload(empty_env, "010503002001", "C30矩形梁", partial=partial)
        model = validate_interrupt(payload)
        assert isinstance(model, InputInterrupt)
        assert model.node == "quota_missing"
        assert model.context and "message" in model.context


def test_review_payload_conforms() -> None:
    """末尾复核闸（graph.rollup_node 内联字面量的镜像样例）→ ReviewInterrupt。

    注：rollup_node 在 graph.py（import langgraph），本测试不拉图依赖，用与其字面量
    同形的样例把关契约；graph 侧改形状时此处样例须同步（有意为之的双向哨兵）。
    """
    payload = {
        "gate_type": "review",
        "node": "rollup",
        "title": "请复核总造价",
        "rollup": {"total": 12345.6, "pre_tax_total": 11322.6, "single_works": []},
        "actions": ["approve"],
    }
    assert isinstance(validate_interrupt(payload), ReviewInterrupt)


def test_session_response_shape() -> None:
    """会话响应契约（session._format 的形状样例）→ CostSessionResponse。

    注：直接校验 ``session._format`` 需 langgraph（模块加载即建图单例），本地/CI 无图依赖时
    用同形样例把关；服务器侧可另跑「真响应过契约」的集成断言（M1 后续）。
    """
    confirm = gates.confirm_payload("list_coding", _ENV, "请确认清单编码")
    resp = {
        "task_id": "t-001",
        "status": "awaiting_input",
        "interrupt": confirm,
        "events": [
            {"step": "list_match", "status": "ok", "provenance": _ENV["provenance"],
             "result": _ENV["result"], "paused": False},
            {"step": "list_gate", "paused": True, "confidence": 0.72, "tau": 0.75,
             "detail": {"code": "010503002001", "band": "mid"}},
        ],
        "items": [{"feature": "C30现浇矩形梁", "code": {"value": None, "locked": False}}],
        "overrides": [],
        "audit_log": [{"node": "list_coding", "action": "pause", "by": "model"}],
        "rates": None,
        "params": None,
        "rollup": None,
    }
    model = validate_session_response(resp)
    assert model.task_id == "t-001"
    assert isinstance(model.interrupt, ConfirmInterrupt)
    assert len(model.events) == 2 and model.events[1].tau == 0.75


def test_bad_payload_rejected() -> None:
    """反向哨兵：必填缺失/枚举越界必须被拒——证明校验真的在咬人，不是全 extra 放行。"""
    from pydantic import ValidationError

    bad_cases = [
        {"gate_type": "confirm", "node": "x", "title": "t"},                      # 缺 proposal/evidence/…
        {"gate_type": "input", "node": "x", "title": "t",
         "fields": [{"key": "k", "type": "checkbox", "label": "l"}]},             # type 枚举越界
        {"gate_type": "review", "node": "x", "title": "t", "actions": []},        # 缺 rollup
    ]
    for bad in bad_cases:
        try:
            validate_interrupt(bad)
        except ValidationError:
            continue
        raise AssertionError(f"非法 payload 未被拒: {bad}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    failed = 0
    for _name in sorted(k for k in dir() if k.startswith("test_")):
        try:
            globals()[_name]()
            print(f"✓ {_name}")
        except AssertionError as exc:
            failed += 1
            print(f"✗ {_name}  {exc}")
    print(f"\n契约测试：{'全绿' if not failed else f'{failed} 败'}")
    sys.exit(1 if failed else 0)
