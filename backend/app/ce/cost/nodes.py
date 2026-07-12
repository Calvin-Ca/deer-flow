"""Node implementations for the application-level CE cost workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .bill_match_engine import AUTO_SELECT_MARGIN, AUTO_SELECT_THRESHOLD
from .bill_match_engine import as_candidates as _as_candidates
from .bill_match_engine import candidate_code as _candidate_code
from .bill_match_engine import feature_gap_report, recall_candidates, select_from_candidates
from .bill_match_engine import top_candidates as _top_candidates
from .calc_engine import calc_check, calc_dispatch, calc_line_total, calc_rollup, calc_unit_price, calc_unit_rate, compute_cost
from .mcp import call_mcp_tool
from .price_engine import query_price, query_with_fallback
from .quota_engine import fetch_quota_compose, rank_schemes
from .quota_engine import scheme_id as _scheme_id
from .state import CostNodeName, normalize_region, normalize_spec, unsupported_spec_error

CostExecutionStrategy = Literal["agent", "tool", "llm"]
CostBusinessStep = Literal["bill_match", "quota_compose", "price_query", "calc"]
CostCalcOperation = Literal["unit_price", "unit_rate", "line_total", "rollup", "check"]


@dataclass(frozen=True)
class StepInterfaces:
    """Reserved handlers for a business step.

    The default implementation is tool-backed. Agent/LLM slots are intentionally
    kept as explicit entry points so the workflow can later swap the execution
    strategy without changing the public node contract.
    """

    agent: Callable[[dict[str, Any]], dict[str, Any]]
    tool: Callable[[dict[str, Any]], dict[str, Any]]
    llm: Callable[[dict[str, Any]], dict[str, Any]]


def _payload_without_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _unsupported_strategy_result(step: str, strategy: str) -> dict[str, Any]:
    return {
        "node": step,
        "strategy": strategy,
        "status": "not_implemented",
        "message": f"{step} 的 {strategy} 实现尚未接入，当前仅预留接口。",
        "supported_strategies": ["agent", "tool", "llm"],
    }


# ---- HITL gate 统一构造（四类节点共用同一套中断载荷契约）--------------------
def _review_gate(node: str, candidates: list[dict[str, Any]], question: str, *, required_fields: list[str]) -> dict[str, Any]:
    """构造 review 型 HITL 中断载荷（推荐候选，用户选一个或自行输入，可选填理由）。

    功能：把节点的推荐候选包成统一的可复核中断，供全流程停下、前端渲染选择控件。
    参数：node 触发节点名；candidates 带 score 的推荐候选；question 面向用户提示语；
        required_fields resume 时必须回传的字段。
    返回：统一结构 interrupt dict，gate_type=review，allow_manual/allow_reason 恒 True。
    """
    return {
        "gate_type": "review",
        "node": node,
        "question": question,
        "candidates": candidates,
        "allow_manual": True,
        "allow_reason": True,
        "required_fields": required_fields,
    }


def _capability_gate(node: str, question: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 capability_gap 型 HITL 中断载荷（节点能力边界外，交人工确认口径/规则）。

    功能：当请求落在节点已实现能力范围之外（如 calc 无对应公式）时停下交人工，
        而非静默返回不支持。
    参数：node 触发节点名；question 面向用户提示语；detail 可选诊断信息（缺失的公式等）。
    返回：统一结构 interrupt dict，gate_type=capability_gap。
    """
    gate = {
        "gate_type": "capability_gap",
        "node": node,
        "question": question,
        "allow_manual": True,
        "allow_reason": True,
        "required_fields": ["manual_result"],
    }
    if detail:
        gate["detail"] = detail
    return gate


# _scheme_id / extract_quota_schemes 已单源迁至 quota_engine（2026-07-12，能力 3 引擎化）；
# 流水线侧的消费方在 stages.py（workflow 打薄后 nodes 不再作转口）。


