"""Shared contracts for the application-level CE cost workflow."""

from __future__ import annotations

from typing import Any, Literal

DEFAULT_REGION = "深圳"
DEFAULT_SPEC = "2013"

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
