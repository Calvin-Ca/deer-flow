"""RAG-facing orchestration boundary for ce-code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import config
from cost import bill_match as bill_match_mod
from ir.query import RetrievalQuery
from rag.contracts import TRUTH_GROUND, TRUTH_PROJECTION, TRUTH_SEMANTIC, build_evidence
from rag import projection_search
from retrieval.service import RetrievalService


class RagService:
    """Expose retrieval and candidate recall as a stable application boundary."""

    def __init__(self, vector_store_root: Path) -> None:
        self.root = Path(vector_store_root)
        self.retrieval = RetrievalService(vector_store_root)

    def search_clause(
        self,
        query: str,
        *,
        standard: str | None = None,
        top_k: int = 15,
        skip_rerank: bool = False,
    ) -> dict[str, Any]:
        # standard 缺省 → 服务端确定性推断（2013 两部之间按问题类型裁定）；显式/推断都过 agent 面闸。
        standard_source = "explicit"
        if not standard:
            standard = config.infer_standard(query)
            standard_source = "inferred"
        config.ensure_agent_standard(standard)
        rq = RetrievalQuery(text=query, standard=standard, top_k=top_k, skip_rerank=skip_rerank)
        ctx = self.retrieval.search(rq)
        raw_resp = ctx.to_response()
        clauses = raw_resp["clauses"]
        evidence = [
            build_evidence(
                source_type="clause",
                title=f"{c.get('node_path') or '?'}".strip(),
                snippet=c.get("content") or "",
                truth_level=TRUTH_GROUND,
                score=c.get("_rerank_score") or c.get("_rrf_score") or c.get("_vector_score") or c.get("_bm25_score"),
                metadata={
                    "standard": ctx.standard,
                    "node_path": c.get("node_path"),
                    "page": c.get("page"),
                    "source": c.get("_source"),
                },
            )
            for c in clauses
        ]
        return {
            **raw_resp,
            "count": len(clauses),
            "evidence": evidence,
            "standard_source": standard_source,
        }

    def expand_clause_refs(self, *, standard: str, node_paths: list[str]) -> dict[str, Any]:
        config.ensure_agent_standard(standard)
        store_name, n_seeds, added = self.retrieval.expand(node_paths, standard)
        expanded_clauses = [item.to_response() for item in added]
        evidence = [
            build_evidence(
                source_type="clause_ref",
                title=item.node_path,
                snippet=item.content,
                truth_level=TRUTH_GROUND,
                score=item.scores.get("rerank") or item.scores.get("rrf") or item.scores.get("vector") or item.scores.get("bm25"),
                metadata={
                    "standard": store_name,
                    "node_path": item.node_path,
                    "page": item.page,
                    "source": item.source,
                },
            )
            for item in added
        ]
        return {
            "standard": store_name,
            "seeds": node_paths,
            "seed_count": n_seeds,
            "count": len(evidence),
            "expanded_clauses": expanded_clauses,
            "evidence": evidence,
            "meta": {"seeds": n_seeds, "added": len(expanded_clauses)},
        }

    def get_clause(self, *, standard: str, node_path: str) -> dict[str, Any]:
        config.ensure_agent_standard(standard)
        store_name, clause = self.retrieval.get_clause(standard, node_path)
        return {"standard": store_name, "node_path": node_path, "clause": clause}

    def match_bill_item(
        self,
        description: str,
        *,
        spec: str,
        top_k: int = 10,
        code_prefixes: list[str] | None = None,
    ) -> dict[str, Any]:
        cfg = config.resolve_spec(spec)
        candidates = bill_match_mod.search_bill(
            description,
            top_k=top_k,
            collection_name=cfg["bill_collection"],
            structural=True,
            code_prefixes=code_prefixes,
        )
        evidence = [
            build_evidence(
                source_type="bill_candidate",
                title=f"{c.get('code')} {c.get('name')}".strip(),
                snippet=(c.get("feature") or "")[:400],
                truth_level=TRUTH_SEMANTIC,
                score=c.get("score"),
                metadata={
                    "code": c.get("code"),
                    "unit": c.get("unit"),
                    "chapter": c.get("chapter"),
                    "doc_id": c.get("doc_id"),
                    "spec_version": c.get("spec_version"),
                    "cast_type": c.get("cast_type"),
                },
            )
            for c in candidates
        ]
        return {"query": description, "spec": spec, "count": len(candidates), "candidates": candidates, "evidence": evidence}

    def search_aux_table(
        self,
        query: str,
        *,
        spec: str | None = None,
        chapter: str | None = None,
        kind: str | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        spec_version = None
        if spec:
            spec_version = config.resolve_spec(spec)["bill_spec_versions"][0]
        rows = projection_search.search_aux_table(
            query,
            spec_version=spec_version,
            chapter=chapter,
            kind=kind,
            top_k=top_k,
        )
        evidence = [
            build_evidence(
                source_type="aux_table_projection",
                title=f"{r.get('caption')} ({r.get('kind')})",
                snippet=r.get("snippet") or "",
                truth_level=TRUTH_PROJECTION,
                score=r.get("score"),
                metadata={
                    "chapter": r.get("chapter"),
                    "doc_id": r.get("doc_id"),
                    "spec_version": r.get("spec_version"),
                    "provenance": r.get("provenance"),
                },
            )
            for r in rows
        ]
        return {"query": query, "count": len(evidence), "evidence": evidence, "rows": rows}

    def search_price_rule(
        self,
        query: str,
        *,
        spec_version: str | None = None,
        region: str | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        rows = projection_search.search_price_rule(
            query,
            spec_version=spec_version,
            region=region,
            top_k=top_k,
        )
        evidence: list[dict[str, Any]] = []
        for row in rows["price_composition"]:
            evidence.append(
                build_evidence(
                    source_type="price_composition_projection",
                    title=f"{row.get('composite')} -> {row.get('component')}",
                    snippet=row.get("snippet") or "",
                    truth_level=TRUTH_PROJECTION,
                    score=row.get("score"),
                    metadata={
                        "doc_id": row.get("doc_id"),
                        "spec_version": row.get("spec_version"),
                        "kind": row.get("kind"),
                        "seq": row.get("seq"),
                    },
                )
            )
        for row in rows["fee_rate"]:
            evidence.append(
                build_evidence(
                    source_type="fee_rate_projection",
                    title=f"{row.get('fee_category')} / {row.get('fee_name')}",
                    snippet=row.get("snippet") or "",
                    truth_level=TRUTH_PROJECTION,
                    score=row.get("score"),
                    metadata={
                        "doc_id": row.get("doc_id"),
                        "spec_version": row.get("spec_version"),
                        "region": row.get("region"),
                    },
                )
            )
        evidence.sort(key=lambda item: item.get("score") or 0.0, reverse=True)
        return {"query": query, "count": len(evidence), "evidence": evidence, "rows": rows}

    def retrieve_evidence(
        self,
        query: str,
        *,
        corpus: str,
        top_k: int = 10,
        standard: str | None = None,
        spec: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        filters = filters or {}
        if corpus == "clause":
            # standard 可省——search_clause 内部按问题类型推断（默认 2013 口径）并过 agent 面闸。
            return self.search_clause(query, standard=standard, top_k=top_k, skip_rerank=bool(filters.get("skip_rerank")))
        if corpus == "bill_match":
            if not spec:
                raise ValueError("corpus=bill_match 时 spec 必填")
            return self.match_bill_item(query, spec=spec, top_k=top_k, code_prefixes=filters.get("code_prefixes"))
        if corpus == "aux_table":
            return self.search_aux_table(query, spec=spec, chapter=filters.get("chapter"), kind=filters.get("kind"), top_k=top_k)
        if corpus == "price_rule":
            return self.search_price_rule(query, spec_version=filters.get("spec_version"), region=filters.get("region"), top_k=top_k)
        raise ValueError(f"未知 corpus={corpus!r}，支持: ['clause', 'bill_match', 'aux_table', 'price_rule']")