def bill_match_node(payload: dict[str, Any]) -> dict[str, Any]:
    spec_err = unsupported_spec_error(payload.get("spec"))
    if spec_err:
        return {"node": "bill_match", **spec_err}
    description = payload.get("description") or payload.get("feature") or payload.get("query")
    if not description:
        return {
            "node": "bill_match",
            "status": "awaiting_input",
            "interrupt": {
                "gate_type": "input",
                "node": "bill_match",
                "question": "请补充构件或做法描述，用于召回清单候选。",
                "required_fields": ["description"],
            },
        }

    recalled = recall_candidates(
        str(description),
        spec=payload.get("spec"),
        top_k=int(payload.get("top_k") or 10),
        code_prefixes=payload.get("code_prefixes"),
        call_tool=call_mcp_tool,
    )
    if recalled["status"] != "ok":
        return {
            "node": "bill_match",
            "status": "blocked",
            "error": recalled["error"],
        }

    candidates = recalled["candidates"]
    return {
        "node": "bill_match",
        "status": "done" if candidates else "need_review",
        "candidates": candidates,
        "count": len(candidates),
        "result": recalled["result"],
        "provenance": {
            "source": "ce-rag_match_bill_item",
            "arguments": recalled["arguments"],
        },
    }


def bill_match_agent_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("bill_match", "agent")


def bill_match_llm_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("bill_match", "llm")


