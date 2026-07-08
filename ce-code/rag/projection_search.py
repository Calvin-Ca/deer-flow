"""Text projection search over structured PG tables for rag-facing retrieval."""

from __future__ import annotations

import json
import re
from typing import Any

from cost import query as cost_query


def _terms(query: str) -> list[str]:
    query = (query or "").strip()
    if not query:
        return []
    parts = [p for p in re.split(r"[\s,，。；;、/]+", query) if p]
    if len(parts) == 1 and len(query) > 4:
        parts.extend({query[i:i + 2] for i in range(len(query) - 1)})
    return [p for p in parts if p]


def _score(text: str, query: str, terms: list[str]) -> float:
    score = 0.0
    if query and query in text:
        score += 5.0
    for term in terms:
        if term in text:
            score += 1.0 + min(text.count(term), 3) * 0.2
    return score


def search_aux_table(
    query: str,
    *,
    spec_version: str | None = None,
    chapter: str | None = None,
    kind: str | None = None,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Search aux_table rows by projected text."""
    conds = ["1=1"]
    params: list[Any] = []
    if spec_version:
        conds.append("spec_version = %s")
        params.append(spec_version)
    if chapter:
        conds.append("chapter ILIKE %s")
        params.append(f"%{chapter}%")
    if kind:
        conds.append("kind = %s")
        params.append(kind)
    sql = (
        "SELECT chapter, caption, kind, header, body, provenance, doc_id, spec_version "
        f"FROM aux_table WHERE {' AND '.join(conds)} LIMIT 200"
    )
    terms = _terms(query)
    rows: list[dict[str, Any]] = []
    with cost_query.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            text = " ".join(
                [
                    row.get("caption") or "",
                    row.get("chapter") or "",
                    json.dumps(row.get("header") or [], ensure_ascii=False),
                    json.dumps(row.get("body") or [], ensure_ascii=False),
                ]
            )
            score = _score(text, query, terms)
            if score <= 0:
                continue
            row["score"] = score
            row["snippet"] = f"{row.get('caption') or ''} / {row.get('chapter') or ''}"
            rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[: max(1, min(top_k, 50))]


def search_price_rule(
    query: str,
    *,
    spec_version: str | None = None,
    region: str | None = None,
    top_k: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """Search price_composition and fee_rate via projected text."""
    terms = _terms(query)
    result = {"price_composition": [], "fee_rate": []}
    with cost_query.connect() as conn, conn.cursor() as cur:
        comp_sql = (
            "SELECT composite, kind, seq, component, note, provenance, doc_id, spec_version "
            "FROM price_composition "
            + ("WHERE spec_version = %s " if spec_version else "")
            + "LIMIT 100"
        )
        cur.execute(comp_sql, [spec_version] if spec_version else [])
        for row in cur.fetchall():
            text = " ".join(
                [
                    row.get("composite") or "",
                    row.get("component") or "",
                    row.get("note") or "",
                ]
            )
            score = _score(text, query, terms)
            if score <= 0:
                continue
            row["score"] = score
            row["snippet"] = f"{row.get('composite')} -> {row.get('component')}"
            result["price_composition"].append(row)

        fee_conds = ["1=1"]
        fee_params: list[Any] = []
        if region:
            fee_conds.append("region = %s")
            fee_params.append(region)
        fee_sql = (
            "SELECT fee_category, fee_name, applicable, ref_low, ref_high, recommended, unit, "
            "provenance, doc_id, spec_version, region FROM fee_rate "
            f"WHERE {' AND '.join(fee_conds)} LIMIT 100"
        )
        cur.execute(fee_sql, fee_params)
        for row in cur.fetchall():
            text = " ".join(
                [
                    row.get("fee_category") or "",
                    row.get("fee_name") or "",
                    row.get("applicable") or "",
                ]
            )
            score = _score(text, query, terms)
            if score <= 0:
                continue
            row["score"] = score
            row["snippet"] = f"{row.get('fee_category')} / {row.get('fee_name')}"
            result["fee_rate"].append(row)

    result["price_composition"].sort(key=lambda r: r["score"], reverse=True)
    result["fee_rate"].sort(key=lambda r: r["score"], reverse=True)
    result["price_composition"] = result["price_composition"][: max(1, min(top_k, 50))]
    result["fee_rate"] = result["fee_rate"][: max(1, min(top_k, 50))]
    return result
