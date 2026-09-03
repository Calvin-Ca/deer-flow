"""Review rule orchestration and report aggregation."""

from __future__ import annotations

from collections.abc import Iterable

from tender_review.domain import ReviewContext, ReviewFinding, ReviewReport
from tender_review.rules import ReviewRule


class ReviewPipeline:
    """Run registered rules in order and aggregate their findings."""

    def __init__(self, rules: Iterable[ReviewRule]) -> None:
        self._rules = tuple(rules)
        rule_ids = tuple(rule.rule_id.strip() for rule in self._rules)
        if any(not rule_id for rule_id in rule_ids):
            raise ValueError("rule_id must not be empty")
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("rule_id must be unique within a pipeline")

    def run(self, context: ReviewContext) -> ReviewReport:
        findings: list[ReviewFinding] = []
        finding_ids: set[str] = set()

        for rule in self._rules:
            for finding in rule.evaluate(context):
                if not isinstance(finding, ReviewFinding):
                    raise TypeError(
                        f"rule {rule.rule_id!r} returned {type(finding).__name__}, "
                        "expected ReviewFinding"
                    )
                if finding.finding_id in finding_ids:
                    raise ValueError(f"duplicate finding_id: {finding.finding_id!r}")
                if finding.rule_id != rule.rule_id:
                    raise ValueError(
                        f"finding {finding.finding_id!r} declares rule_id "
                        f"{finding.rule_id!r}, expected {rule.rule_id!r}"
                    )
                finding_ids.add(finding.finding_id)
                findings.append(finding)

        return ReviewReport.from_context(
            context=context,
            findings=tuple(findings),
            rule_ids=tuple(rule.rule_id for rule in self._rules),
        )
