"""Shared contracts for the application-level CE cost workflow."""

from __future__ import annotations

import os
from typing import Any, Literal

DEFAULT_REGION = "深圳"
DEFAULT_SPEC = "2013"

# ── Agent 面 spec 口径闸（2026-07-12 产品裁定：系统仅深圳·2013）──────────────────
# normalize_spec 保留 2024 归一能力（历史数据/评测后门要用），但 agent 面（bill_match 工具、
# workflow 选码节点）只放行允许清单内的 spec——点名 2024 在服务层硬拒（提示词之外的第二道
# 防串库纵深，与 ce-rag 侧 CE_RAG_AGENT_STANDARDS 同款）。评测进程需要 2024 时用 env 放开。
_AGENT_SPECS_ENV = "CE_COST_AGENT_SPECS"
_AGENT_SPECS_DEFAULT = "2013"


def agent_allowed_specs() -> set[str]:
    """Agent 面允许的清单口径版本集合（env ``CE_COST_AGENT_SPECS`` 覆盖，默认仅 2013）。"""
    raw = os.environ.get(_AGENT_SPECS_ENV) or _AGENT_SPECS_DEFAULT
    return {s.strip() for s in raw.split(",") if s.strip()}


def unsupported_spec_error(spec: str | None) -> dict[str, Any] | None:
    """spec 归一后不在 agent 面允许清单 → 统一错误载荷（体面话术供 agent 原样透传）；支持则 None。"""
    normalized = normalize_spec(spec)
    if normalized in agent_allowed_specs():
        return None
    return {
        "status": "unsupported_spec",
        "spec": normalized,
        "message": f"不支持的清单口径版本 {normalized!r}——本系统仅支持深圳·2013 版规范，不提供该版本的选码/组价数据",
    }

CostNodeName = Literal[
    "bill_match",
    "select_bill",
    "select_quota",
    "price_compose",
    "quota_compose",
    "bill_get",
    "quota_get",
    "price_query",
    "price_review",
    "fee_rate_lookup",
    "unit_price",
    "unit_rate",
    "line_total",
    "compute",
    "rollup",
    "check",
    "calc",
]


def normalize_spec(spec: str | None) -> str:
    value = str(spec or DEFAULT_SPEC).strip()
    if value in {"13", "2013版", "GB50854-2013", "gb50854-2013"}:
        return "2013"
    if value in {"24", "2024版", "GB50854-2024", "gb50854-2024"}:
        return "2024"
    return value or DEFAULT_SPEC


def normalize_region(region: str | None) -> str:
    return str(region or DEFAULT_REGION).strip() or DEFAULT_REGION


def normalize_feature_items(
    *,
    feature: str | None,
    features: list[dict[str, Any]] | None,
    quantity: float | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if features:
        for raw in features:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            description = item.get("description") or item.get("feature") or item.get("name")
            if description:
                item["description"] = str(description)
                items.append(item)
    elif feature:
        item = {"description": feature}
        if quantity is not None:
            item["quantity"] = quantity
        items.append(item)
    return items
