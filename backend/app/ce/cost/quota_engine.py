"""定额方案推荐引擎——能力 3 的单源底座（lead 工具与 workflow 节点共用，2026-07-12）。

架构定案：workflow 直接装配能力件（与能力 2 的 ``bill_match_engine`` 同款模式）——
- **lead 面**：``quota_recommend`` 工具（本模块底部薄壳）——已编清单项 → 定额组价方案推荐；
- **workflow 面**：``nodes.price_compose_node``（取数）与 ``nodes.select_quota_node``（多方案闸）
  复用本模块的 fetch / extract / rank——流水线上的多方案闸载荷自动附带同一 LLM 预排建议。
原 quota-recommend 子智能体随之退役（能力智能收进引擎，暴露形态由消费方自选）。

分层：**取数与提取是确定性的**（ce-db_price_compose + schemes 解析），**多方案排序是 LLM 单次
结构化调用**（``rank_schemes``，可注入替换；默认走 config 默认模型，env ``CE_QUOTA_RANK_MODEL``
可指到 32B——分桶用模的预留钩子；``CE_QUOTA_RANK_ENABLED=0`` 关闭）。LLM 排序 **fail-open**：
模型不可用/输出不合法 → 无推荐、候选照常返回，绝不阻断也绝不代替人拍板——多方案的最终选定
在对话里归用户、在流水线里归 review 闸（模型距离原则：LLM 只当预裁员，不当决策者）。

spec 过 agent 面口径闸（``state.unsupported_spec_error``，默认仅 2013）。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from langchain.tools import tool

from .mcp import call_mcp_tool
from .state import normalize_region, normalize_spec, unsupported_spec_error

_McpCall = Callable[[str, dict[str, Any]], dict[str, Any]]
_RankCall = Callable[[str, list[dict[str, Any]]], dict[str, Any] | None]

_RANK_ENABLED_ENV = "CE_QUOTA_RANK_ENABLED"
_RANK_MODEL_ENV = "CE_QUOTA_RANK_MODEL"
_THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def scheme_id(scheme: dict[str, Any]) -> str | None:
    """从定额方案候选里取稳定标识（scheme_id / id / code 任一；供选择回传与推荐对齐）。"""
    for key in ("scheme_id", "id", "code"):
        value = scheme.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_quota_schemes(price_compose_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从 price_compose 返回里提取可替代定额方案列表（无多方案时空列表 → 单方案降级不闸）。"""
    if not isinstance(price_compose_result, dict):
        return []
    inner = price_compose_result.get("result")
    if not isinstance(inner, dict):
        return []
    for key in ("schemes", "options", "plans", "alternatives"):
        raw = inner.get(key)
        if isinstance(raw, list):
            return [scheme for scheme in raw if isinstance(scheme, dict)]
    return []


def fetch_quota_compose(code: str, spec: str | None = None, region: str | None = None, on_date: Any = None, *, call_tool: _McpCall | None = None) -> dict[str, Any]:
    """按已确认清单码取组价数据（``ce-db_price_compose``：可组定额 + 工料机含量 + 信息价）。

    返回：``{status: ok|error|unsupported_spec, result?, arguments?, ...}``；spec 先过口径闸。
    """
    spec_err = unsupported_spec_error(spec)
    if spec_err:
        return spec_err
    call = call_tool or call_mcp_tool
    arguments: dict[str, Any] = {
        "code": str(code).strip(),
        "spec": normalize_spec(spec),
        "region": normalize_region(region),
    }
    if on_date is not None:
        arguments["on_date"] = on_date
    tool_result = call("ce-db_price_compose", arguments)
    if tool_result.get("status") != "ok":
        return {"status": "error", "error": tool_result, "arguments": arguments}
    return {"status": "ok", "result": tool_result.get("result"), "arguments": arguments}


