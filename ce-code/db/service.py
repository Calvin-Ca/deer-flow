"""DB-facing orchestration boundary for ce-code."""

from __future__ import annotations

from datetime import date
from typing import Any

import config
from cost import query as cost_query
from db import dao


class DbService:
    """Expose readonly structured data lookups for ce-db."""

    def bill_get(self, code: str, *, spec: str) -> dict[str, Any] | None:
        cfg = config.resolve_spec(spec)
        with cost_query.connect() as conn:
            return cost_query.get_bill(conn, code, spec_versions=cfg["bill_spec_versions"])

    def bill_list(
        self,
        *,
        spec: str,
        code_prefix: str | None = None,
        chapter: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        cfg = config.resolve_spec(spec)
        with cost_query.connect() as conn:
            return dao.list_bill(
                conn,
                spec_versions=cfg["bill_spec_versions"],
                code_prefix=code_prefix,
                chapter=chapter,
                limit=limit,
            )

    def quota_get(self, *, region: str, code: str) -> dict[str, Any] | None:
        with cost_query.connect() as conn:
            return cost_query.get_quota(conn, region, code)

    def quota_list(
        self,
        *,
        region: str = "深圳",
        spec: str | None = None,
        bill_code: str | None = None,
        chapter: str | None = None,
        quota_code_prefix: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        bill_spec_versions = None
        if spec:
            bill_spec_versions = config.resolve_spec(spec)["bill_spec_versions"]
        with cost_query.connect() as conn:
            return dao.list_quota(
                conn,
                region=region,
                chapter=chapter,
                quota_code_prefix=quota_code_prefix,
                bill_code=bill_code,
                bill_spec_versions=bill_spec_versions,
                limit=limit,
            )

    def price_query(
        self,
        *,
        name: str,
        region: str = "深圳",
        period: str | None = None,
        category: str | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        with cost_query.connect() as conn:
            rows = cost_query.query_resource_price(
                conn,
                name,
                region=region,
                period=period,
                category=category,
                top_k=top_k,
            )
        return {"name": name, "region": region, "period": period, "count": len(rows), "results": rows}

    def price_suggest(
        self,
        *,
        name: str,
        region: str = "深圳",
        category: str | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        with cost_query.connect() as conn:
            rows = cost_query.suggest_resource_prices(conn, name, region=region, category=category, top_k=top_k)
        return {"name": name, "region": region, "category": category, "count": len(rows), "suggestions": rows}

    def price_compose(
        self,
        *,
        code: str,
        spec: str,
        region: str = "深圳",
        on_date: date | None = None,
    ) -> dict[str, Any] | None:
        cfg = config.resolve_spec(spec)
        with cost_query.connect() as conn:
            return cost_query.compose_price(
                conn,
                region,
                code,
                on_date,
                spec_versions=cfg["bill_spec_versions"],
            )

    def fee_rate_lookup(
        self,
        *,
        region: str = "深圳",
        fee_category: str | None = None,
        fee_name: str | None = None,
        applicable: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with cost_query.connect() as conn:
            return dao.lookup_fee_rate(
                conn,
                region=region,
                fee_category=fee_category,
                fee_name=fee_name,
                applicable=applicable,
                limit=limit,
            )

    def price_composition_get(
        self,
        *,
        composite: str | None = None,
        spec_version: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with cost_query.connect() as conn:
            return dao.get_price_composition(
                conn,
                composite=composite,
                spec_version=spec_version,
                limit=limit,
            )

    def aux_table_get(
        self,
        *,
        doc_id: str,
        caption: str,
        chapter: str | None = None,
    ) -> dict[str, Any] | None:
        with cost_query.connect() as conn:
            return dao.get_aux_table(conn, doc_id=doc_id, caption=caption, chapter=chapter)

    def aux_table_list(
        self,
        *,
        spec_version: str | None = None,
        doc_id: str | None = None,
        kind: str | None = None,
        chapter: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with cost_query.connect() as conn:
            return dao.list_aux_table(
                conn,
                spec_version=spec_version,
                doc_id=doc_id,
                kind=kind,
                chapter=chapter,
                limit=limit,
            )

    def resource_lookup(
        self,
        *,
        name: str,
        category: str | None = None,
        unit: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with cost_query.connect() as conn:
            return dao.lookup_resource(
                conn,
                name=name,
                category=category,
                unit=unit,
                limit=limit,
            )
