from dataclasses import dataclass

import pytest

from tender_review import (
    ReviewContext,
    ReviewDecision,
    ReviewFinding,
    ReviewPipeline,
    ReviewScope,
    ReviewSeverity,
)

CONTEXT = ReviewContext(
    project_id="project-1",
    tender_document_id="tender-v1",
    bid_document_id="bid-a-v1",
)


@dataclass
class StaticRule:
    rule_id: str
    findings: tuple[ReviewFinding, ...]

    def evaluate(self, context: ReviewContext):
        assert context is CONTEXT
        return self.findings


def finding(severity: ReviewSeverity, finding_id: str = "finding-1") -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        rule_id="rule-1",
        criterion_id="criterion-1",
        scope=ReviewScope.COMPLIANCE,
        severity=severity,
        title="测试发现",
        description="投标响应与招标要求存在差异。",
        evidence_refs=("bid-a-v1:page=3:paragraph=2",),
        recommended_action="请评审人员复核原文。",
    )


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (ReviewSeverity.INFO, ReviewDecision.PASS),
        (ReviewSeverity.MINOR, ReviewDecision.PASS),
        (ReviewSeverity.MAJOR, ReviewDecision.MANUAL_REVIEW),
        (ReviewSeverity.BLOCKER, ReviewDecision.REJECT),
    ],
)
def test_pipeline_aggregates_recommendation(severity, expected):
    report = ReviewPipeline([StaticRule("rule-1", (finding(severity),))]).run(CONTEXT)

    assert report.recommendation is expected
    assert report.requires_human_review is (expected is not ReviewDecision.PASS)
    assert report.rule_ids == ("rule-1",)


def test_empty_pipeline_passes():
    report = ReviewPipeline([]).run(CONTEXT)

    assert report.recommendation is ReviewDecision.PASS
    assert report.findings == ()


def test_finding_requires_evidence():
    with pytest.raises(ValueError, match="evidence_refs"):
        ReviewFinding(
            finding_id="finding-1",
            rule_id="rule-1",
            criterion_id="criterion-1",
            scope=ReviewScope.TECHNICAL,
            severity=ReviewSeverity.MAJOR,
            title="无证据发现",
            description="不应接受没有证据的结论。",
            evidence_refs=(),
            recommended_action="人工复核。",
        )


def test_pipeline_rejects_duplicate_rule_ids():
    with pytest.raises(ValueError, match="rule_id must be unique"):
        ReviewPipeline([StaticRule("same", ()), StaticRule("same", ())])


def test_pipeline_rejects_duplicate_finding_ids():
    first = finding(ReviewSeverity.MINOR)
    second = ReviewFinding(
        finding_id=first.finding_id,
        rule_id="rule-2",
        criterion_id="criterion-2",
        scope=ReviewScope.PRICE,
        severity=ReviewSeverity.MAJOR,
        title="重复 ID",
        description="不同规则不得返回相同发现 ID。",
        evidence_refs=("bid-a-v1:page=8:table=1:cell=C4",),
        recommended_action="修正规则输出。",
    )

    with pytest.raises(ValueError, match="duplicate finding_id"):
        ReviewPipeline(
            [StaticRule("rule-1", (first,)), StaticRule("rule-2", (second,))]
        ).run(CONTEXT)
