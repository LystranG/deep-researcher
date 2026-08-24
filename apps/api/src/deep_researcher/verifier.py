"""Durable Research Run verification and mutually exclusive stop routing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from deep_researcher.models import (
    Citation,
    CoverageSnapshot,
    EvidenceGap,
    ResearchClaim,
    ResearchLedger,
    ResearchPlan,
    ResearchRun,
    ResearchTask,
    StopDecision,
    TaskOutcome,
    TaskResultProposalRecord,
)

CoverageState = Literal["covered", "missing", "conflicted"]
VerificationRoute = Literal["complete", "replan", "partial", "failed", "cancelled"]


class VerificationBarrierError(RuntimeError):
    """Raised when a run is verified before the current runnable stage is durable."""


VerifierBarrierError = VerificationBarrierError


@dataclass(frozen=True)
class CoverageItem:
    """One success criterion and its explicit coverage state."""

    criterion: str
    state: CoverageState
    evidence_refs: tuple[str, ...] = ()
    conflict_refs: tuple[str, ...] = ()
    actionable: bool = True

    def __post_init__(self) -> None:
        if not self.criterion:
            raise ValueError("coverage criterion must not be empty")
        if self.state not in {"covered", "missing", "conflicted"}:
            raise ValueError(f"invalid coverage state: {self.state}")
        if self.state == "conflicted" and not self.conflict_refs:
            raise ValueError("conflicted coverage requires conflict_refs")

    def as_dict(self) -> dict[str, object]:
        return {
            "criterion": self.criterion,
            "state": self.state,
            "evidence_refs": list(self.evidence_refs),
            "conflict_refs": list(self.conflict_refs),
            "actionable": self.actionable,
        }


@dataclass(frozen=True)
class ClaimVerification:
    """The minimum evidence contract required before a Claim is publishable."""

    claim_ref: str
    verified: bool = True
    valid_citation: bool = False
    conflict_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.claim_ref:
            raise ValueError("claim_ref must not be empty")

    @property
    def publishable(self) -> bool:
        return self.verified and self.valid_citation and not self.conflict_refs

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_ref": self.claim_ref,
            "verified": self.verified,
            "valid_citation": self.valid_citation,
            "conflict_refs": list(self.conflict_refs),
            "publishable": self.publishable,
        }


@dataclass(frozen=True)
class VerificationFacts:
    """Auditable inputs to the pure four-way routing function."""

    coverage: tuple[CoverageItem, ...] = ()
    claims: tuple[ClaimVerification, ...] = ()
    actionable_gaps: tuple[str, ...] = ()
    blocking_failures: tuple[str, ...] = ()
    material_conflicts: tuple[str, ...] = ()
    budget_remaining: bool = True
    replans_remaining: bool = True
    cancelled: bool = False
    stop_reason: str | None = None

    @property
    def missing_coverage(self) -> tuple[CoverageItem, ...]:
        return tuple(item for item in self.coverage if item.state == "missing")

    @property
    def conflicted_coverage(self) -> tuple[CoverageItem, ...]:
        return tuple(item for item in self.coverage if item.state == "conflicted")

    @property
    def publishable_claims(self) -> tuple[ClaimVerification, ...]:
        return tuple(claim for claim in self.claims if claim.publishable)


@dataclass(frozen=True)
class VerificationDecision:
    """One immutable routing decision returned by the global Verifier."""

    route: VerificationRoute
    reason: str
    coverage: tuple[CoverageItem, ...] = ()
    gaps: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    stop_reason: str | None = None
    publishable_claim_refs: tuple[str, ...] = ()

    @property
    def status(self) -> VerificationRoute:
        """Alias used by callers that call the route a run status."""
        return self.route

    @property
    def complete(self) -> bool:
        return self.route == "complete"

    def details(self) -> dict[str, object]:
        return {
            "route": self.route,
            "reason": self.reason,
            "coverage": [item.as_dict() for item in self.coverage],
            "gaps": list(self.gaps),
            "conflicts": list(self.conflicts),
            "stop_reason": self.stop_reason,
            "publishable_claim_refs": list(self.publishable_claim_refs),
        }


def decide_verification(facts: VerificationFacts) -> VerificationDecision:
    """Form exactly one route from explicit coverage and evidence facts."""
    if facts.cancelled:
        return VerificationDecision(
            route="cancelled",
            reason="cancelled",
            coverage=facts.coverage,
            gaps=facts.actionable_gaps,
            conflicts=facts.material_conflicts,
            stop_reason=facts.stop_reason or "run_cancelled",
            publishable_claim_refs=tuple(
                claim.claim_ref for claim in facts.publishable_claims
            ),
        )

    missing = facts.missing_coverage
    conflicts = tuple(
        dict.fromkeys(
            (
                *facts.material_conflicts,
                *(
                    ref
                    for item in facts.conflicted_coverage
                    for ref in item.conflict_refs
                ),
            )
        )
    )
    gaps = tuple(
        dict.fromkeys(
            (
                *facts.actionable_gaps,
                *(item.criterion for item in missing if item.actionable),
            )
        )
    )
    invalid_claims = tuple(
        claim.claim_ref
        for claim in facts.claims
        if not claim.publishable and (claim.verified or claim.conflict_refs)
    )
    if invalid_claims:
        gaps = tuple(dict.fromkeys((*gaps, "missing_valid_citation")))
    publishable_claim_refs = tuple(claim.claim_ref for claim in facts.publishable_claims)
    has_substantive_conflict = bool(conflicts or any(claim.conflict_refs for claim in facts.claims))

    if (
        facts.coverage
        and not missing
        and not has_substantive_conflict
        and not facts.blocking_failures
        and facts.claims
        and len(publishable_claim_refs) == len(facts.claims)
    ):
        return VerificationDecision(
            route="complete",
            reason="coverage_complete",
            coverage=facts.coverage,
            publishable_claim_refs=publishable_claim_refs,
        )

    replan_needed = bool(gaps or facts.blocking_failures or has_substantive_conflict)
    if replan_needed and facts.budget_remaining and facts.replans_remaining:
        return VerificationDecision(
            route="replan",
            reason=(
                "material_evidence_conflict"
                if has_substantive_conflict
                else "actionable_evidence_gap"
            ),
            coverage=facts.coverage,
            gaps=tuple(dict.fromkeys((*gaps, *facts.blocking_failures))),
            conflicts=conflicts,
            stop_reason=facts.stop_reason,
            publishable_claim_refs=publishable_claim_refs,
        )

    if publishable_claim_refs:
        return VerificationDecision(
            route="partial",
            reason="budget_exhausted" if not facts.budget_remaining else "cannot_continue",
            coverage=facts.coverage,
            gaps=tuple(dict.fromkeys((*gaps, *facts.blocking_failures))),
            conflicts=conflicts,
            stop_reason=facts.stop_reason or "no_remaining_route",
            publishable_claim_refs=publishable_claim_refs,
        )

    return VerificationDecision(
        route="failed",
        reason="no_publishable_claim",
        coverage=facts.coverage,
        gaps=tuple(dict.fromkeys((*gaps, *facts.blocking_failures))),
        conflicts=conflicts,
        stop_reason=facts.stop_reason or "no_publishable_claim",
    )


class RunVerifier:
    """Verify a Research Run after its scheduler barrier and persist one decision."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_replans: int = 4,
    ) -> None:
        if max_replans < 0:
            raise ValueError("max_replans must not be negative")
        self._session_factory = session_factory
        self._max_replans = max_replans

    def verify(
        self,
        run_id: UUID,
        facts: VerificationFacts | None = None,
    ) -> VerificationDecision:
        """Return the existing decision or persist one after checking the barrier."""
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
            )
            if run is None:
                raise ValueError(f"research run not found: {run_id}")
            ledger = session.scalar(
                select(ResearchLedger)
                .where(ResearchLedger.run_id == run.id)
                .with_for_update()
            )
            if ledger is None:
                raise ValueError(f"research ledger not found for run: {run_id}")
            existing = session.scalar(
                select(StopDecision).where(StopDecision.ledger_id == ledger.id)
            )
            if existing is not None:
                return _decision_from_record(existing)

            resolved_facts = facts or self._facts_from_database(session, run, ledger)
            if run.cancel_requested_at is not None or run.status == "cancelled":
                resolved_facts = replace(
                    resolved_facts,
                    cancelled=True,
                    stop_reason=resolved_facts.stop_reason or "run_cancelled",
                )
            else:
                self._assert_barrier(session, run)
            decision = decide_verification(resolved_facts)
            ledger.status = decision.route
            run.status = decision.route
            if decision.route in {"complete", "partial", "failed", "cancelled"}:
                run.completed_at = run.completed_at or datetime.now(UTC)

            session.add(
                CoverageSnapshot(
                    workspace_id=run.workspace_id,
                    ledger_id=ledger.id,
                    citation_count=len(resolved_facts.publishable_claims),
                    verified_claim_count=sum(
                        1 for claim in resolved_facts.claims if claim.verified
                    ),
                    complete=decision.complete,
                    items=[item.as_dict() for item in decision.coverage],
                )
            )
            session.add_all(
                [
                    EvidenceGap(
                        workspace_id=run.workspace_id,
                        ledger_id=ledger.id,
                        description=description,
                        status="open",
                    )
                    for description in (*decision.gaps, *decision.conflicts)
                ]
            )
            session.add(
                StopDecision(
                    workspace_id=run.workspace_id,
                    ledger_id=ledger.id,
                    route=decision.route,
                    reason=decision.reason,
                    completeness="complete" if decision.complete else "partial",
                    details=decision.details(),
                )
            )
            return decision

    def verify_barrier(self, run_id: UUID) -> None:
        """Check the scheduler barrier without creating a decision."""
        with self._session_factory() as session:
            run = session.get(ResearchRun, run_id)
            if run is None:
                raise ValueError(f"research run not found: {run_id}")
            self._assert_barrier(session, run)

    def _assert_barrier(self, session: Session, run: ResearchRun) -> None:
        tasks = session.scalars(
            select(ResearchTask)
            .where(ResearchTask.run_id == run.id)
            .order_by(ResearchTask.ordinal)
        ).all()
        outcomes = {
            outcome.task_id: outcome
            for outcome in session.scalars(
                select(TaskOutcome).where(TaskOutcome.run_id == run.id)
            )
        }
        active = [
            task
            for task in tasks
            if task.status in {"ready", "running"}
            or (
                task.status == "pending"
                and _dependencies_completed(tasks, outcomes, task)
            )
        ]
        missing = [
            str(task.ordinal)
            for task in active
            if task.id not in outcomes
        ]
        terminal_without_outcome = [
            str(task.ordinal)
            for task in tasks
            if task.status in {
                "completed",
                "partial",
                "failed",
                "cancelled",
                "skipped",
                "superseded",
            }
            and task.id not in outcomes
        ]
        if missing or terminal_without_outcome:
            details = ", ".join(dict.fromkeys((*missing, *terminal_without_outcome)))
            raise VerificationBarrierError(
                f"verification barrier is not satisfied for task ordinals: {details}"
            )

    def _facts_from_database(
        self,
        session: Session,
        run: ResearchRun,
        ledger: ResearchLedger,
    ) -> VerificationFacts:
        tasks = session.scalars(
            select(ResearchTask).where(ResearchTask.run_id == run.id).order_by(ResearchTask.ordinal)
        ).all()
        proposals = session.scalars(
            select(TaskResultProposalRecord).where(TaskResultProposalRecord.run_id == run.id)
        ).all()
        valid_criteria: set[str] = set()
        for proposal in proposals:
            if proposal.valid:
                valid_criteria.update(proposal.covered_criteria)

        coverage: list[CoverageItem] = []
        for task in tasks:
            for criterion in task.success_criteria:
                coverage.append(
                    CoverageItem(
                        criterion=str(criterion),
                        state="covered" if criterion in valid_criteria else "missing",
                    )
                )

        claims = session.scalars(
            select(ResearchClaim).where(ResearchClaim.run_id == run.id)
        ).all()
        citations = session.scalars(
            select(Citation).where(Citation.message_id == run.assistant_message_id)
        ).all()
        valid_citation_count = sum(
            1
            for citation in citations
            if citation.source_hash
            and (citation.source_chunk_id is not None or citation.derived_evidence_id is not None)
        )
        claim_facts = tuple(
            ClaimVerification(
                claim_ref=str(claim.id),
                verified=claim.verdict == "verified" and claim.status == "verified",
                valid_citation=index < valid_citation_count,
                conflict_refs=(
                    (f"claim:{claim.id}",)
                    if claim.verdict == "contradicted"
                    else ()
                ),
            )
            for index, claim in enumerate(claims)
        )
        conflicts = tuple(
            f"claim:{claim.id}"
            for claim in claims
            if claim.verdict == "contradicted"
        )
        gaps = tuple(
            gap.description
            for gap in session.scalars(
                select(EvidenceGap)
                .where(EvidenceGap.ledger_id == ledger.id, EvidenceGap.status == "open")
                .order_by(EvidenceGap.created_at, EvidenceGap.id)
            )
        )
        failures = tuple(
            outcome.failure_ref or f"task:{outcome.task_id}"
            for outcome in session.scalars(
                select(TaskOutcome).where(
                    TaskOutcome.run_id == run.id,
                    TaskOutcome.kind == "failed",
                )
            )
        )
        plan_versions = len(
            session.scalars(
                select(ResearchPlan).where(ResearchPlan.run_id == run.id)
            ).all()
        )
        return VerificationFacts(
            coverage=tuple(coverage),
            claims=claim_facts,
            actionable_gaps=gaps,
            blocking_failures=failures,
            material_conflicts=conflicts,
            budget_remaining=run.reservation_status not in {"exhausted", "released"},
            replans_remaining=max(plan_versions - 1, 0) < self._max_replans,
            cancelled=run.cancel_requested_at is not None or run.status == "cancelled",
        )