def _rank_enabled() -> bool:
    return os.environ.get(_RANK_ENABLED_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _parse_rank_output(text: str, valid_ids: set[str]) -> dict[str, Any] | None:
    """从模型输出里解析 {scheme_id, rationale}（剥 <think>、抓首个 JSON 块、校验 id 在候选内）。"""
    cleaned = _THINK_RE.sub("", str(text or ""))
    m = _JSON_RE.search(cleaned)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    sid = str(obj.get("scheme_id") or "").strip()
    if sid not in valid_ids:
        return None  # 推荐了候选外的方案 id → 视为无效（不许编造，与选码同款红线）
    rationale = str(obj.get("rationale") or "").strip()
    return {"recommended_scheme_id": sid, "rationale": rationale}


def _default_llm_rank(feature: str, schemes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """默认 LLM 排序实现：config 默认模型单次结构化调用（env 可换模型/关闭）。"""
    from deerflow.models import create_chat_model  # 延迟 import：纯函数路径不拖模型依赖

    model_name = os.environ.get(_RANK_MODEL_ENV) or None
    model = create_chat_model(name=model_name)
    brief = json.dumps(
        [{"scheme_id": scheme_id(s), "summary": {k: v for k, v in s.items() if k not in ("components", "quotas")}} for s in schemes],
        ensure_ascii=False,
    )[:4000]
    prompt = (
        "你是造价定额方案比选助手。给定构件特征与多套可替代定额方案，选出最适用的一套。\n"
        f"构件特征：{feature or '（未提供）'}\n候选方案：{brief}\n"
        '只输出一行 JSON（不要多余文字）：{"scheme_id": "候选内的方案id", "rationale": "排序理由,必须落在特征与定额适用范围的对应上"}'
    )
    response = model.invoke(prompt)
    content = getattr(response, "content", response)
    valid = {sid for sid in (scheme_id(s) for s in schemes) if sid}
    return _parse_rank_output(str(content), valid)


def rank_schemes(feature: str, schemes: list[dict[str, Any]], *, llm_call: _RankCall | None = None) -> dict[str, Any] | None:
    """多方案 LLM 预排（fail-open：关闭/异常/输出无效一律 None，候选与闸照常）。

    返回：``{recommended_scheme_id, rationale}`` 或 None。仅在 ≥2 套方案时调用才有意义。
    """
    if len(schemes) < 2 or not _rank_enabled():
        return None
    try:
        call = llm_call or _default_llm_rank
        return call(feature, schemes)
    except Exception:  # noqa: BLE001 —— 预排是增强不是依赖，模型不可用不阻断
        return None


def recommend_quota(
    code: str,
    feature: str = "",
    spec: str | None = None,
    region: str | None = None,
    *,
    call_tool: _McpCall | None = None,
    llm_call: _RankCall | None = None,
) -> dict[str, Any]:
    """能力 3 统一入口：已编清单项 → 组价取数 + 方案提取 + 多方案 LLM 预排。

    返回（按情形）：
    - ``unsupported_spec`` / ``blocked``（取数失败，透传 error）；
    - ``done`` + ``selection_source=no_alternatives|auto_single_scheme``（0/1 套方案，直接采用）；
    - ``need_review`` + ``schemes`` + 可选 ``recommendation``（多套方案：预排建议仅供参考，
      最终选定归调用方——对话里归用户，流水线里归 review 闸）。
    组价数据（quotas/含量/价格）在 ``compose`` 字段原样透传，price_status 等由消费方转述。
    """
    if not str(code or "").strip():
        return {"status": "awaiting_input", "required_fields": ["code"], "message": "请先提供已确认的 9 位清单编码"}
    fetched = fetch_quota_compose(code, spec, region, call_tool=call_tool)
    if fetched["status"] == "unsupported_spec":
        return fetched
    if fetched["status"] != "ok":
        return {"status": "blocked", "error": fetched.get("error"), "arguments": fetched.get("arguments")}

    schemes = extract_quota_schemes(fetched)
    base = {
        "spec": normalize_spec(spec),
        "region": normalize_region(region),
        "compose": fetched.get("result"),
        "schemes": schemes,
        "provenance": {"compose": "ce-db_price_compose", "arguments": fetched.get("arguments")},
    }
    if len(schemes) <= 1:
        return {
            "status": "done",
            "selected_scheme": schemes[0] if schemes else None,
            "selection_source": "auto_single_scheme" if schemes else "no_alternatives",
            **base,
        }
    recommendation = rank_schemes(feature, schemes, llm_call=llm_call)
    result = {"status": "need_review", **base}
    if recommendation:
        result["recommendation"] = recommendation
    return result


def quota_recommend(
    code: str,
    feature: str = "",
    spec: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """给已编制好的清单项推荐定额组价方案（取数确定性、多方案附 LLM 预排建议）。

    Quota-scheme recommender for a confirmed bill item. It fetches composable
    quota schemes with labor/material/machine contents and info prices from
    ce-db, auto-adopts when there is at most one scheme, and for multiple
    alternative schemes returns all candidates plus an advisory ranking with
    rationale — the final choice stays with the user. Not for picking a bill
    code from features (use bill_match) and it does not compute prices
    (use cost_calc or the cost workflow).

    Args:
        code: Confirmed bill code, 9 or 12 digits.
        feature: Optional project feature description, used to ground the ranking rationale.
        spec: Bill standard version. Only 2013 (Shenzhen caliber) is supported; omit to use it by default.
        region: Region for quota and price data. Defaults to Shenzhen.
    """
    return recommend_quota(code=code, feature=feature, spec=spec, region=region, call_tool=call_mcp_tool)


quota_recommend_tool = tool("quota_recommend", parse_docstring=True)(quota_recommend)

__all__ = [
    "extract_quota_schemes", "fetch_quota_compose", "quota_recommend", "quota_recommend_tool",
    "rank_schemes", "recommend_quota", "scheme_id",
]
