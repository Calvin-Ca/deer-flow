"""Shared response builders for ce-rag."""

from __future__ import annotations

from typing import Any

TRUTH_SEMANTIC = "semantic_candidate"
TRUTH_PROJECTION = "text_projection"
TRUTH_GROUND = "ground_truth_row"


def build_evidence(
    *,
    source_type: str,
    title: str,
    snippet: str,
    truth_level: str,
    score: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a uniform evidence envelope for rag-facing tools."""
    return {
        "source_type": source_type,
        "title": title,
        "snippet": snippet,
        "truth_level": truth_level,
        "score": round(float(score), 4) if score is not None else None,
        "metadata": metadata or {},
    }
