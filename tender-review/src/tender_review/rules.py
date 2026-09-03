"""Extension protocol for deterministic or model-backed review rules."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from tender_review.domain import ReviewContext, ReviewFinding


class ReviewRule(Protocol):
    """A versioned review unit registered in the pipeline."""

    rule_id: str

    def evaluate(self, context: ReviewContext) -> Iterable[ReviewFinding]:
        """Return traceable findings for one bid document."""
        ...
