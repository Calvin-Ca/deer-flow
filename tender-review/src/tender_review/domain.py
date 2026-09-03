"""Core review types with no infrastructure dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ReviewScope(StrEnum):
    """Business dimension to which a finding belongs."""

    QUALIFICATION = "qualification"
    COMPLIANCE = "compliance"
    TECHNICAL = "technical"
    COMMERCIAL = "commercial"
    PRICE = "price"


class ReviewSeverity(StrEnum):
    """Impact of a finding on the automated recommendation."""

    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    BLOCKER = "blocker"


class ReviewDecision(StrEnum):
    """Automated recommendation; never the final award decision."""

    PASS = "pass"
    MANUAL_REVIEW = "manual_review"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """Identifiers and immutable inputs shared by all review rules."""

    project_id: str
    tender_document_id: str
    bid_document_id: str
    lot_id: str | None = None
    bidder_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project_id", "tender_document_id", "bid_document_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """One traceable issue or observation produced by a review rule."""

    finding_id: str
    rule_id: str
    criterion_id: str
    scope: ReviewScope
    severity: ReviewSeverity
    title: str
    description: str
    evidence_refs: tuple[str, ...]
    recommended_action: str

    def __post_init__(self) -> None:
        required_text = {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "criterion_id": self.criterion_id,
            "title": self.title,
            "description": self.description,
            "recommended_action": self.recommended_action,
        }
        for name, value in required_text.items():
            if not str(value).strip():
                raise ValueError(f"{name} must not be empty")
        if not self.evidence_refs or any(not str(ref).strip() for ref in self.evidence_refs):
            raise ValueError("evidence_refs must contain at least one non-empty reference")


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """Auditable output of one pipeline run."""

    project_id: str
    tender_document_id: str
    bid_document_id: str
    findings: tuple[ReviewFinding, ...]
    rule_ids: tuple[str, ...]
    recommendation: ReviewDecision
    run_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def requires_human_review(self) -> bool:
        """All non-pass recommendations require an explicit human decision."""

        return self.recommendation is not ReviewDecision.PASS

    @classmethod
    def from_context(
        cls,
        context: ReviewContext,
        findings: tuple[ReviewFinding, ...],
        rule_ids: tuple[str, ...],
    ) -> ReviewReport:
        severities = {finding.severity for finding in findings}
        if ReviewSeverity.BLOCKER in severities:
            recommendation = ReviewDecision.REJECT
        elif ReviewSeverity.MAJOR in severities:
            recommendation = ReviewDecision.MANUAL_REVIEW
        else:
            recommendation = ReviewDecision.PASS

        return cls(
            project_id=context.project_id,
            tender_document_id=context.tender_document_id,
            bid_document_id=context.bid_document_id,
            findings=findings,
            rule_ids=rule_ids,
            recommendation=recommendation,
        )
