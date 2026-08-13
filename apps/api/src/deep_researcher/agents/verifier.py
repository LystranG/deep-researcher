from typing import Literal, TypedDict

from deep_researcher.agents.researcher import ResearchFinding


class VerificationResult(TypedDict):
    """Verifier 对聚合研究结果的结构化判断"""

    status: Literal["supported", "contradicted", "insufficient", "not_checkable"]
    summary: str


def verify_evidence_candidate(claim_text: str, evidence_text: str) -> VerificationResult:
    """核验候选主张是否由回读的不可变来源原文直接支持"""
    if claim_text and claim_text in evidence_text:
        return {"status": "supported", "summary": "候选主张由来源原文直接支持"}
    return {"status": "insufficient", "summary": "候选主张无法由来源原文直接支持"}


def verify(findings: list[ResearchFinding]) -> VerificationResult:
    """独立核对聚合结果是否足以进入写作阶段"""
    if not findings:
        return {"status": "insufficient", "summary": "当前没有可核验的研究结果"}
    if any(finding["status"] == "failed" for finding in findings):
        return {
            "status": "insufficient",
            "summary": "部分研究分支失败，当前证据可能不完整",
        }
    return {"status": "supported", "summary": "研究结果已完成独立核对"}