GlobalVerifier = RunVerifier
Verifier = RunVerifier


def _dependencies_completed(
    tasks: Sequence[ResearchTask],
    outcomes: dict[UUID, TaskOutcome],
    task: ResearchTask,
) -> bool:
    by_ordinal = {candidate.ordinal: candidate.id for candidate in tasks}
    return all(
        by_ordinal.get(ordinal) in outcomes
        and outcomes[by_ordinal[ordinal]].kind == "completed"
        for ordinal in task.dependencies
    )


def _decision_from_record(decision: StopDecision) -> VerificationDecision:
    details = decision.details or {}
    route_value = (
        _route_from_legacy(decision)
        if not details and decision.route == "failed"
        else decision.route or details.get("route") or _route_from_legacy(decision)
    )
    route: VerificationRoute = (
        cast(VerificationRoute, route_value)
        if route_value in {"complete", "replan", "partial", "failed", "cancelled"}
        else "failed"
    )
    raw_coverage = details.get("coverage", [])
    raw_gaps = details.get("gaps", [])
    raw_conflicts = details.get("conflicts", [])
    raw_claim_refs = details.get("publishable_claim_refs", [])
    coverage_values = raw_coverage if isinstance(raw_coverage, list) else []
    gap_values = raw_gaps if isinstance(raw_gaps, list) else []
    conflict_values = raw_conflicts if isinstance(raw_conflicts, list) else []
    claim_ref_values = raw_claim_refs if isinstance(raw_claim_refs, list) else []
    stop_reason_value = details.get("stop_reason")
    stop_reason = stop_reason_value if isinstance(stop_reason_value, str) else None
    coverage = tuple(
        CoverageItem(
            criterion=str(item["criterion"]),
            state=cast(CoverageState, item["state"]),
            evidence_refs=tuple(str(value) for value in item.get("evidence_refs", [])),
            conflict_refs=tuple(str(value) for value in item.get("conflict_refs", [])),
            actionable=bool(item.get("actionable", True)),
        )
        for item in coverage_values
        if isinstance(item, dict)
        and isinstance(item.get("criterion"), str)
        and item.get("state") in {"covered", "missing", "conflicted"}
    )
    return VerificationDecision(
        route=route,
        reason=decision.reason,
        coverage=coverage,
        gaps=tuple(str(value) for value in gap_values),
        conflicts=tuple(str(value) for value in conflict_values),
        stop_reason=stop_reason,
        publishable_claim_refs=tuple(str(value) for value in claim_ref_values),
    )


def _route_from_legacy(decision: StopDecision) -> str:
    if decision.reason == "cancelled":
        return "cancelled"
    if decision.completeness == "complete":
        return "complete"
    return "partial"
