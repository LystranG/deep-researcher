from deep_researcher.stop_policy import StopPolicyInput, decide_stop


def test_publishable_claim_without_valid_citation_remains_partial() -> None:
    """验证可发布主张缺少有效 Citation 时不能完成研究"""
    outcome = decide_stop(
        StopPolicyInput(
            requested_status="completed",
            verification_status="supported",
            verified_claim_count=2,
            valid_citation_count=1,
        )
    )

    assert outcome.status == "partial"
    assert outcome.complete is False
    assert outcome.reason == "missing_valid_citation"
    assert outcome.gap_descriptions == ("可发布 Claim 缺少有效 Citation",)
