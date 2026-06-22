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
from cost.selection import select_code


def compose(
    description: str,
    spec: str,
    region: str,
    llm_url: str,
    model_id: str,
    top_k: int = 10,
) -> dict[str, Any]:
    """构件描述 → 候选 → 选码 → 组价取数，组装端到端结果。

    参数：
        description —— 构件/做法描述；spec —— 国标版本（2013/2024，必填）；region —— 地区（如「深圳」）；
        llm_url / model_id —— Qwen3 vLLM 配置；top_k —— 候选召回数。
    返回：
        ``{description, spec, region, candidates_count, selection, code, price, price_status}``：
        - selection —— select_code 全量结果（code/confidence/reason/need_review/alternatives）；
        - 选不出码（code=None）→ price=None、price_status="skipped(need_review)"，不调组价；
        - spec=2013 组价未就绪（compose 501）→ price=None、price_status="未就绪(2013 组价数据未就绪)"；
        - 正常 → price=price_compose 结果（含工料机含量 + 信息价单价 + 小计，未命中价的资源 no_source）。
        bill_match 的 spec 400 / 知识服务 503、LLM 错误经异常上抛（router 映射状态码）。
    """
    # ① 召回候选（spec 400 / 知识服务 503 上抛由 router 映射）
    match_resp = cost_client.bill_match(description, spec, top_k=top_k)
    candidates = match_resp.get("candidates", [])

    # ② LLM 候选内选码 + 红线兜底
    selection = select_code(description, candidates, llm_url, model_id)
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

    # ③ 选不出码 → 不调组价，转 HITL
    if not code:
        result["price_status"] = "skipped(need_review)"
        return result

    # ④ 组价取数；spec=2013 未就绪（501）降级透传，不丢选码结果
    try:
        result["price"] = cost_client.price_compose(region, code, spec)
        result["price_status"] = "ok"
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 501:
            result["price_status"] = "未就绪(该国标版本组价数据未就绪，仅返回选码)"
        else:
            raise  # 404/503 等交 router 映射
    return result
