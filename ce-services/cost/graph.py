"""可中断组价状态机（langgraph）—— HITL 全 13 步图（设计 §3.1）。

链路（**多构件外层循环**）：``setup →〔每件：list_match → feature_gate →（缺特征?回环 list_match）→ list_gate
       →（有码?compose → quota_gate → price_gate → quantity_gate / 无码 skip）→ advance →（还有件?回 list_match）〕
       → rates_gate（项目级·给每件算综合单价）→ params_gate → rollup → done``。
- **多构件**：``current_item`` 标当前在办件，per-item 闸只动 ``items[current_item]``（``_put_item`` 写回保留其余件）；
  ``advance`` 推进到下一件、办完才进项目级收尾（费率/参数/汇总一套管全单）。单构件退化为列表只一项。
- **compute / gate 双拆**：原语调用（list_match / compose，含 LLM 与知识层取数）放「compute 节点」，
  暂停闸放「gate 节点」。因 langgraph 的 ``interrupt()`` 在 resume 时**会从节点头部重跑**——把昂贵且
  非确定性的 LLM 调用单独放上游 compute 节点（节点间有 checkpoint，跑且仅跑一次），gate 节点只读 state +
  interrupt（幂等、便宜），避免 resume 重跑 LLM 漂移（原则 3）。后段费率/参数/汇总均为**确定性算钱**
  （``compute_unit_price`` / ``rollup_cost``，无 LLM），故 interrupt 与计算同节点、resume 重跑无漂移。
- 每个节点完成都往 ``state["events"]`` 追加一条 provenance 事件（前端依据卡数据源），无论是否暂停。
- 是否跳闸的判断全在 ``gates`` 代码里，弱模型不驱动流程（§1.2 / §10）。

后段三节点（§8 综合单价费率 / §10⑪§12 项目级费用 / §13 末尾 review）：费率/参数走录入闸（缺政策数则停、
不杜撰），rollup 始终暂停做总造价复核（§6「末尾 review 始终暂停」）。
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from common.config import HITL_CONFIDENCE_TAU, LLM_MODEL_ID, LLM_URL
from cost import clarify, gates, provenance
from cost.pricing import RollupInput, UnitPriceInput, compute_unit_price, rollup_cost
from cost.state import (
    CostTaskState,
    audit_entry,
    gate_event,
    lock_value,
    override_entry,
    provenance_event,
)

logger = logging.getLogger("ce-services.cost.hitl")

TOP_K = 10
# 特征澄清最多回环轮次（PRD FR-P02/EH-04）：超过则不再反问、落 list_gate 确认闸兜底，避免无解死循环。
MAX_CLARIFY_ROUNDS = 2


def _item(state: CostTaskState) -> dict[str, Any]:
    """取当前在办构件 item（items[current_item]）；缺则按 feature 初始化一个空 item（多构件外层循环用 current_item 选位）。"""
    items = state.get("items") or [{"feature": state.get("feature")}]
    idx = min(state.get("current_item", 0), len(items) - 1)
    return dict(items[idx])


def _put_item(state: CostTaskState, item: dict[str, Any]) -> list[dict[str, Any]]:
    """把更新后的 item 写回 items[current_item]，**保留其余构件**（多构件写回原语，替代整表覆盖 ``[item]``）。

    参数：state —— 当前状态；item —— 改后的当前构件。返回：整张 items 列表（仅当前位被替换）。
    单构件时退化为「列表只一项、替换它」，与旧行为等价。
    """
    items = [dict(it) for it in (state.get("items") or [])]
    idx = state.get("current_item", 0)
    if 0 <= idx < len(items):
        items[idx] = item
    else:
        items.append(item)
    return items


# ── 节点 ──

def setup_node(state: CostTaskState) -> dict[str, Any]:
    """setup 闸（§3①/§6）：必填项（spec/region）缺失才暂停收集，齐了直接过。

    参数：state —— 当前任务状态。
    返回：状态增量。spec/region 缺失 → ``interrupt(input_payload)`` 收集
      spec/region/period/price_source；齐全（start 已带）→ 不暂停、直接置 running（§6「已有默认值自动过」）。
    """
    missing = [f for f in ("spec_version", "region") if not state.get(f)]
    if missing:
        data = interrupt(
            gates.input_payload(
                "setup",
                "请确认组价口径",
                [
                    {"key": "spec_version", "type": "enum", "label": "国标版本",
                     "options": ["2013", "2024"], "required": True},
                    {"key": "region", "type": "text", "label": "地区", "default": "深圳"},
                    {"key": "period", "type": "month", "label": "信息价期号（年月）", "required": False},
                    {"key": "price_source", "type": "enum", "label": "信息价来源",
                     "options": ["local", "online", "manual"], "default": "local"},
                ],
            )
        )
        return {
            "spec_version": data.get("spec_version") or state.get("spec_version"),
            "region": data.get("region") or state.get("region") or "深圳",
            "period": data.get("period") or state.get("period"),
            "price_source": data.get("price_source") or state.get("price_source") or "local",
            "status": "running",
        }
    return {
        "region": state.get("region") or "深圳",
        "price_source": state.get("price_source") or "local",
        "status": "running",
    }


def list_match_node(state: CostTaskState) -> dict[str, Any]:
    """编码 compute（§3②）：bill_match 召回 + select_code 选码（含 LLM），结果入 item，**不暂停**。

    参数：state。返回：item.code 挂 list_match 信封 + item.missing_features（缺特征探测）+ provenance 事件，
      并清 needs_rematch（回环已消费）。LLM 选码在此唯一一次调用（与 gate 拆开，resume 不重跑）。
    缺特征探测（PRD FR-P02/EH-04）：仅 need_review（低置信/选不出码）且澄清未超轮次时抽缺口供下游特征澄清闸；
      高置信直配 / 超轮次 → 空（不澄清）。LLM 不可靠时 extract 返回空，degrade 到现有行为。
    """
    item = _item(state)
    env = provenance.list_match(item.get("feature"), state["spec_version"], TOP_K)
    item["code"] = {"envelope": env, "value": env["result"].get("code"), "locked": False}
    # clarify_rounds 存 item（多构件各自计澄清预算，互不挪用）。
    if env["status"] == provenance.STATUS_NEED_REVIEW and (item.get("clarify_rounds") or 0) < MAX_CLARIFY_ROUNDS:
        hints = [env["result"].get("name")] + [a.get("name") for a in env["provenance"].get("alternatives", [])]
        item["missing_features"] = clarify.extract_missing_features(item.get("feature"), hints, LLM_URL, LLM_MODEL_ID)
    else:
        item["missing_features"] = []
    return {"items": _put_item(state, item), "events": [provenance_event(env, paused=False)], "needs_rematch": False}


def feature_gate_node(state: CostTaskState) -> dict[str, Any]:
    """特征澄清闸（§3② 前置 / PRD FR-P02、EH-04）：描述缺关键特征→反问补全→回填后回环重匹配；齐→直接过。

    参数：state。返回：有缺口 → interrupt 收特征值、把补充拼回 item.feature、clarify_rounds+1、置 needs_rematch
      （路由回 list_match 重走门控，呼应 PRD §8.2「澄清回填后重走门控」）；无缺口 → 空增量（路由直走 list_gate）。
    缺口由上游 list_match 探测存 item.missing_features；本闸只读 state + interrupt（幂等、resume 不重跑 LLM，原则 3）。
    """
    item = _item(state)
    missing = item.get("missing_features") or []
    if not missing:
        return {}  # 描述充分 / 已澄清够 → 不停，路由走 list_gate

    data = interrupt(
        gates.input_payload(
            "feature",
            "请补全构件关键特征（缺这些信息无法唯一选码/套定额）",
            [{"key": m["key"], "type": "text", "label": m.get("label") or m["key"], "required": True}
             for m in missing],
            context={"feature": item.get("feature"),
                     "why": [{"label": m.get("label"), "why": m.get("why")} for m in missing]},
        )
    )
    # resume：把用户补的特征拼回描述、清缺口、标回环重匹配。
    parts = [f"{m.get('label') or m['key']}={(data or {}).get(m['key'])}"
             for m in missing if (data or {}).get(m["key"]) not in (None, "")]
    added = "，".join(parts)
    item["feature"] = (item.get("feature") or "") + (("，" + added) if added else "")
    item["missing_features"] = []
    rounds = (item.get("clarify_rounds") or 0) + 1
    item["clarify_rounds"] = rounds  # 澄清轮次记 item（多构件各自计）
    idx = state.get("current_item", 0)
    out: dict[str, Any] = {
        "items": _put_item(state, item),
        # 有补充才回环重匹配；用户全空（拒答）则不回环、落 list_gate 确认闸兜底，不空转。
        "needs_rematch": bool(added),
        "audit_log": [audit_entry("feature_clarify", "input", {"added": added, "round": rounds, "item": idx}, by="user")],
        "events": [gate_event("feature_gate", paused=True, detail={"added": added, "round": rounds, "item": idx})],
    }
    if added:
        out["overrides"] = [override_entry("feature", idx, added, by="user")]
    return out


def list_gate_node(state: CostTaskState) -> dict[str, Any]:
    """编码确认闸（§3②/§6）：高置信唯一码自动过，否则暂停等确认；resume 后钉码（原则 3）。

    参数：state。返回：item.code 钉成 ``lock_value`` + 审计/override 记录。
    """
    item = _item(state)
    env = item["code"]["envelope"]
    pause = gates.should_pause_coding(env, HITL_CONFIDENCE_TAU)
    if pause:
        decision = interrupt(gates.confirm_payload("list_coding", env, "请确认清单编码"))
        value, prov, action = gates.apply_confirm_decision(env, decision)
        by = "user"
    else:
        value, prov, action = env["result"].get("code"), env["provenance"], "auto_pass"
        by = "model"

    item["code"] = lock_value(value, prov, by=by)
    idx = state.get("current_item", 0)
    out: dict[str, Any] = {
        "items": _put_item(state, item),
        "audit_log": [audit_entry("list_coding", action, {"code": value, "item": idx}, by=by)],
        # 门控决策入依据时间线：自动过时带 confidence/τ，让「为什么没问你」可见（≥τ 故自动采纳）。
        "events": [gate_event("list_gate", paused=pause,
                              confidence=env.get("provenance", {}).get("confidence"),
                              tau=HITL_CONFIDENCE_TAU, provenance=prov, detail={"code": value})],
    }
    if by == "user" and action in ("manual_override", "select_alternative"):
        out["overrides"] = [override_entry("code", idx, value, by="user")]
    # 选不出码：该构件跳过取数（_has_code 路由 skip→advance），不硬编、不阻断其余构件；
    # 整单是否 blocked 由 done_node 据「是否有任一构件算出综合合价」统一判（多构件不被单个坏件拖死）。
    return out


def compose_node(state: CostTaskState) -> dict[str, Any]:
    """组价取数 compute（§3④⑥）：对已钉 code 调 price_compose，拆定额块 + 信息价材料块入 item，**不暂停**。

    参数：state。返回：item 挂 quota 信封与 materials；spec 未就绪（501）降级为空块、status 标未就绪（§10 透传）。
    """
    item = _item(state)
    code = (item.get("code") or {}).get("value")
    region, spec = state["region"], state["spec_version"]
    try:
        bundle = provenance.from_price_compose(region, code, spec)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 501:
            item["quota"] = {"status": "未就绪"}
            item["materials"] = []
            return {
                "items": _put_item(state, item),
                "status": "running",
                "events": [{"step": "from_price_compose", "status": "未就绪",
                            "provenance": {"source_ref": f"spec={spec} 组价数据未就绪"}, "paused": False}],
            }
        raise

    item["quota"] = {"envelope": bundle["quota_envelope"], "locked": False}
    item["materials"] = bundle["materials"]
    return {
        "items": _put_item(state, item),
        "events": [provenance_event(bundle["quota_envelope"], paused=False)],
    }


def quota_gate_node(state: CostTaskState) -> dict[str, Any]:
    """定额确认闸（§3④/§6）：唯一子目自动过，多子目暂停确认；**无定额映射→告知+可补录基价+可放弃**（P0）。

    参数：state。返回：item.quota 钉值 + quota_basis + 审计。无定额块（未就绪/无 envelope）→ 透传跳过。
    缺定额映射（quotas 空）不再弹空白确认卡：改弹「缺定额」录入闸——补齐人材机基价→钉用户基价续算综合单价；
      三项留空放弃→诚实标缺口（quota_basis=None，下游 ``no_pricing`` 据此终止、不算假总价，§6 数据缺失应停）。
    """
    item = _item(state)
    quota = item.get("quota") or {}
    env = quota.get("envelope")
    if env is None:  # compose 未就绪/无定额块，跳过本闸
        return {"status": state.get("status", "running")}

    quotas = env.get("result", {}).get("quotas", [])
    idx = state.get("current_item", 0)

    # ── 缺定额映射（P0）：告知 + 可补录人材机基价 + 可放弃；不弹空白确认卡、不往下算假总价 ──
    if not quotas:
        code = (item.get("code") or {}).get("value")
        data = interrupt(gates.quota_missing_payload(env, code, item.get("feature")))
        basis = gates.build_manual_basis(data)
        if basis is None:
            # 放弃补录：诚实标缺口，不钉子目、不造基价（下游 no_pricing 据 quota_basis 空终止）。
            item["quota"] = lock_value(None, {"source_type": "quota_lib",
                                              "source_ref": "无映射定额子目（用户未补录）"}, by="user")
            item["quota_basis"] = None
            return {
                "items": _put_item(state, item),
                "audit_log": [audit_entry("quota", "skip_no_quota", {"code": code, "item": idx}, by="user")],
                "events": [gate_event("quota_gate", paused=True, detail={"code": code, "no_quota": True})],
            }
        # 补录：钉用户给的子目 + 人材机基价，续算综合单价（quota_basis 同知识层定额块，rates_gate 直接可算）。
        item["quota"] = lock_value(basis["子目号"], {"source_type": "user_input",
                                   "source_ref": basis["source_ref"], "confidence": None}, by="user")
        item["quota_basis"] = basis
        return {
            "items": _put_item(state, item),
            "audit_log": [audit_entry("quota", "manual_input", {"子目号": basis["子目号"], "item": idx}, by="user")],
            "overrides": [override_entry("quota", idx, basis["子目号"], by="user")],
            "events": [gate_event("quota_gate", paused=True,
                                  provenance={"source_type": "user_input", "source_ref": basis["source_ref"]},
                                  detail={"子目号": basis["子目号"]})],
        }

    # ── 有子目：唯一自动过 / 多子目人工确认 ──
    paused = len(quotas) > 1
    if paused:
        decision = interrupt(gates.confirm_payload("quota", env, "请确认套用定额子目"))
        value, prov, action = gates.apply_confirm_decision(env, decision)
        by = "user"
    else:
        value = quotas[0].get("子目号")
        prov, action, by = env["provenance"], "auto_pass", "model"

    item["quota"] = lock_value(value, prov, by=by)
    # 保留所选子目的定额基价（人材机净价）供 §8 综合单价计算；越界子目无对应基价 → None（下游 missing_base 透传）。
    item["quota_basis"] = next((q for q in quotas if str(q.get("子目号")) == str(value)), None)
    return {
        "items": _put_item(state, item),
        "audit_log": [audit_entry("quota", action, {"子目号": value}, by=by)],
        # 自动过=唯一子目，无 LLM 置信（tau=None）；停闸=多子目人工选。
        "events": [gate_event("quota_gate", paused=paused, provenance=prov, detail={"子目号": value})],
    }


def price_gate_node(state: CostTaskState) -> dict[str, Any]:
    """信息价逐项例外闸（§3⑥/§7）：命中价自动过，缺价/状态非 ok 的逐项暂停录入。

    参数：state。返回：item.materials 逐条补价 + 缺价项的 override/审计。
    多个缺价材料 → 节点内多次 interrupt（resume 逐个回填，langgraph 按序匹配）；命中的绝不问（§7 反例）。
    """
    item = _item(state)
    materials = item.get("materials") or []
    overrides: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    for idx, m in enumerate(materials):
        price = m.get("price", {})
        if gates.should_pause_price(price):
            data = interrupt(
                gates.input_payload(
                    f"price_item:{idx}",
                    f"信息价缺失，请录入「{m.get('std') or m.get('raw')}」单价",
                    [{"key": "value", "type": "number", "label": "单价（元）", "required": True}],
                    context={"raw": m.get("raw"), "std": m.get("std"), "unit": m.get("unit"),
                             "category": m.get("category"), "spec": m.get("spec"),
                             "consumption": m.get("consumption"), "price_status": price.get("status")},
                )
            )
            val = (data or {}).get("value")
            m["price"] = {
                "value": val,
                "status": "user_input",
                "provenance": {"source_type": "user_input", "source_ref": "用户录入"},
            }
            overrides.append(override_entry("price", idx, val, by="user"))
            audits.append(audit_entry("price_query", "manual_override", {"material": m.get("std"), "value": val}, by="user"))

    item["materials"] = materials
    out: dict[str, Any] = {"items": _put_item(state, item)}
    if overrides:
        out["overrides"] = overrides
        out["audit_log"] = audits
    else:
        out["audit_log"] = [audit_entry("price_query", "auto_pass", {"materials": len(materials)})]
    return out


def quantity_gate_node(state: CostTaskState) -> dict[str, Any]:
    """工程量录入闸（§3⑨/§6/§2 步骤 9）：Q 已给定自动过，缺则停闸录入；钉 Q 供综合合价计算。

    参数：state。返回：item.quantity 钉值 + 审计/override。无定额基价（未就绪/选错码无映射）→ 跳过
      （此时综合单价必为 missing_base，问 Q 无意义）。Q 是用户输入、非本系统计算（BIM 算量范围外），
      缺则显式录入、绝不静默按 1 计（原则 3 / §10 不杜撰）。
    """
    item = _item(state)
    if item.get("quota_basis") is None:  # 无基价，Q 喂下去也算不出综合单价，跳过本闸
        return {"status": state.get("status", "running")}

    quantity = item.get("quantity")
    if gates.should_pause_quantity(quantity):
        data = interrupt(
            gates.input_payload(
                "quantity",
                "请录入工程量（清单数量）",
                [{"key": "quantity", "type": "number", "label": "工程量 Q", "required": True}],
                context={"feature": item.get("feature"), "code": (item.get("code") or {}).get("value"),
                         "unit": (item.get("quota_basis") or {}).get("单位")},
            )
        )
        quantity = (data or {}).get("quantity")
        action, by = "input", "user"
    else:
        action, by = "auto_pass", "model"

    item["quantity"] = quantity
    idx = state.get("current_item", 0)
    out: dict[str, Any] = {
        "items": _put_item(state, item),
        "audit_log": [audit_entry("quantity", action, {"quantity": quantity, "item": idx}, by=by)],
        # 自动过=start 已预供 Q（缺口 1：让 quantity_gate 也进依据时间线）。
        "events": [gate_event("quantity_gate", paused=(by == "user"), detail={"quantity": quantity, "item": idx})],
    }
    if by == "user":
        out["overrides"] = [override_entry("quantity", idx, quantity, by="user")]
    # 缺 Q：该构件综合合价标 missing_quantity（rates_gate），不静默按 1 算、也不阻断其余构件。
    return out


def _unit_price_for(basis: dict[str, Any] | None, rates: dict[str, Any], quantity: float) -> dict[str, Any]:
    """按定额基价 + 费率 + 工程量算单条综合单价/综合合价（确定性，复用 ``compute_unit_price``）。

    参数：basis —— 所选定额子目的人材机净价 ``{labor_cost, material_cost, machine_cost}`` 或 None；
      rates —— 费率块（管理费/利润/风险率 + 取费基数）；quantity —— 工程量 Q（清单数量，>0，由 quantity_gate 录入）。
    返回：``compute_unit_price`` 结果（含 ``total_price = 综合单价 × Q``）；缺基价（手填子目/未就绪）→
      ``{"status":"missing_base"}``（不杜撰基价）。
    口径：以定额基价为人材机费，综合单价**不含税**（税金在 §13 rollup 一次性计，GB 50500 §2.0.9）；
      Q 必由上游 quantity_gate 显式给定，不在此默认（默认 1 会算出错误总价）。
    """
    if not basis or any(basis.get(k) is None for k in ("labor_cost", "material_cost", "machine_cost")):
        return {"status": "missing_base"}
    inp = UnitPriceInput(
        labor_cost=float(basis["labor_cost"]),
        material_cost=float(basis["material_cost"]),
        machine_cost=float(basis["machine_cost"]),
        management_fee_rate=rates["management_fee_rate"],
        profit_rate=rates["profit_rate"],
        risk_rate=rates.get("risk_rate", 0.0),
        fee_base=rates["fee_base"],
        quantity=float(quantity),  # 工程量 Q：综合合价 = 综合单价 × Q（§2 步骤 9）
        tax_rate=None,  # 综合单价不含税，税金在 rollup
    )
    return compute_unit_price(inp)


def rates_gate_node(state: CostTaskState) -> dict[str, Any]:
    """综合单价费率录入闸（§3⑧/§6）·**项目级**：费率一套管全单，钉率后给**每条构件**算综合单价/综合合价。

    参数：state。返回：state.rates 钉值 + 每个 item.unit_price（compute_unit_price 结果）+ 审计/override/逐件事件。
    费率是政策数（库内无）、整单一套，缺则停闸录入（gates.should_pause_rates）、齐则自动过；算钱确定性、不入 LLM。
    本节点在外层循环跑完所有构件后执行一次（current_item 已越界），故遍历全 items、不依赖 _item。
    """
    rates = state.get("rates")
    paused = gates.should_pause_rates(rates)
    if paused:
        rates = interrupt(
            gates.input_payload(
                "rates",
                "请录入综合单价费率（库内无管理费/利润，须按工程类别给定）",
                [
                    {"key": "management_fee_rate", "type": "number", "label": "管理费率（%）", "required": True},
                    {"key": "profit_rate", "type": "number", "label": "利润率（%）", "required": True},
                    {"key": "risk_rate", "type": "number", "label": "风险费率（%）", "default": 0},
                    {"key": "fee_base", "type": "enum", "label": "取费基数（labor=人工费 / labor_machine=人工+机械 / lmm=人材机）",
                     "options": ["labor", "labor_machine", "lmm"], "required": True},
                ],
            )
        )

    items = [dict(it) for it in (state.get("items") or [])]
    events: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        quantity = it.get("quantity")
        basis = it.get("quota_basis")
        # 有基价却缺 Q → missing_quantity（不静默按 1 计）；无基价（未就绪/选错码）→ missing_base。
        up = ({"status": "missing_quantity"} if basis is not None and quantity is None
              else _unit_price_for(basis, rates, quantity))
        it["unit_price"] = up
        events.append({"step": f"compute_unit_price[{i}]", "status": up.get("status", "ok"),
                       "provenance": up.get("provenance"), "result": up,
                       "paused": paused and i == 0, "auto_pass": not paused})
    by = "user" if paused else "model"
    out: dict[str, Any] = {
        "rates": rates,
        "items": items,
        "audit_log": [audit_entry("unit_price", "input" if paused else "auto_pass",
                                  {"rates": rates, "items": len(items)}, by=by)],
        "events": events,
    }
    if paused:
        out["overrides"] = [override_entry("rates", 0, rates, by="user")]
    return out


def params_gate_node(state: CostTaskState) -> dict[str, Any]:
    """项目级费用录入闸（§3⑩⑪⑫/§6）：有参数自动过、缺税金率停闸录入措施/其他/规费/税金率。

    参数：state。返回：state.params 钉值 + 审计/override。税金率是政策数须显式给（gates.should_pause_params），
      措施/其他/规费可缺省 0。本节点只采集、不算钱（汇总在 rollup）。
    """
    params = state.get("params")
    paused = gates.should_pause_params(params)
    if paused:
        params = interrupt(
            gates.input_payload(
                "params",
                "请录入项目级费用（措施/其他/规费）与税金率",
                [
                    {"key": "measure_fee", "type": "number", "label": "措施项目费（元）", "default": 0},
                    {"key": "other_fee", "type": "number", "label": "其他项目费（元）", "default": 0},
                    {"key": "fee_levy", "type": "number", "label": "规费（元）", "default": 0},
                    {"key": "tax_rate", "type": "number", "label": "税金率（%）", "required": True},
                ],
            )
        )
    by = "user" if paused else "model"
    out: dict[str, Any] = {
        "params": params,
        "audit_log": [audit_entry("project_params", "input" if paused else "auto_pass", {"params": params}, by=by)],
        # 自动过=已带 params（税率已给）（缺口 1：让 params_gate 也进依据时间线）。
        "events": [gate_event("params_gate", paused=paused, detail={"params": params})],
    }
    if paused:
        out["overrides"] = [override_entry("params", 0, params, by="user")]
    return out


def _compute_rollup(state: CostTaskState) -> dict[str, Any]:
    """汇总各 item 综合合价 + 项目级费用 → 总造价（确定性，复用 ``rollup_cost``）。

    参数：state（含 items[].unit_price 与 params）。返回：rollup_cost 结果（含 missing 计数提示）。
    分部分项合价 = Σ 各 item 综合合价（total_price）；缺综合单价（missing_base）的 item 计入 missing、不计金额（不杜撰）。
    """
    subtotal = 0.0
    missing = 0
    for it in state.get("items") or []:
        up = it.get("unit_price") or {}
        total = up.get("total_price")
        if total is not None:
            subtotal += total
        else:
            missing += 1
    params = state.get("params") or {}
    result = rollup_cost(RollupInput(
        subtotal=subtotal,
        measure_fee=float(params.get("measure_fee") or 0),
        other_fee=float(params.get("other_fee") or 0),
        fee_levy=float(params.get("fee_levy") or 0),
        tax_rate=params.get("tax_rate"),
    ))
    result["missing_unit_price_items"] = missing
    return result


def rollup_node(state: CostTaskState) -> dict[str, Any]:
    """末尾 review（§3⑬/§6）：确定性汇总总造价后**始终暂停**复核，resume(approve) → done。

    参数：state。返回：state.rollup（总造价明细）+ status=done + 审计。
    §6「末尾 review 始终暂停」：总造价定稿前必看，故无条件 interrupt（汇总确定性、resume 重跑无漂移）。
    """
    rollup = _compute_rollup(state)
    interrupt({
        "gate_type": "review",
        "node": "rollup",
        "title": "请复核总造价",
        "rollup": rollup,
        "actions": ["approve"],
    })
    return {
        "rollup": rollup,
        "status": "done",
        "audit_log": [audit_entry("rollup", "final_review", {"total": rollup.get("total"),
                                  "pre_tax_total": rollup.get("pre_tax_total")}, by="user")],
        "events": [{"step": "rollup", "status": "ok", "provenance": rollup.get("provenance"),
                    "result": rollup, "paused": True}],
    }


def advance_node(state: CostTaskState) -> dict[str, Any]:
    """多构件外层循环推进：当前构件处理完，current_item+1（路由 ``_after_advance`` 据此回 list_match 或进 rates_gate）。"""
    return {"current_item": (state.get("current_item", 0) + 1)}


def no_pricing_node(state: CostTaskState) -> dict[str, Any]:
    """无可算价终态（§6 数据缺失应停 / P0）：全部构件都无定额基价（缺映射且未补录）→ blocked。

    参数：state。返回：status=blocked + rollup.blocked_reason（原因 + 涉及构件清单）+ 审计/事件。
    与 rates→params→rollup 正常收尾链路的本质区别：本节点**直达 END**，绝不弹费率/税率闸、绝不产出
      虚构总造价——数据缺失时诚实告知无法组价到总价，而非算一个只由项目费/税金堆出来的误导数字。
    """
    unresolved = [
        {"item": i, "code": (it.get("code") or {}).get("value"), "feature": it.get("feature"),
         "reason": "无定额映射且未补录人材机基价"}
        for i, it in enumerate(state.get("items") or [])
        if not gates.basis_complete((it or {}).get("quota_basis"))
    ]
    reason = "无可用定额数据，无法组价到总价（缺定额映射、用户未补录基价）"
    return {
        "status": "blocked",
        "rollup": {"blocked_reason": reason, "unpriceable_items": unresolved},
        "audit_log": [audit_entry("no_pricing", "blocked", {"unpriceable": unresolved}, by="model")],
        "events": [{"step": "no_pricing", "status": "blocked",
                    "provenance": {"source_type": "quota_lib", "source_ref": "缺定额映射，未算总价（不虚构）"},
                    "result": {"blocked_reason": reason, "unpriceable_items": unresolved}, "paused": False}],
    }


def done_node(state: CostTaskState) -> dict[str, Any]:
    """收尾（§3⑬）：有任一构件算出综合合价 → done；全无（都选不出码/未就绪/缺 Q）→ blocked，不掩盖缺口。

    多构件下单个坏件不拖死整单（坏件在 rollup 计入 missing），仅当**所有**构件都无综合合价才整单 blocked。
    """
    items = state.get("items") or []
    any_ok = any((it.get("unit_price") or {}).get("total_price") is not None for it in items)
    return {"status": "done" if any_ok else "blocked"}


def _after_feature(state: CostTaskState) -> str:
    """feature_gate 后路由：刚补了特征 → 回 list_match 重匹配重门控（回环，§8.2）；否则继续 list_gate。"""
    return "list_match" if state.get("needs_rematch") else "list_gate"


def _has_code(state: CostTaskState) -> str:
    """list_gate 后路由：已钉到码 → 取数 compose；否则该构件跳过（skip→advance，处理下一构件）。"""
    item = _item(state)
    return "compose" if (item.get("code") or {}).get("value") else "skip"


def _after_advance(state: CostTaskState) -> str:
    """advance 后路由：还有未办构件 → 回 list_match 办下一件；全办完 → 项目级收尾 or 无价终止。

    全办完时：至少一件拿到定额基价（可算价）→ rates_gate 走费率/参数/汇总；无一件可算（都缺定额映射且
      未补录基价）→ no_pricing 终止（不问费率、不算假总价，P0 数据缺失应停）。
    """
    items = state.get("items") or []
    if state.get("current_item", 0) < len(items):
        return "list_match"
    return "rates_gate" if gates.has_priceable_item(items) else "no_pricing"


def build_graph(checkpointer: Any):
    """组装并编译可中断组价图。

    参数：checkpointer —— langgraph checkpointer（SqliteSaver），按 thread_id 持久化、支撑暂停恢复。
    返回：编译后的图（``invoke``/``get_state`` 用 ``config={"configurable":{"thread_id":task_id}}``）。
    """
    g = StateGraph(CostTaskState)
    g.add_node("setup", setup_node)
    g.add_node("list_match", list_match_node)
    g.add_node("feature_gate", feature_gate_node)
    g.add_node("list_gate", list_gate_node)
    g.add_node("compose", compose_node)
    g.add_node("quota_gate", quota_gate_node)
    g.add_node("price_gate", price_gate_node)
    g.add_node("quantity_gate", quantity_gate_node)
    g.add_node("advance", advance_node)
    g.add_node("rates_gate", rates_gate_node)
    g.add_node("params_gate", params_gate_node)
    g.add_node("rollup", rollup_node)
    g.add_node("no_pricing", no_pricing_node)
    g.add_node("done", done_node)

    g.add_edge(START, "setup")
    g.add_edge("setup", "list_match")
    g.add_edge("list_match", "feature_gate")
    # 特征澄清回环：补了特征→回 list_match 重匹配；齐→list_gate（PRD §8.2 澄清回填后重走门控）。
    g.add_conditional_edges("feature_gate", _after_feature, {"list_match": "list_match", "list_gate": "list_gate"})
    # 选不出码 → skip 到 advance（跳过该构件取数，办下一件）；有码 → compose 取数。
    g.add_conditional_edges("list_gate", _has_code, {"compose": "compose", "skip": "advance"})
    g.add_edge("compose", "quota_gate")
    g.add_edge("quota_gate", "price_gate")
    g.add_edge("price_gate", "quantity_gate")
    g.add_edge("quantity_gate", "advance")
    # 多构件外层循环：还有未办构件→回 list_match；全办完→rates_gate 项目级收尾 or no_pricing 无价终止。
    g.add_conditional_edges("advance", _after_advance,
                            {"list_match": "list_match", "rates_gate": "rates_gate", "no_pricing": "no_pricing"})
    g.add_edge("rates_gate", "params_gate")
    g.add_edge("params_gate", "rollup")
    g.add_edge("rollup", "done")
    g.add_edge("no_pricing", END)  # 无可算价：直达 END，不走费率/参数/汇总、不产虚构总价（P0）
    g.add_edge("done", END)

    return g.compile(checkpointer=checkpointer)
