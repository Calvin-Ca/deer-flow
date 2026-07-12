from __future__ import annotations

from typing import Any

import pytest

from app.ce.cost import nodes
from app.ce.cost import workflow as workflow_module
from app.ce.cost.tools import (
    cost_workflow_node_tool,
    cost_workflow_resume_tool,
    cost_workflow_start_tool,
    cost_workflow_state_tool,
)
from app.ce.cost.workflow import get_workflow_state, resume_workflow, start_workflow
from deerflow.config.checkpointer_config import load_checkpointer_config_from_dict
from deerflow.runtime.checkpointer import reset_checkpointer


@pytest.fixture(autouse=True)
def use_memory_checkpointer():
    load_checkpointer_config_from_dict({"type": "memory"})
    reset_checkpointer()
    workflow_module._GRAPH = None
    workflow_module._GRAPH_CHECKPOINTER_ID = None
    yield
    workflow_module._GRAPH = None
    workflow_module._GRAPH_CHECKPOINTER_ID = None
    reset_checkpointer()
    load_checkpointer_config_from_dict(None)


def test_cost_workflow_tools_are_named_for_config_loading():
    assert cost_workflow_start_tool.name == "cost_workflow_start"
    assert cost_workflow_node_tool.name == "cost_workflow_node"
    assert cost_workflow_resume_tool.name == "cost_workflow_resume"
    assert cost_workflow_state_tool.name == "cost_workflow_state"


