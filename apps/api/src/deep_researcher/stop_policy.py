from dataclasses import dataclass


@dataclass(frozen=True)
class StopPolicyInput:
    """描述形成停止裁决所需的可审计账本事实"""

    requested_status: str
    verification_status: str | None = None
    verified_claim_count: int = 0
    valid_citation_count: int = 0
    blocking_gap_descriptions: tuple[str, ...] = ()
    missing_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class StopPolicyOutcome:
    """返回 Research Run 终态、完整性和待持久化 Evidence Gap"""

    status: str
    complete: bool
    reason: str
    gap_descriptions: tuple[str, ...]


def decide_stop(facts: StopPolicyInput) -> StopPolicyOutcome:
    """根据账本事实形成确定性停止裁决"""
    if facts.requested_status != "completed":
        return StopPolicyOutcome(
            status=facts.requested_status,
            complete=False,
            reason=facts.requested_status,
            gap_descriptions=facts.blocking_gap_descriptions
            or ("运行未形成足够的可定位证据",),
        )
    if facts.blocking_gap_descriptions:
        return StopPolicyOutcome(
            status="partial",
            complete=False,
            reason="blocking_evidence_gap",
            gap_descriptions=facts.blocking_gap_descriptions,
        )
    if facts.missing_chunk_ids:
        return StopPolicyOutcome(
            status="partial",
            complete=False,
            reason="missing_required_map_work",
            gap_descriptions=("仍有必需的来源 Chunk 未完成处理",),
        )
    if facts.verification_status in {"insufficient", "not_checkable"}:
        return StopPolicyOutcome(
            status="partial",
            complete=False,
            reason="verifier_insufficient",
            gap_descriptions=("Verifier 判定现有证据不足以核验结论",),
        )
    if facts.verified_claim_count == 0:
        return StopPolicyOutcome(
            status="partial",
            complete=False,
            reason="no_verified_claim",
            gap_descriptions=("运行没有经过核验的 Claim",),
        )
    if facts.valid_citation_count < facts.verified_claim_count:
        return StopPolicyOutcome(
            status="partial",
            complete=False,
            reason="missing_valid_citation",
            gap_descriptions=("可发布 Claim 缺少有效 Citation",),
        )
    return StopPolicyOutcome(
        status="completed",
        complete=True,
        reason="evidence_complete",
        gap_descriptions=(),
    )
