"""Provenance 信封 + 原语适配器 —— HITL 图节点的「取数 + 来源」统一表面。

设计 §5.1：所有原语统一返回 ``{step, status, result, provenance{...}}`` 信封，**来源是字段不是散文**
（设计原则 1）。本模块**不重写**底层原语，而是「原地包一层」：调现有
``cost_client.bill_match`` / ``selection.select_code`` / ``cost_client.price_compose``，
把它们已有的结构化输出（candidate.score/chapter、select_code.confidence/alternatives、
resource.price_status）裹进信封，供图节点与前端依据卡直接渲染。

诚实边界：现有 ``price_compose`` 未必返回「信息价文件名+行号」级 ``source_ref``。本期用现有可得字段
（期号/区域/price_status）best-effort 填，缺的标 ``TODO(knowledge-layer)``——知识层补抽精确 source_ref
是设计 §9 步1 的后续，不阻塞图骨架。
"""
from __future__ import annotations

from typing import Any

from common import cost_client
from cost.selection import select_code

# 信封 status 取值（设计 §5.1）
STATUS_OK = "ok"
STATUS_NEED_REVIEW = "need_review"
STATUS_NO_SOURCE = "no_source"
STATUS_ERROR = "error"


def envelope(
    step: str,
    status: str,
    result: dict[str, Any],
    *,
    source_type: str,
    source_ref: str | None,
    confidence: float | None = None,
    alternatives: list[Any] | None = None,
) -> dict[str, Any]:
    """构造 §5.1 通用 provenance 信封。

    参数：
        step —— 原语名（list_match / pick_quota / query_price …）。
        status —— ok / need_review / no_source / error。
        result —— 原语结果字段（见 §5.1 各原语 result）。
        source_type —— spec_clause / quota_lib / price_book / online / user_input。
        source_ref —— 来源引用（条文号 / 定额号 / 信息价期+行 / URL / "用户输入"）；缺则 None。
        confidence —— 置信度 0~1，缺则 None。
        alternatives —— 备选项列表（结构化，非散文），缺则空。
    返回：``{step, status, result, provenance{source_type, source_ref, confidence, alternatives}}``。
    """
    return {
        "step": step,
        "status": status,
        "result": result,
        "provenance": {
            "source_type": source_type,
            "source_ref": source_ref,
            "confidence": confidence,
            "alternatives": alternatives or [],
        },
    }


def list_match(
    description: str,
    spec: str,
    top_k: int,
    llm_url: str,
    model_id: str,
) -> dict[str, Any]:
    """编码原语 —— ``bill_match`` 召回 + ``select_code`` 候选内选码，包成信封。

    参数：description —— 构件/做法描述；spec —— 国标版本（2013/2024）；top_k —— 候选召回数；
      llm_url / model_id —— Qwen3 vLLM 配置。
    返回：信封，``result={code, name, 项目特征归类, unit}``；status = select_code need_review/选不出码→
      need_review，否则 ok；provenance.source_type=spec_clause，confidence 取 select_code，
      alternatives 取候选内次优（带 name/confidence，均来自候选、非杜撰）。
    红线透传：选码红线（不造码/低置信转人工）在 ``select_code`` 已兜底，本层只搬运不二次判断。
    """
    match_resp = cost_client.bill_match(description, spec, top_k=top_k)
    candidates = match_resp.get("candidates", [])
    by_code = {str(c.get("code", "")).strip(): c for c in candidates if c.get("code")}

    sel = select_code(description, candidates, llm_url, model_id)
    code = sel.get("code")
    chosen = by_code.get(str(code).strip()) if code else None

    result = {
        "code": code,
        "name": (chosen or {}).get("name"),
        "项目特征归类": (chosen or {}).get("feature") or (chosen or {}).get("chapter"),
        "unit": (chosen or {}).get("unit"),
        "reason": sel.get("reason"),
    }
    # 备选：select_code 已校验 alternatives ⊆ 候选，这里补 name/章节供依据卡展开。
    alternatives = [
        {"code": a, "name": by_code.get(str(a).strip(), {}).get("name")}
        for a in sel.get("alternatives", [])
    ]
    status = STATUS_NEED_REVIEW if (sel.get("need_review") or not code) else STATUS_OK
    source_ref = (chosen or {}).get("chapter") if chosen else None
    if source_ref is None:
        source_ref = "TODO(knowledge-layer): 清单条文号待知识层补抽"

    return envelope(
        "list_match",
        status,
        result,
        source_type="spec_clause",
        source_ref=source_ref,
        confidence=sel.get("confidence"),
        alternatives=alternatives,
    )


def from_price_compose(region: str, code: str, spec: str) -> dict[str, Any]:
    """组价取数原语 —— 一次 ``price_compose`` 调用，拆出「定额块」与「信息价材料块」两类信封。

    一次取数同时含定额工料机含量与信息价单价，故合并一次 ``price_compose`` 调用避免重复打知识层；
    供 ``quota`` 节点用定额信封、``price_query`` 节点用材料信封。

    参数：region —— 地区；code —— 已确认的 9 位清单编码；spec —— 国标版本。
    返回：``{"quota_envelope": <信封>, "materials": [<材料 price 块>], "raw": <price_compose 原始结果>}``：
      - quota_envelope —— step=pick_quota，result={quotas:[{子目号, 人材机基价}]}，source_type=quota_lib；
      - materials —— 每条 ``{raw, std, unit, price:{value, status, provenance}}``；命中信息价 status=ok、
        未命中 status=no_source（设计：缺价不杜撰、逐项例外闸据此触发）。
    异常：``price_compose`` 的 HTTPError（501 未就绪 / 404 / 503）原样上抛，由 compose_node 决定降级或冒泡。
    """
    raw = cost_client.price_compose(region, code, spec)
    quotas = raw.get("quotas", []) or []

    # ── 定额信封 ──
    quota_items = [
        {
            "子目号": q.get("quota_code") or q.get("code") or q.get("子目号"),
            "name": q.get("name"),
            "labor_cost": q.get("labor_cost"),
            "material_cost": q.get("material_cost"),
            "machine_cost": q.get("machine_cost"),
        }
        for q in quotas
    ]
    quota_status = STATUS_OK if quota_items else STATUS_NEED_REVIEW
    quota_envelope = envelope(
        "pick_quota",
        quota_status,
        {"quotas": quota_items, "count": len(quota_items)},
        source_type="quota_lib",
        source_ref=f"{region}定额 / spec={spec} / code={code}",
        confidence=None,
        alternatives=[],
    )

    # ── 信息价材料块（逐资源）──
    materials: list[dict[str, Any]] = []
    for q in quotas:
        for r in (q.get("resources", []) or []):
            price_status = (r.get("price_status") or "").strip().lower()
            ok = price_status == "ok"
            unit_price = r.get("unit_price") if r.get("unit_price") is not None else r.get("price")
            materials.append(
                {
                    "raw": r.get("name") or r.get("raw"),
                    "std": r.get("std") or r.get("name"),
                    "unit": r.get("unit"),
                    "price": {
                        "value": unit_price if ok else None,
                        "status": STATUS_OK if ok else STATUS_NO_SOURCE,
                        "provenance": {
                            "source_type": "price_book",
                            "source_ref": (
                                r.get("price_source")
                                or "TODO(knowledge-layer): 信息价期号+行号待知识层补抽"
                            ),
                            "price_status": price_status or "unknown",
                        },
                    },
                }
            )

    return {"quota_envelope": quota_envelope, "materials": materials, "raw": raw}