def test_bill_match_node_invokes_ce_rag_mcp(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured["name"] = name
        captured["arguments"] = arguments
        return {
            "status": "ok",
            "result": {
                "candidates": [
                    {"code": "010502001", "name": "矩形柱", "score": 0.91},
                ]
            },
        }

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    result = nodes.bill_match_node({"description": "C30现浇钢筋混凝土矩形柱", "spec": "2024", "top_k": 3})

    assert captured == {
        "name": "ce-rag_match_bill_item",
        "arguments": {
            "description": "C30现浇钢筋混凝土矩形柱",
            "spec": "2024",
            "top_k": 3,
        },
    }
    assert result["status"] == "done"
    assert result["candidates"][0]["code"] == "010502001"


def test_business_step_interfaces_expose_agent_tool_llm_slots():
    for step in ("bill_match", "quota_compose", "price_query", "calc"):
        interfaces = nodes._BUSINESS_STEP_INTERFACES[step]
        assert callable(interfaces.agent)
        assert callable(interfaces.tool)
        assert callable(interfaces.llm)


def test_run_business_step_defaults_to_tool_strategy(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_bill_match(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = payload
        return {"node": "bill_match", "status": "done"}

    monkeypatch.setattr(nodes, "bill_match_node", fake_bill_match)
    monkeypatch.setattr(
        nodes,
        "_BUSINESS_STEP_INTERFACES",
        {
            "bill_match": nodes.StepInterfaces(
                agent=nodes.bill_match_agent_node,
                tool=fake_bill_match,
                llm=nodes.bill_match_llm_node,
            )
        },
    )

    result = nodes.run_business_step("bill_match", {"description": "矩形柱"})

    assert result == {"node": "bill_match", "status": "done"}
    assert captured["payload"] == {"description": "矩形柱"}


def test_run_business_step_llm_and_agent_slots_are_reserved():
    agent_result = nodes.run_business_step("bill_match", {}, strategy="agent")
    llm_result = nodes.run_business_step("bill_match", {}, strategy="llm")

    assert agent_result["status"] == "not_implemented"
    assert llm_result["status"] == "not_implemented"
    assert agent_result["supported_strategies"] == ["agent", "tool", "llm"]
    assert llm_result["supported_strategies"] == ["agent", "tool", "llm"]


def test_quota_compose_and_calc_aliases_are_routable(monkeypatch):
    def fake_price_compose(payload: dict[str, Any]) -> dict[str, Any]:
        return {"node": "price_compose", "status": "done", "payload": payload}

    def fake_unit_price(payload: dict[str, Any]) -> dict[str, Any]:
        return {"node": "unit_price", "status": "done", "payload": payload}

    monkeypatch.setattr(nodes, "price_compose_node", fake_price_compose)
    monkeypatch.setattr(nodes, "unit_price_node", fake_unit_price)
    monkeypatch.setitem(
        nodes._NODES,
        "quota_compose",
        fake_price_compose,
    )
    monkeypatch.setitem(
        nodes._NODES,
        "calc",
        fake_unit_price,
    )

    quota_result = nodes.run_node("quota_compose", {"code": "010502001"})
    calc_result = nodes.run_node("calc", {"operation": "unit_price", "components": []})

    assert quota_result == {"node": "price_compose", "status": "done", "payload": {"code": "010502001"}}
    assert calc_result == {"node": "unit_price", "status": "done", "payload": {"operation": "unit_price", "components": []}}


def test_workflow_routes_business_steps_through_registry(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_run_business_step(step: str, payload: dict[str, Any], *, strategy: str = "tool") -> dict[str, Any]:
        calls.append((step, strategy, payload))
        if step == "bill_match":
            return {"node": "bill_match", "status": "done", "candidates": [{"code": "010502001"}]}
        if step == "quota_compose":
            return {"node": "price_compose", "status": "done", "result": {"code": payload["code"]}}
        raise AssertionError(f"unexpected step: {step}")

    monkeypatch.setattr(workflow_module, "run_business_step", fake_run_business_step)

    state = {
        "task_id": "cost-test",
        "created_at": "now",
        "updated_at": "now",
        "status": "running",
        "spec": "2013",
        "region": "深圳",
        "period": None,
        "top_k": 10,
        "items": [{"description": "矩形柱", "components": [{"quantity": 2, "unit_price": 3}]}],
        "current_index": 0,
        "events": [],
        "interrupt": None,
        "pending": None,
        "step_strategies": {"bill_match": "tool", "quota_compose": "tool", "calc": "tool"},
    }

    result = workflow_module._workflow_step(state)
    assert result["items"][0]["bill_match"]["node"] == "bill_match"
    assert calls[0][0] == "bill_match"
    assert calls[0][1] == "tool"

    result["items"][0]["selection"] = {"selected_code": "010502001"}
    result = workflow_module._workflow_step(result)
    assert result["items"][0]["price_compose"]["node"] == "price_compose"
    assert calls[1][0] == "quota_compose"

    # 单方案自动降级后新增 price_review 步（无 no_source 料 → 直接 done，不调 MCP）
    result = workflow_module._workflow_step(result)
    assert result["items"][0]["price_review"]["status"] == "done"

    # settle 步：compute 引擎算综合单价（默认 target=unit_rate）
    result = workflow_module._workflow_step(result)
    assert result["items"][0]["settle"]["status"] == "done"

    # 所有阶段产物齐备 → 游标推进到下一条（已删除残留的 unit_price/calc 阶段）
    result = workflow_module._workflow_step(result)
    assert result["current_index"] == 1
    assert "unit_price" not in result["items"][0]
    assert [c[0] for c in calls] == ["bill_match", "quota_compose"]


def test_start_workflow_pauses_for_bill_selection_and_resumes(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "ce-rag_match_bill_item":
            return {
                "status": "ok",
                "result": {
                    "candidates": [
                        {"code": "010502001", "name": "矩形柱", "score": 0.62},
                        {"code": "010502002", "name": "异形柱", "score": 0.60},
                    ]
                },
            }
        if name == "ce-db_price_compose":
            return {"status": "ok", "result": {"code": arguments["code"], "quotas": []}}
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    started = start_workflow(feature="C30现浇钢筋混凝土矩形柱", spec=None, region="深圳")

    assert started["status"] == "awaiting_input"
    assert started["interrupt"]["gate_type"] == "review"
    assert started["interrupt"]["node"] == "select_bill"
    assert calls == [
        (
            "ce-rag_match_bill_item",
            {"description": "C30现浇钢筋混凝土矩形柱", "spec": "2013", "top_k": 10},
        )
    ]
    checkpointed = get_workflow_state(started["task_id"])
    assert checkpointed["task_id"] == started["task_id"]
    assert checkpointed["status"] == "awaiting_input"

    resumed = resume_workflow(started["task_id"], {"selected_code": "010502001"})

    assert resumed["status"] == "done"
    assert resumed["result"]["items"][0]["selected_code"] == "010502001"
    assert calls[-1] == (
        "ce-db_price_compose",
        {"code": "010502001", "spec": "2013", "region": "深圳"},
    )


def test_select_bill_accepts_manual_input_and_reason():
    result = nodes.select_bill_node(
        {
            "manual_input": "010502099",
            "candidates": [{"code": "010502001", "score": 0.6}],
            "reason": "甲方指定编码",
        }
    )
    assert result["status"] == "done"
    assert result["selected_code"] == "010502099"
    # 自输编码不在候选内 → manual_input 来源，且理由留痕
    assert result["selection_source"] == "manual_input"
    assert result["reason"] == "甲方指定编码"


def test_select_bill_low_confidence_gate_allows_manual_and_reason():
    result = nodes.select_bill_node(
        {"candidates": [{"code": "010502001", "score": 0.62}, {"code": "010502002", "score": 0.60}]}
    )
    assert result["status"] == "awaiting_input"
    gate = result["interrupt"]
    assert gate["gate_type"] == "review"
    assert gate["allow_manual"] is True
    assert gate["allow_reason"] is True


def test_select_quota_single_scheme_auto_selects():
    result = nodes.select_quota_node({"schemes": [{"scheme_id": "S1"}]})
    assert result["status"] == "done"
    assert result["selection_source"] == "auto_single_scheme"


def test_select_quota_multi_scheme_always_gates(monkeypatch):
    # 多方案一律落 review 闸（2026-07-12 定案：方案取舍是审批级动作，不做门限自动选）；
    # 引擎 LLM 预排 fail-open——此处显式关掉，验证裸候选照常落闸。
    monkeypatch.setenv("CE_QUOTA_RANK_ENABLED", "0")
    result = nodes.select_quota_node(
        {"schemes": [{"scheme_id": "S1", "score": 0.95}, {"scheme_id": "S2", "score": 0.40}]}
    )
    assert result["status"] == "awaiting_input"
    assert result["interrupt"]["gate_type"] == "review"
    assert result["interrupt"]["node"] == "select_quota"
    assert "recommendation" not in result


def test_select_quota_multi_scheme_gate_carries_llm_recommendation(monkeypatch):
    # LLM 预排可用时，建议附进闸载荷当「系统建议」——选定仍归人（闸不撤）。
    monkeypatch.setattr(nodes, "rank_schemes", lambda feature, schemes: {"recommended_scheme_id": "S1", "rationale": "泵送匹配"})
    result = nodes.select_quota_node(
        {"schemes": [{"scheme_id": "S1", "score": 0.6}, {"scheme_id": "S2", "score": 0.58}]}
    )
    assert result["status"] == "awaiting_input"
    assert result["interrupt"]["recommendation"]["recommended_scheme_id"] == "S1"


def test_unit_rate_computes_comprehensive_price_with_breakdown():
    result = nodes.unit_rate_node(
        {
            "components": [
                {"category": "人工", "name": "综合用工", "consumption": 1.0, "unit_price": 150},
                {"category": "材料", "name": "混凝土", "consumption": 1.01, "unit_price": 400},
                {"category": "机械", "name": "振捣器", "consumption": 0.1, "unit_price": 50},
            ],
            "management_rate": 0.1,
            "profit_rate": 0.05,
        }
    )
    assert result["status"] == "done"
    assert result["labor_cost"] == 150.0
    assert result["material_cost"] == 404.0
    assert result["machine_cost"] == 5.0
    assert result["rmm_cost"] == 559.0
    assert result["management_fee"] == 55.9  # 559 × 10%
    assert result["profit"] == 27.95  # 559 × 5%
    assert result["unit_price"] == 642.85  # 559 + 55.9 + 27.95
    # 逐步 breakdown（供前端展示）：末行是综合单价
    assert result["breakdown"][-1]["item"] == "综合单价"
    assert result["breakdown"][-1]["amount"] == 642.85


def test_unit_rate_requires_components():
    result = nodes.unit_rate_node({"management_rate": 0.1})
    assert result["status"] == "awaiting_input"
    assert result["required_fields"] == ["components"]


def test_calc_routes_unit_rate_as_supported():
    # unit_rate 现为已实现场景 → 不触发 capability_gap
    result = nodes.calc_tool_node(
        {"operation": "unit_rate", "components": [{"category": "人工", "consumption": 1, "unit_price": 100}]}
    )
    assert result["status"] == "done"
    assert result["node"] == "unit_rate"
    assert result["unit_price"] == 100.0


def test_line_total_computes_amount_with_breakdown():
    result = nodes.line_total_node({"unit_price": 642.85, "quantity": 12})
    assert result["status"] == "done"
    assert result["amount"] == 7714.2  # 642.85 × 12
    assert result["breakdown"][0]["item"] == "清单合价"


def test_compute_target_unit_rate_stops_at_comprehensive_price():
    # 浅 target：只算到综合单价，不碰工程量/合价
    result = nodes.compute_cost(
        {
            "target": "unit_rate",
            "components": [{"category": "人工", "consumption": 1, "unit_price": 100}],
            "management_rate": 0.1,
            "profit_rate": 0.05,
        }
    )
    assert result["status"] == "done"
    assert result["target"] == "unit_rate"
    assert result["unit_price"] == 115.0  # 100 ×(1+0.15)
    assert result["steps"] == ["unit_rate"]
    assert "total_price" not in result  # 未算合价


def test_compute_target_line_total_runs_full_chain():
    # 深 target：从工料机一路算到合价，breakdown 含综合单价 + 合价两段
    result = nodes.compute_cost(
        {
            "target": "line_total",
            "components": [{"category": "人工", "consumption": 1, "unit_price": 100}],
            "management_rate": 0.1,
            "profit_rate": 0.05,
            "quantity": 10,
        }
    )
    assert result["status"] == "done"
    assert result["unit_price"] == 115.0
    assert result["total_price"] == 1150.0  # 115 × 10
    assert result["steps"] == ["unit_rate", "line_total"]
    assert result["breakdown"][-1]["item"] == "清单合价"


def test_compute_target_line_total_skips_unit_rate_when_price_given():
    # 起点由数据决定：直接给了综合单价 → 跳过 unit_rate，只算合价
    result = nodes.compute_cost({"target": "line_total", "unit_price": 200, "quantity": 5})
    assert result["status"] == "done"
    assert result["total_price"] == 1000.0
    assert result["steps"] == ["line_total"]  # 未重算综合单价


def test_calc_tool_node_dispatches_to_engine_on_target():
    result = nodes.calc_tool_node({"target": "unit_rate", "components": [{"category": "材料", "consumption": 2, "unit_price": 50}]})
    assert result["node"] == "compute"
    assert result["unit_price"] == 100.0


def test_compute_unimplemented_target_raises_capability_gap():
    result = nodes.compute_cost({"target": "grand_total", "components": []})
    assert result["status"] == "awaiting_input"
    assert result["interrupt"]["gate_type"] == "capability_gap"
    assert result["interrupt"]["detail"]["requested_target"] == "grand_total"


def test_start_workflow_settle_capability_gap_resumes_with_model_estimate(monkeypatch):
    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "ce-rag_match_bill_item":
            return {"status": "ok", "result": {"candidates": [{"code": "010502001", "name": "矩形柱", "score": 0.95}]}}
        if name == "ce-db_price_compose":
            return {
                "status": "ok",
                "result": {
                    "code": arguments["code"],
                    "quotas": [
                        {
                            "quota_code": "010001-1",
                            "resources": [
                                {"category": "人工", "name": "综合用工", "consumption": 1.0, "unit_price": 150, "price_status": "matched"},
                            ],
                        }
                    ],
                },
            }
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    # settle_target=grand_total 无确定性公式 → 结算阶段触发 capability_gap
    started = start_workflow(feature="矩形柱", spec=None, region="深圳", settle_target="grand_total")
    assert started["status"] == "awaiting_input"
    assert started["interrupt"]["gate_type"] == "capability_gap"
    assert started["interrupt"]["node"] == "compute"

    # 模型按用户规则试算的结果回传 → 记录并强制标注非定稿 + 保留 breakdown
    resumed = resume_workflow(
        started["task_id"],
        {
            "manual_result": {
                "value": 8888.0,
                "breakdown": [{"item": "总造价", "formula": "综合单价×工程量×(1+费率)", "amount": 8888.0}],
                "rule": "用户自定义口径",
                "source": "llm_estimate",
            }
        },
    )
    assert resumed["status"] == "done"
    settle = resumed["items"][0]["settle"]
    assert settle["value"] == 8888.0
    assert settle["is_final"] is False  # 非定稿
    assert settle["verdict"] == "需人工复核"
    assert settle["source"] == "llm_estimate"
    assert settle["breakdown"][0]["amount"] == 8888.0  # 计算过程保留展示


def test_calc_out_of_range_operation_raises_capability_gap():
    result = nodes.calc_tool_node({"operation": "project_cost"})
    assert result["status"] == "awaiting_input"
    gate = result["interrupt"]
    assert gate["gate_type"] == "capability_gap"
    assert gate["node"] == "calc"
    assert gate["detail"]["requested_operation"] == "project_cost"


def test_price_query_multi_candidate_returns_review_gate(monkeypatch):
    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "ce-db_price_query"
        return {
            "status": "ok",
            "result": {"candidates": [{"name": "商品混凝土C30", "price": 520}, {"name": "商品混凝土C30泵送", "price": 545}]},
        }

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    result = nodes.price_query_node({"name": "C30混凝土"})
    assert result["status"] == "need_review"
    assert result["interrupt"]["gate_type"] == "review"
    assert result["interrupt"]["node"] == "price_query"


def test_price_review_all_matched_passes():
    quotas = [
        {
            "quota_code": "010001-1",
            "resources": [
                {"category": "人工", "name": "综合用工", "consumption": 1.0, "unit_price": 150, "price_status": "matched"},
            ],
        }
    ]
    result = nodes.price_review_node({"quotas": quotas, "region": "深圳"})
    assert result["status"] == "done"
    assert result["missing_count"] == 0


def test_price_review_no_source_triggers_hitl_with_heuristic_candidates(monkeypatch):
    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "ce-db_price_query"  # 启发式：按料名模糊召回信息价近似料
        return {"status": "ok", "result": {"candidates": [{"name": "干混砌筑砂浆 M10", "price": 460}]}}

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    quotas = [
        {
            "quota_code": "010001-1",
            "resources": [
                {"category": "材料", "name": "干混砌筑砂浆", "spec": "M7.5", "unit": "m3", "consumption": 0.2, "price_status": "no_source"},
            ],
        }
    ]
    result = nodes.price_review_node({"quotas": quotas, "region": "深圳"})
    assert result["status"] == "awaiting_input"
    gate = result["interrupt"]
    assert gate["gate_type"] == "review"
    assert gate["node"] == "price_review"
    assert len(gate["missing"]) == 1
    assert gate["missing"][0]["candidates"][0]["price"] == 460


def test_price_review_falls_back_to_approximate_suggest(monkeypatch):
    # 精确/子串查 miss → 回退近似料召回 ce-db_price_suggest
    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "ce-db_price_query":
            return {"status": "ok", "result": {"results": []}}  # 子串未命中
        if name == "ce-db_price_suggest":
            assert arguments.get("category") == "材料"  # 同类过滤透传
            return {"status": "ok", "result": {"suggestions": [{"name": "干混砂浆 M10", "price": 460, "score": 0.66, "match": "heuristic_ngram"}]}}
        raise AssertionError(f"unexpected tool: {name}")

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    quotas = [
        {
            "quota_code": "010001-1",
            "resources": [
                {"category": "材料", "name": "干混砌筑砂浆", "spec": "M7.5", "consumption": 0.2, "price_status": "no_source"},
            ],
        }
    ]
    result = nodes.price_review_node({"quotas": quotas, "region": "深圳"})
    assert result["status"] == "awaiting_input"
    cand = result["interrupt"]["missing"][0]["candidates"]
    assert cand[0]["name"] == "干混砂浆 M10" and cand[0]["match"] == "heuristic_ngram"


def test_price_review_resume_applies_price_and_reruns_amount():
    quotas = [
        {
            "quota_code": "010001-1",
            "resources": [
                {"category": "材料", "name": "干混砌筑砂浆", "spec": "M7.5", "unit": "m3", "consumption": 0.2, "price_status": "no_source"},
            ],
        }
    ]
    key = "010001-1::干混砌筑砂浆::M7.5"
    result = nodes.price_review_node(
        {"quotas": quotas, "region": "深圳", "prices": [{"key": key, "price": 450, "reason": "市场询价"}]}
    )
    assert result["status"] == "done"
    assert result["missing_count"] == 0  # 已补价，无剩余缺口
    priced = result["priced"][0]
    assert priced["amount"] == 90.0  # 0.2 × 450 × 1
    # 就地写回定额资源：状态 manual + amount + 理由留痕
    resource = quotas[0]["resources"][0]
    assert resource["price_status"] == "manual"
    assert resource["amount"] == 90.0
    assert resource["reason"] == "市场询价"


def test_price_review_unpriced_resource_keeps_no_source():
    quotas = [
        {
            "quota_code": "010001-1",
            "resources": [
                {"category": "材料", "name": "料A", "consumption": 1.0, "price_status": "no_source"},
                {"category": "材料", "name": "料B", "consumption": 2.0, "price_status": "no_source"},
            ],
        }
    ]
    # 只补料A，料B 未提供 → 红线：如实保留缺口，不编价
    result = nodes.price_review_node(
        {"quotas": quotas, "prices": [{"key": "010001-1::料A::", "price": 10}]}
    )
    assert result["status"] == "done"
    assert result["missing_count"] == 1
    assert result["still_missing"][0]["name"] == "料B"
    assert quotas[0]["resources"][1]["price_status"] == "no_source"


def test_start_workflow_pauses_for_price_review_and_resumes(monkeypatch):
    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "ce-rag_match_bill_item":
            return {"status": "ok", "result": {"candidates": [{"code": "010502001", "name": "矩形柱", "score": 0.95}]}}
        if name == "ce-db_price_compose":
            # 单方案（无 schemes）+ 一条 no_source 工料机 → 组价后停在询价
            return {
                "status": "ok",
                "result": {
                    "code": arguments["code"],
                    "quotas": [
                        {
                            "quota_code": "010001-1",
                            "resources": [
                                {"category": "材料", "name": "干混砂浆", "spec": "M7.5", "consumption": 0.2, "price_status": "no_source"},
                            ],
                        }
                    ],
                },
            }
        if name == "ce-db_price_query":
            return {"status": "ok", "result": {"candidates": [{"name": "干混砂浆 M10", "price": 460}]}}
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    started = start_workflow(feature="C30现浇钢筋混凝土矩形柱", spec=None, region="深圳")

    assert started["status"] == "awaiting_input"
    assert started["interrupt"]["node"] == "price_review"
    assert started["interrupt"]["missing"][0]["candidates"][0]["price"] == 460

    resumed = resume_workflow(
        started["task_id"],
        {"prices": [{"key": "010001-1::干混砂浆::M7.5", "price": 450, "reason": "现场询价"}]},
    )
    assert resumed["status"] == "done"
    # 询价结果落进组价：该料状态 manual、amount 重算
    resource = resumed["items"][0]["price_compose"]["result"]["quotas"][0]["resources"][0]
    assert resource["price_status"] == "manual"
    assert resource["amount"] == 90.0


def test_start_workflow_pauses_for_quota_scheme_and_resumes(monkeypatch):
    def fake_call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "ce-rag_match_bill_item":
            # 高置信单候选 → 自动选码，跳过 bill 复核，直达 quota 方案复核
            return {"status": "ok", "result": {"candidates": [{"code": "010502001", "name": "矩形柱", "score": 0.95}]}}
        if name == "ce-db_price_compose":
            return {
                "status": "ok",
                "result": {
                    "code": arguments["code"],
                    "schemes": [
                        {"scheme_id": "S1", "name": "组合钢模板", "score": 0.6},
                        {"scheme_id": "S2", "name": "木模板", "score": 0.58},
                    ],
                },
            }
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(nodes, "call_mcp_tool", fake_call_mcp_tool)

    started = start_workflow(feature="C30现浇钢筋混凝土矩形柱", spec=None, region="深圳")

    assert started["status"] == "awaiting_input"
    assert started["interrupt"]["node"] == "select_quota"
    assert started["interrupt"]["gate_type"] == "review"

    resumed = resume_workflow(started["task_id"], {"selected_scheme": "S1", "reason": "现场采用钢模板"})

    assert resumed["status"] == "done"
    selection = resumed["items"][0]["quota_selection"]
    assert selection["selected_scheme_id"] == "S1"
    assert selection["selection_source"] == "explicit_input"
    assert selection["reason"] == "现场采用钢模板"


# ---- 显式 stage 状态机（STAGES 表 + 游标推导 + 统一暂停）------------------------------
def test_stage_table_output_keys_match_item_progression():
    # STAGES 的 output_key 序列 = 单条清单的组价 DAG，防漏配/错序
    assert [s.output_key for s in workflow_module.STAGES] == [
        "bill_match",
        "selection",
        "price_compose",
        "quota_selection",
        "price_review",
        "settle",
    ]
    # gate 阶段（可 HITL、strict 策略）与取数阶段（lenient）划分正确
    gate_nodes = {s.name: s.gate_node for s in workflow_module.STAGES}
    assert gate_nodes["select_bill"] == "select_bill"
    assert gate_nodes["select_quota"] == "select_quota"
    assert gate_nodes["price_review"] == "price_review"
    assert gate_nodes["settle"] == "settle"
    assert gate_nodes["bill_match"] is None
    assert gate_nodes["quota_compose"] is None
    # resume 表与正向 gate 阶段一一对应
    assert set(workflow_module._RESUME_HANDLERS) == {"select_bill", "select_quota", "price_review", "settle"}


def test_current_stage_skips_quota_when_single_scheme_autoselected():
    # 单方案已自动降级写入 quota_selection → 游标跳过 select_quota 直达 price_review
    item = {
        "bill_match": {"status": "done"},
        "selection": {"selected_code": "010502001"},
        "price_compose": {"status": "done"},
        "quota_selection": {"selection_source": "auto_single_scheme"},
    }
    assert workflow_module._current_stage(item).name == "price_review"


def test_current_stage_returns_select_quota_when_not_autoselected():
    item = {
        "bill_match": {"status": "done"},
        "selection": {"selected_code": "010502001"},
        "price_compose": {"status": "done"},
    }
    assert workflow_module._current_stage(item).name == "select_quota"


def test_current_stage_none_when_all_stage_keys_present():
    item = {k: {"status": "done"} for k in ("bill_match", "selection", "price_compose", "quota_selection", "price_review", "settle")}
    assert workflow_module._current_stage(item) is None


def test_current_stage_first_stage_on_empty_item():
    assert workflow_module._current_stage({}).name == "bill_match"


def test_pause_settle_gate_carries_gate_type_in_pending():
    settle_stage = next(s for s in workflow_module.STAGES if s.name == "settle")
    result = {"status": "awaiting_input", "interrupt": {"gate_type": "capability_gap", "node": "compute"}}
    out = workflow_module._pause({"items": []}, settle_stage, result, 0)
    assert out["status"] == "awaiting_input"
    assert out["pending"] == {"node": "settle", "index": 0, "gate_type": "capability_gap"}
    assert out["interrupt"]["node"] == "compute"


def test_pause_review_gate_omits_gate_type_in_pending():
    stage = next(s for s in workflow_module.STAGES if s.name == "select_bill")
    result = {"status": "awaiting_input", "interrupt": {"gate_type": "review", "node": "select_bill"}}
    out = workflow_module._pause({"items": []}, stage, result, 2)
    assert out["pending"] == {"node": "select_bill", "index": 2}
