"""Supplemental readonly PG queries for ce-db."""

from __future__ import annotations

from decimal import Decimal
from datetime import date, datetime
from typing import Any

import psycopg


def _cap_limit(limit: int, default: int = 20, max_limit: int = 100) -> int:
    return max(1, min(int(limit or default), max_limit))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in value.items()}
    return value


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _normalize_value(v) for k, v in row.items()} for row in rows]


def list_bill(
    conn: psycopg.Connection,
    *,
    spec_versions: list[str] | None = None,
    code_prefix: str | None = None,
    chapter: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    conds = ["1=1"]
    params: list[Any] = []
    if spec_versions:
        conds.append("spec_version = ANY(%s)")
        params.append(list(spec_versions))
    if code_prefix:
        conds.append("code LIKE %s")
        params.append(f"{code_prefix}%")
    if chapter:
        conds.append("chapter ILIKE %s")
        params.append(f"%{chapter}%")
    params.append(_cap_limit(limit))
    sql = (
        "SELECT code, name, unit, unit_options, feature_schema, work_content, chapter, doc_id, "
        "spec_version, region, effective_priority FROM bill_spec "
        f"WHERE {' AND '.join(conds)} ORDER BY code LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _normalize_rows(cur.fetchall())


def get_aux_table(
    conn: psycopg.Connection,
    *,
    doc_id: str,
    caption: str,
    chapter: str | None = None,
) -> dict[str, Any] | None:
    conds = ["doc_id = %s", "caption = %s"]
    params: list[Any] = [doc_id, caption]
    if chapter:
        conds.append("chapter = %s")
        params.append(chapter)
    sql = (
        "SELECT id, chapter, caption, kind, header, body, provenance, doc_id, spec_version "
        f"FROM aux_table WHERE {' AND '.join(conds)} LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else _normalize_value(row)


def list_aux_table(
    conn: psycopg.Connection,
    *,
    spec_version: str | None = None,
    doc_id: str | None = None,
    kind: str | None = None,
    chapter: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    conds = ["1=1"]
    params: list[Any] = []
    if spec_version:
        conds.append("spec_version = %s")
        params.append(spec_version)
    if doc_id:
        conds.append("doc_id = %s")
        params.append(doc_id)
    if kind:
        conds.append("kind = %s")
        params.append(kind)
    if chapter:
        conds.append("chapter ILIKE %s")
        params.append(f"%{chapter}%")
    params.append(_cap_limit(limit))
    sql = (
        "SELECT id, chapter, caption, kind, provenance, doc_id, spec_version "
        f"FROM aux_table WHERE {' AND '.join(conds)} ORDER BY doc_id, caption LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _normalize_rows(cur.fetchall())


def lookup_fee_rate(
    conn: psycopg.Connection,
    *,
    region: str = "深圳",
    fee_category: str | None = None,
    fee_name: str | None = None,
    applicable: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    conds = ["region = %s"]
    params: list[Any] = [region]
    if fee_category:
        conds.append("fee_category ILIKE %s")
        params.append(f"%{fee_category}%")
    if fee_name:
        conds.append("fee_name ILIKE %s")
        params.append(f"%{fee_name}%")
    if applicable:
        conds.append("COALESCE(applicable, '') ILIKE %s")
        params.append(f"%{applicable}%")
    params.append(_cap_limit(limit))
    sql = (
        "SELECT fee_category, fee_name, applicable, ref_low, ref_high, recommended, unit, "
        "provenance, doc_id, spec_version, region, effective_priority "
        f"FROM fee_rate WHERE {' AND '.join(conds)} "
        "ORDER BY fee_category, fee_name LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _normalize_rows(cur.fetchall())


def get_price_composition(
    conn: psycopg.Connection,
    *,
    composite: str | None = None,
    spec_version: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conds = ["1=1"]
    params: list[Any] = []
    if composite:
        conds.append("composite ILIKE %s")
        params.append(f"%{composite}%")
    if spec_version:
        conds.append("spec_version = %s")
        params.append(spec_version)
    params.append(_cap_limit(limit, default=50, max_limit=200))
    sql = (
        "SELECT composite, kind, seq, component, note, provenance, doc_id, spec_version, "
        "region, effective_priority "
        f"FROM price_composition WHERE {' AND '.join(conds)} ORDER BY composite, seq LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _normalize_rows(cur.fetchall())


def list_quota(
    conn: psycopg.Connection,
    *,
    region: str = "深圳",
    chapter: str | None = None,
    quota_code_prefix: str | None = None,
    bill_code: str | None = None,
    bill_spec_versions: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    params: list[Any] = [region]
    if bill_code:
        conds = ["q.region = %s", "m.bill_code = %s"]
        params.append(bill_code)
        if bill_spec_versions:
            conds.append("m.bill_spec_version = ANY(%s)")
            params.append(list(bill_spec_versions))
        if chapter:
            conds.append("q.chapter ILIKE %s")
            params.append(f"%{chapter}%")
        if quota_code_prefix:
            conds.append("q.quota_code LIKE %s")
            params.append(f"{quota_code_prefix}%")
        params.append(_cap_limit(limit))
        sql = (
            "SELECT q.quota_code, q.name, q.unit, q.base_price, q.labor_cost, q.material_cost, "
            "q.machine_cost, q.chapter, q.doc_id, q.spec_version, q.region, m.bill_code, "
            "m.bill_spec_version, m.confidence, m.source "
            "FROM quota_item q JOIN bill_quota_map m "
            "ON q.quota_code = m.quota_code AND q.doc_id = m.quota_doc_id "
            f"WHERE {' AND '.join(conds)} "
            "ORDER BY m.confidence DESC NULLS LAST, q.quota_code LIMIT %s"
        )
    else:
        conds = ["region = %s"]
        if chapter:
            conds.append("chapter ILIKE %s")
            params.append(f"%{chapter}%")
        if quota_code_prefix:
            conds.append("quota_code LIKE %s")
            params.append(f"{quota_code_prefix}%")
        params.append(_cap_limit(limit))
        sql = (
            "SELECT quota_code, name, unit, base_price, labor_cost, material_cost, machine_cost, "
            "chapter, doc_id, spec_version, region "
            f"FROM quota_item WHERE {' AND '.join(conds)} "
            "ORDER BY quota_code LIMIT %s"
        )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _normalize_rows(cur.fetchall())


def lookup_resource(
    conn: psycopg.Connection,
    *,
    name: str,
    category: str | None = None,
    unit: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    conds = ["name ILIKE %s"]
    params: list[Any] = [f"%{name}%"]
    if category:
        conds.append("category = %s")
        params.append(category)
    if unit:
        conds.append("unit = %s")
        params.append(unit)
    params.append(_cap_limit(limit))
    sql = (
        "SELECT id, res_code, name, spec, category, unit, doc_id "
        f"FROM resource WHERE {' AND '.join(conds)} ORDER BY length(name), name LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return _normalize_rows(cur.fetchall())