def select_bill_node(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        candidates = _as_candidates(payload.get("bill_match") or {})
    candidates = [item for item in candidates if isinstance(item, dict)]

    reason = payload.get("reason")
    requested_code = payload.get("selected_code") or payload.get("code") or payload.get("manual_input")
    if isinstance(requested_code, str) and requested_code.strip():
        selected_code = requested_code.strip()
        selected = next((candidate for candidate in candidates if _candidate_code(candidate) == selected_code), None)
        result = {
            "node": "select_bill",
            "status": "done",
            "selected_code": selected_code,
            "selected": selected,
            "selection_source": "explicit_input" if selected is not None else "manual_input",
        }
        if reason:
            result["reason"] = reason
        return result

    if not candidates:
        return {
            "node": "select_bill",
            "status": "awaiting_input",
            "interrupt": {
                "gate_type": "input",
                "node": "select_bill",
                "question": "未召回到可用清单候选，请补充构件特征或手工输入清单编码。",
                "allow_manual": True,
                "allow_reason": True,
                "required_fields": ["selected_code"],
            },
        }

    threshold = float(payload.get("auto_select_threshold") or AUTO_SELECT_THRESHOLD)
    margin = float(payload.get("auto_select_margin") or AUTO_SELECT_MARGIN)
    decision = select_from_candidates(candidates, threshold=threshold, margin=margin)

    if decision["decided"]:
        result = {
            "node": "select_bill",
            "status": "done",
            "selected_code": decision["selected_code"],
            "selected": decision["selected"],
            "selection_source": "auto_confident_candidate",
            "confidence": decision["confidence"],
        }
        # 选定后顺带做特征项缺口检查（能力 2 的"少特征"提醒也覆盖正向选码路径）；
        # best-effort：payload 无描述或真值不可得时静默跳过，不阻断选码。
        feature_text = payload.get("description") or payload.get("feature")
        if isinstance(feature_text, str) and feature_text.strip():
            gap = feature_gap_report(
                decision["selected_code"], payload.get("spec"), feature_text,
                payload.get("provided_features"), call_tool=call_mcp_tool,
            )
            if gap["checked"]:
                result["required_features"] = gap["required_features"]
                result["missing_features"] = gap["missing_features"]
        return result

    return {
        "node": "select_bill",
        "status": "awaiting_input",
        "candidates": _top_candidates(decision["sorted_candidates"]),
        "interrupt": _review_gate(
            "select_bill",
            _top_candidates(decision["sorted_candidates"]),
            "清单候选需要人工复核，请选择一个候选编码，或自行输入编码（可选填理由）。",
            required_fields=["selected_code"],
        ),
    }


def select_quota_node(payload: dict[str, Any]) -> dict[str, Any]:
    """选定定额组价方案（多套可替代方案时推荐选一套；单方案自动采用）。

    功能：一个清单码可能对应多套可替代定额方案（工艺/材料档次不同）。高置信单方案
        自动采用；多方案且不够自信时停下，返回 review 中断让用户选一套或自行输入。
    参数：payload 含 schemes（候选方案列表）；resume 时含 selected_scheme / manual_input / reason。
    返回：status=done（已定方案）或 awaiting_input（需人工复核）的节点结果 dict。
    """
    schemes = payload.get("schemes")
    if not isinstance(schemes, list):
        schemes = []
    schemes = [scheme for scheme in schemes if isinstance(scheme, dict)]

    reason = payload.get("reason")
    requested = payload.get("selected_scheme") or payload.get("scheme_id")
    if isinstance(requested, str) and requested.strip():
        scheme_id = requested.strip()
        selected = next((scheme for scheme in schemes if _scheme_id(scheme) == scheme_id), None)
        result = {
            "node": "select_quota",
            "status": "done",
            "selected_scheme_id": scheme_id,
            "selected_scheme": selected,
            "selection_source": "explicit_input" if selected is not None else "manual_input",
        }
        if reason:
            result["reason"] = reason
        return result

    manual_input = payload.get("manual_input")
    if isinstance(manual_input, str) and manual_input.strip():
        result = {
            "node": "select_quota",
            "status": "done",
            "selected_scheme_id": None,
            "manual_input": manual_input.strip(),
            "selection_source": "manual_input",
        }
        if reason:
            result["reason"] = reason
        return result

    if len(schemes) <= 1:
        return {
            "node": "select_quota",
            "status": "done",
            "selected_scheme": schemes[0] if schemes else None,
            "selection_source": "auto_single_scheme",
        }

    # 多方案一律落 review 闸（方案取舍是审批级动作，不做门限自动选——此前的相似度门限对
    # schemes 无 score 时恒不生效，2026-07-12 写实删除）。引擎的 LLM 预排（能力 3 单源，
    # 与 lead 的 quota_recommend 工具同一 rank_schemes）结果附进闸载荷当「系统建议」，
    # fail-open：模型不可用则裸候选照常落闸。
    recommendation = rank_schemes(str(payload.get("feature") or payload.get("description") or ""), schemes)
    gate = _review_gate(
        "select_quota",
        _top_candidates(schemes),
        "该清单码存在多套可替代定额方案，请选择一套，或自行输入方案（可选填理由）。",
        required_fields=["selected_scheme"],
    )
    if recommendation:
        gate["recommendation"] = recommendation
    result = {
        "node": "select_quota",
        "status": "awaiting_input",
        "candidates": _top_candidates(schemes),
        "interrupt": gate,
    }
    if recommendation:
        result["recommendation"] = recommendation
    return result


def price_compose_node(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code") or payload.get("selected_code")
    if not isinstance(code, str) or not code.strip():
        return {
            "node": "price_compose",
            "status": "awaiting_input",
            "interrupt": {
                "gate_type": "input",
                "node": "price_compose",
                "question": "请先提供已确认的 9 位清单编码。",
                "required_fields": ["code"],
            },
        }

    # 取数走能力 3 引擎（spec 口径闸内置：2024 → unsupported_spec 直接透传）。
    fetched = fetch_quota_compose(
        code.strip(),
        spec=payload.get("spec"),
        region=payload.get("region"),
        on_date=payload.get("on_date"),
        call_tool=call_mcp_tool,
    )
    if fetched["status"] == "unsupported_spec":
        return {"node": "price_compose", **fetched}
    if fetched["status"] != "ok":
        return {
            "node": "price_compose",
            "status": "blocked",
            "error": fetched.get("error"),
        }
    return {
        "node": "price_compose",
        "status": "done",
        "result": fetched.get("result"),
        "provenance": {
            "source": "ce-db_price_compose",
            "arguments": fetched.get("arguments"),
        },
    }


def quota_compose_tool_node(payload: dict[str, Any]) -> dict[str, Any]:
    """Tool-backed implementation for the quota/pricing composition step."""
    return price_compose_node(payload)


def quota_compose_node(payload: dict[str, Any]) -> dict[str, Any]:
    return quota_compose_tool_node(payload)


def quota_compose_agent_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("quota_compose", "agent")


def quota_compose_llm_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("quota_compose", "llm")


def bill_get_node(payload: dict[str, Any]) -> dict[str, Any]:
    spec_err = unsupported_spec_error(payload.get("spec"))
    if spec_err:
        return {"node": "bill_get", **spec_err}
    code = payload.get("code") or payload.get("selected_code")
    if not isinstance(code, str) or not code.strip():
        return {"node": "bill_get", "status": "awaiting_input", "required_fields": ["code"]}
    tool_result = call_mcp_tool("ce-db_bill_get", {"code": code.strip(), "spec": normalize_spec(payload.get("spec"))})
    return {"node": "bill_get", "status": "done" if tool_result.get("status") == "ok" else "blocked", "result": tool_result}


def quota_get_node(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("quota_code") or payload.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"node": "quota_get", "status": "awaiting_input", "required_fields": ["quota_code"]}
    tool_result = call_mcp_tool("ce-db_quota_get", {"code": code.strip(), "region": normalize_region(payload.get("region"))})
    return {"node": "quota_get", "status": "done" if tool_result.get("status") == "ok" else "blocked", "result": tool_result}


def price_query_node(payload: dict[str, Any]) -> dict[str, Any]:
    name = payload.get("name") or payload.get("material") or payload.get("query")
    if not isinstance(name, str) or not name.strip():
        return {"node": "price_query", "status": "awaiting_input", "required_fields": ["name"]}
    # 取数走能力 4 引擎（region 口径闸内置：他省 → unsupported_region 直接透传）。
    queried = query_price(
        name.strip(),
        region=payload.get("region"),
        period=payload.get("period"),
        category=payload.get("category"),
        top_k=int(payload.get("top_k") or 10),
        call_tool=call_mcp_tool,
    )
    if queried["status"] == "unsupported_region":
        return {"node": "price_query", **queried}
    if queried["status"] == "blocked":
        return {"node": "price_query", "status": "blocked", "result": queried.get("error")}
    candidates = queried.get("candidates") or []
    tool_result = {"result": queried.get("result")}
    # 多条价格候选 → 返回结构化 review 候选，由对话层/前端让用户选材料规格或自行输入。
    # 单步语义：不进 checkpoint、不可 resume（作用域：HITL 仅完整流程闭环）。
    if len(candidates) > 1:
        return {
            "node": "price_query",
            "status": "need_review",
            "candidates": candidates,
            "result": tool_result.get("result"),
            "interrupt": _review_gate(
                "price_query",
                _top_candidates(candidates),
                "查询到多条价格候选，请选择对应的材料/规格，或自行输入价格（可选填理由）。",
                required_fields=["selected"],
            ),
            "provenance": {"source": "ce-db_price_query"},
        }
    return {"node": "price_query", "status": "done", "candidates": candidates, "result": tool_result.get("result")}


def _resource_key(quota_code: Any, resource: dict[str, Any]) -> str:
    """为一条定额工料机资源生成稳定标识（定额码::名称::规格），供询价回传定位。

    参数：quota_code —— 所属定额子目编号；resource —— 工料机资源 dict。
    返回：稳定 key 字符串。
    """
    return f"{quota_code}::{resource.get('name') or ''}::{resource.get('spec') or ''}"


def _collect_no_source_resources(quotas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """收集组价里信息价缺失（price_status=no_source）的工料机资源，待人工询价。

    功能：遍历选定组价的定额子目 → 工料机，挑出信息价未登录（no_source）的人材机，
        这些是询价 HITL 的对象（约 90% 定额料未登信息价，红线：不杜撰价）。
    参数：quotas —— 选定组价的定额子目列表（含 resources，带 price_status）。
    返回：缺价资源描述列表（含 key/quota_code/category/name/spec/unit/consumption/unit_factor）。
    """
    missing: list[dict[str, Any]] = []
    for quota in quotas:
        if not isinstance(quota, dict):
            continue
        quota_code = quota.get("quota_code")
        for resource in quota.get("resources") or []:
            if isinstance(resource, dict) and resource.get("price_status") == "no_source":
                missing.append(
                    {
                        "key": _resource_key(quota_code, resource),
                        "quota_code": quota_code,
                        "category": resource.get("category"),
                        "name": resource.get("name"),
                        "spec": resource.get("spec"),
                        "unit": resource.get("unit"),
                        "consumption": resource.get("consumption"),
                        "unit_factor": resource.get("unit_factor") or 1,
                    }
                )
    return missing


# 启发式询价（精确子串 miss → 近似料召回）已单源迁至 price_engine.query_with_fallback
# （2026-07-12，能力 4 引擎化）；price_review_node 直接消费。


def refine_price_candidates_reserved(missing: list[dict[str, Any]], *, region: str | None = None, period: str | None = None) -> list[dict[str, Any]]:
    """（预留）其它询价方式（市场价/历史价 hist_bill/LLM 估价）接口，当前未接入。

    功能：启发式召回（_heuristic_price_candidates）之外的询价来源预留槽，与 select_quota 的
        agent/llm 槽、bill_quota 的 source 同思路，未来接入其它询价通道时在此实现。
    参数：missing —— 缺价资源列表；region/period —— 询价地区与期段。
    返回：候选价方案（同启发式结构）。
    异常：NotImplementedError —— 尚未接入，调用方应回退启发式。
    """
    raise NotImplementedError("price_review 的其它询价方式尚未接入，当前用 _heuristic_price_candidates 启发式")


def _normalize_price_inputs(prices: Any) -> dict[str, dict[str, Any]]:
    """把 resume 回传的询价结果归一为 {key: {price, source?, reason?}}。

    功能：兼容 dict（key→价格或价格对象）与 list（[{key, price, ...}]）两种回传形态。
    参数：prices —— 用户回传的价格集合。
    返回：以资源 key 为键的价格对象映射。
    """
    out: dict[str, dict[str, Any]] = {}
    if isinstance(prices, dict):
        for key, value in prices.items():
            if isinstance(value, dict):
                out[str(key)] = value
            elif isinstance(value, (int, float)):
                out[str(key)] = {"price": value}
    elif isinstance(prices, list):
        for entry in prices:
            if isinstance(entry, dict) and entry.get("key") is not None:
                out[str(entry["key"])] = entry
    return out


def _apply_prices_to_quotas(quotas: list[dict[str, Any]], priced: list[dict[str, Any]]) -> None:
    """把人工询到的价格就地写回定额工料机资源（含 amount 重算、状态置 manual）。

    功能：让询价结果真正落到组价——按 key 匹配资源，写 unit_price/amount/price_status=manual。
    参数：quotas —— 选定组价定额子目列表（原地修改）；priced —— 已定价资源列表。
    返回：无（原地修改 quotas 内的 resource dict）。
    """
    by_key = {item["key"]: item for item in priced}
    for quota in quotas:
        if not isinstance(quota, dict):
            continue
        quota_code = quota.get("quota_code")
        for resource in quota.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            entry = by_key.get(_resource_key(quota_code, resource))
            if entry:
                resource["unit_price"] = entry["unit_price"]
                resource["amount"] = entry["amount"]
                resource["unit_factor"] = float(entry.get("unit_factor") or 1)
                resource["price_status"] = "manual"
                resource["price_source"] = entry.get("price_source")
                if entry.get("reason"):
                    resource["reason"] = entry["reason"]


def price_review_node(payload: dict[str, Any]) -> dict[str, Any]:
    """定额工料机询价复核（信息价缺失的人材机 → 启发式召回候选价，用户选/自输，可选填理由）。

    功能：本质同 select_quota——推荐候选、选一个或自行输入、可 resume。对象是选定组价里
        price_status=no_source 的工料机。无缺价料直接通过；有缺价料则启发式召回候选价并停下询价；
        resume 回传价格后就地写回并重算 amount，用户未提供的料如实保留缺口（红线：不杜撰价）。
    参数：payload —— 含 quotas（选定组价子目）、region/period；resume 时含 prices（询价回传）。
    返回：status=done（无缺价或已询价）或 awaiting_input（待人工询价）的节点结果 dict。
    """
    quotas = payload.get("quotas")
    if not isinstance(quotas, list):
        quotas = []
    missing = _collect_no_source_resources(quotas)
    if not missing:
        return {"node": "price_review", "status": "done", "priced": [], "missing_count": 0}

    prices = payload.get("prices")
    if prices:
        price_map = _normalize_price_inputs(prices)
        priced: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for item in missing:
            entry = price_map.get(item["key"])
            unit_price = entry.get("price") if isinstance(entry, dict) else None
            if isinstance(unit_price, (int, float)):
                factor = float(item.get("unit_factor") or 1)
                consumption = item.get("consumption")
                amount = round(consumption * float(unit_price) * factor, 2) if isinstance(consumption, (int, float)) else None
                priced.append(
                    {
                        **item,
                        "unit_price": float(unit_price),
                        "amount": amount,
                        "price_status": "manual",
                        "price_source": entry.get("source") or "manual_input",
                        "reason": entry.get("reason"),
                    }
                )
            else:
                remaining.append(item)  # 用户未补的料如实保留缺口，不编价
        _apply_prices_to_quotas(quotas, priced)
        return {"node": "price_review", "status": "done", "priced": priced, "still_missing": remaining, "missing_count": len(remaining)}

    # 初次：为每个缺价料启发式召回候选价（先精确查、miss 则近似料召回，能力 4 引擎），停下询价
    for item in missing:
        item["candidates"] = query_with_fallback(
            str(item.get("name") or ""), region=payload.get("region"), period=payload.get("period"), category=item.get("category"), top_k=5, call_tool=call_mcp_tool
        )
    return {
        "node": "price_review",
        "status": "awaiting_input",
        "missing": missing,
        "interrupt": {
            "gate_type": "review",
            "node": "price_review",
            "question": "以下定额工料机在信息价中未登录，请为其选择候选价或自行输入价格（可选填理由）；未提供的将如实保留缺口、不编价。",
            "missing": missing,
            "allow_manual": True,
            "allow_reason": True,
            "required_fields": ["prices"],
        },
    }


def price_query_agent_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("price_query", "agent")


def price_query_llm_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("price_query", "llm")


def fee_rate_lookup_node(payload: dict[str, Any]) -> dict[str, Any]:
    tool_result = call_mcp_tool(
        "ce-db_fee_rate_lookup",
        _payload_without_none(
            {
                "region": normalize_region(payload.get("region")),
                "fee_category": payload.get("fee_category"),
                "fee_name": payload.get("fee_name"),
                "applicable": payload.get("applicable"),
                "limit": int(payload.get("limit") or 20),
            }
        ),
    )
    return {"node": "fee_rate_lookup", "status": "done" if tool_result.get("status") == "ok" else "blocked", "result": tool_result}


# ---- 计算家族（能力 5）：全部单源迁至 calc_engine（2026-07-12），此处保留原节点名薄壳 ----
def unit_price_node(payload: dict[str, Any]) -> dict[str, Any]:
    return calc_unit_price(payload)


def unit_rate_node(payload: dict[str, Any]) -> dict[str, Any]:
    return calc_unit_rate(payload)


def line_total_node(payload: dict[str, Any]) -> dict[str, Any]:
    return calc_line_total(payload)


def rollup_node(payload: dict[str, Any]) -> dict[str, Any]:
    return calc_rollup(payload)


def check_node(payload: dict[str, Any]) -> dict[str, Any]:
    return calc_check(payload)


def calc_tool_node(payload: dict[str, Any]) -> dict[str, Any]:
    """Tool-backed calculation entry point (calc_engine.calc_dispatch 薄壳)。

    The calc business step is intentionally split from the concrete calculation
    operation so future agent/LLM strategies can target one stable entry point.
    """
    return calc_dispatch(payload)


def _components_from_quotas(quotas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从选定组价的定额子目工料机组装 unit_rate 所需 components（仅有价资源）。

    功能：把定额 resources 展平成 {category, name, consumption, unit_price}，供 compute/unit_rate
        算综合单价；无价（no_source 未补）资源跳过（红线：不编价，不参与计算）。
    参数：quotas —— 选定组价定额子目列表（含 resources）。
    返回：components 列表。
    """
    components: list[dict[str, Any]] = []
    for quota in quotas:
        if not isinstance(quota, dict):
            continue
        for resource in quota.get("resources") or []:
            if isinstance(resource, dict) and isinstance(resource.get("unit_price"), (int, float)):
                components.append(
                    {
                        "category": resource.get("category"),
                        "name": resource.get("name"),
                        "consumption": resource.get("consumption"),
                        "unit_price": resource.get("unit_price"),
                    }
                )
    return components


def calc_agent_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("calc", "agent")


def calc_llm_node(payload: dict[str, Any]) -> dict[str, Any]:
    return _unsupported_strategy_result("calc", "llm")


_BUSINESS_STEP_INTERFACES: dict[CostBusinessStep, StepInterfaces] = {
    "bill_match": StepInterfaces(agent=bill_match_agent_node, tool=bill_match_node, llm=bill_match_llm_node),
    "quota_compose": StepInterfaces(agent=quota_compose_agent_node, tool=quota_compose_tool_node, llm=quota_compose_llm_node),
    "price_query": StepInterfaces(agent=price_query_agent_node, tool=price_query_node, llm=price_query_llm_node),
    "calc": StepInterfaces(agent=calc_agent_node, tool=calc_tool_node, llm=calc_llm_node),
}


_NODES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "bill_match": bill_match_node,
    "select_bill": select_bill_node,
    "select_quota": select_quota_node,
    "price_compose": price_compose_node,
    "quota_compose": quota_compose_node,
    "bill_get": bill_get_node,
    "quota_get": quota_get_node,
    "price_query": price_query_node,
    "price_review": price_review_node,
    "fee_rate_lookup": fee_rate_lookup_node,
    "unit_price": unit_price_node,
    "unit_rate": unit_rate_node,
    "line_total": line_total_node,
    "compute": compute_cost,
    "rollup": rollup_node,
    "check": check_node,
    "calc": calc_tool_node,
}


def run_business_step(step: CostBusinessStep | str, payload: dict[str, Any], *, strategy: CostExecutionStrategy = "tool") -> dict[str, Any]:
    interfaces = _BUSINESS_STEP_INTERFACES.get(step)
    if interfaces is None:
        return {
            "node": str(step),
            "strategy": strategy,
            "status": "unsupported_step",
            "supported_steps": sorted(_BUSINESS_STEP_INTERFACES),
        }

    handler = getattr(interfaces, strategy, None)
    if handler is None:
        return {
            "node": str(step),
            "strategy": strategy,
            "status": "unsupported_strategy",
            "supported_strategies": ["agent", "tool", "llm"],
        }
    return handler(payload)


def run_node(node: CostNodeName | str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = _NODES.get(str(node))
    if handler is None:
        return {
            "node": str(node),
            "status": "unsupported_node",
            "supported_nodes": sorted(_NODES),
        }
    return handler(payload)
