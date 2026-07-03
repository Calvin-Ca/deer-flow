"""编排层 —— 构件描述 → 候选召回 → 选码 → 组价取数（CostAgent P1 串链）。

把三件既成件串成一条：
  ``cost_client.bill_match``（:8100 召回清单候选） → ``selection.select_code``（LLM 候选内选码 + 红线兜底）
  → ``cost_client.price_compose``（:8100 取定额工料机含量 + 信息价单价）。

P1 范围：只到「选码 + 组价取数」，**不组装综合单价**（那是 P2，确定性公式、LLM 不算钱）。

红线透传：选不出码（need_review / code=None）→ 不调组价、原样返回选码结果（转 HITL）；spec=2013 组价数据
未就绪 → price_compose 返 501，本层**捕获并降级**为 ``price=None`` + ``price_status`` 说明（选码结果仍有价值，
不因组价未就绪丢弃）；bill_match 的 spec 400 / 知识服务 503 等不在本层吞、上抛由 router 映射状态码。
"""
from __future__ import annotations

from typing import Any

import requests

from common import cost_client
from cost.guards import audit_cost_result
from cost.pricing import UnitPriceInput, compute_unit_price
from cost.selection import select_code


def _price_unit_prices(price: dict[str, Any], rates: dict[str, Any]) -> None:
    """对组价取数结果的每条定额，按定额基价 + 费率算综合单价，原地挂到各 quota（确定性、不入 LLM）。

    参数：price —— ``price_compose`` 结果（含 quotas[].labor_cost/material_cost/machine_cost 定额基价）；
      rates —— 费率块（management_fee_rate/profit_rate/risk_rate/fee_base/tax_rate）。
    返回：None（原地为每条 quota 增 ``unit_price``：算成则为 compute_unit_price 结果，
      定额缺人材机基价则 ``{"status": "missing_base"}``——绝不拿缺失基价杜撰）。
    口径：以**定额基价**（净人材机，不含信息价价差调整）为人材机费；价差调整属后续精算，不在此步。
    """
    for quota in price.get("quotas", []):
        labor = quota.get("labor_cost")
        material = quota.get("material_cost")
        machine = quota.get("machine_cost")
        if labor is None or material is None or machine is None:
            quota["unit_price"] = {"status": "missing_base"}
            continue
        inp = UnitPriceInput(
            labor_cost=float(labor),
            material_cost=float(material),
            machine_cost=float(machine),
            management_fee_rate=rates["management_fee_rate"],
            profit_rate=rates["profit_rate"],
            risk_rate=rates.get("risk_rate", 0.0),
            fee_base=rates["fee_base"],
            tax_rate=rates.get("tax_rate"),
        )
        quota["unit_price"] = compute_unit_price(inp)


def compose(
    description: str,
    spec: str,
    region: str,
    top_k: int = 10,
    rates: dict[str, Any] | None = None,
    spec_source: str = "user",
) -> dict[str, Any]:
    """构件描述 → 候选 → 选码 → 组价取数，组装端到端结果。

    参数：
        description —— 构件/做法描述；spec —— 国标版本（2013/2024）；region —— 地区（如「深圳」）；
        spec_source —— 版本来源：``user``（用户显式给定）/ ``default``（缺省归一到深圳·2013，
        PRD §4.0/C-05 不反问）；进 ``meta.caliber`` 供输出层做首答口径声明。
        top_k —— 候选召回数；
        选码模型不在本层选：唯一的 LLM 步 ``select_code`` 自定桶 B 32b（§9.3），本层不透传；取数
        （bill_match/price_compose）与算价（compute_unit_price）均无 LLM。
        rates —— 可选费率块（management_fee_rate/profit_rate/risk_rate/fee_base/tax_rate）；给定且组价取数 ok
        时为每条定额算综合单价（P2，确定性不入 LLM），缺则维持 P1 行为（仅选码 + 取数，不算钱）。
    返回：
        ``{description, spec, region, candidates_count, selection, code, price, price_status, meta}``：
        - selection —— select_code 全量结果（code/confidence/reason/need_review/alternatives）；
        - meta.guard —— ③ 校验闸 GuardReport（C-01/02/03，verdict/tier/caliber_pure/provenance_complete）；
        - 选不出码（code=None）→ price=None、price_status="skipped(need_review)"，不调组价；
        - spec=2013 组价未就绪（compose 501）→ price=None、price_status="未就绪(2013 组价数据未就绪)"；
        - 正常 → price=price_compose 结果（含工料机含量 + 信息价单价 + 小计，未命中价的资源 no_source）。
        bill_match 的 spec 400 / 知识服务 503、LLM 错误经异常上抛（router 映射状态码）。
    """
    # ① 召回候选（spec 400 / 知识服务 503 上抛由 router 映射）
    match_resp = cost_client.bill_match(description, spec, top_k=top_k)
    candidates = match_resp.get("candidates", [])

    # ② LLM 候选内选码 + 红线兜底（select_code 自定桶 B 32b，§9.3）
    selection = select_code(description, candidates)
    code = selection.get("code")

    result: dict[str, Any] = {
        "description": description,
        "spec": spec,
        "region": region,
        "candidates_count": len(candidates),
        "selection": selection,
        "code": code,
        "price": None,
        "price_status": None,
    }

    # 口径声明（§4.0/T9-1）：地区·版本 + 版本来源（default=缺省归一，输出层据此做首答声明）。
    caliber = {"declared": f"{region}·{spec}", "region": region, "spec": spec, "spec_source": spec_source}

    # ③ 选不出码 → 不调组价，转 HITL
    if not code:
        result["price_status"] = "skipped(need_review)"
        result["meta"] = {"guard": audit_cost_result(result, spec=spec, region=region).as_meta(),
                          "caliber": caliber}
        return result

    # ④ 组价取数；spec=2013 未就绪（501）降级透传，不丢选码结果
    try:
        price = cost_client.price_compose(region, code, spec)
        # ⑤（可选）给定费率块 → 末步对每条定额算综合单价（确定性、不入 LLM）
        if rates is not None:
            _price_unit_prices(price, rates)
        result["price"] = price
        result["price_status"] = "ok"
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 501:
            result["price_status"] = "未就绪(该国标版本组价数据未就绪，仅返回选码)"
        else:
            raise  # 404/503 等交 router 映射
    # ③ 校验闸（C-01/02/03 → GuardReport，进 meta.guard，对齐 norm 侧同契约）
    result["meta"] = {"guard": audit_cost_result(result, spec=spec, region=region).as_meta(),
                      "caliber": caliber}
    return result
