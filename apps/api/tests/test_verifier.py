from datetime import UTC, datetime
from uuid import UUID

import pytest
from deep_researcher.app import create_app
from deep_researcher.models import ResearchRun, ResearchTask, StopDecision, TaskOutcome
from deep_researcher.settings import Settings
from deep_researcher.verifier import (
    ClaimVerification,
    CoverageItem,
    RunVerifier,
    VerificationBarrierError,
    VerificationFacts,
    decide_verification,
)
from fastapi.testclient import TestClient
from sqlalchemy import select


def register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "verifier@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_run(client: TestClient) -> tuple[dict[str, str], str]:
    headers = register(client)
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Verifier"}
    ).json()["id"]
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "Verifier"},
    ).json()["id"]
    run = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={**headers, "Idempotency-Key": "verifier-run"},
        json={"content": "验证研究结果"},
    ).json()
    return headers, run["run_id"]


def facts(
    *,
    route: str = "complete",
    budget_remaining: bool = True,
    replans_remaining: bool = True,
) -> VerificationFacts:
    coverage = (CoverageItem("criterion-1", "covered"),)
    claims = (ClaimVerification("claim-1", valid_citation=True),)
    if route == "replan":
        coverage = (CoverageItem("criterion-1", "missing"),)
    if route == "partial":
        coverage = (CoverageItem("criterion-1", "missing"),)
        budget_remaining = False
    if route == "failed":
        claims = ()
        coverage = (CoverageItem("criterion-1", "missing"),)
        replans_remaining = False
    return VerificationFacts(
        coverage=coverage,
        claims=claims,
        actionable_gaps=("missing source",) if route != "complete" else (),
        material_conflicts=("source-conflict",) if route == "replan" else (),
        budget_remaining=budget_remaining,
        replans_remaining=replans_remaining,
    )


@pytest.mark.parametrize(
    ("expected", "input_facts"),
    [
        ("complete", facts()),
        ("replan", facts(route="replan")),
        ("partial", facts(route="partial")),
        ("failed", facts(route="failed")),
    ],
)
def test_verifier_routes_are_mutually_exclusive(
    expected: str, input_facts: VerificationFacts
) -> None:
    decision = decide_verification(input_facts)
    assert decision.route == expected
    assert decision.status == expected


def test_verifier_rejects_before_scheduler_barrier_and_persists_one_decision(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'verifier.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        _headers, run_id = create_run(client)
        verifier = RunVerifier(app.state.session_factory)
        with pytest.raises(VerificationBarrierError):
            verifier.verify(UUID(run_id), facts=facts())

        with app.state.session_factory.begin() as session:
            run = session.get(ResearchRun, UUID(run_id))
            assert run is not None
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == UUID(run_id))
                .order_by(ResearchTask.ordinal)
            ).all()
            for task in tasks:
                task.status = "completed"
                session.add(
                    TaskOutcome(
                        workspace_id=task.workspace_id,
                        run_id=task.run_id,
                        task_id=task.id,
                        fencing_epoch=task.fencing_epoch,
                        kind="completed",
                        outcome_ref=f"test-outcome:{task.id}",
                    )
                )

        decision = verifier.verify(UUID(run_id), facts=facts())
        replay = verifier.verify(
            UUID(run_id),
            facts=VerificationFacts(
                coverage=(CoverageItem("criterion-1", "missing"),),
            ),
        )
        with app.state.session_factory() as session:
            persisted = session.scalars(
                select(StopDecision).where(StopDecision.ledger_id.is_not(None))
            ).all()

    assert decision.route == "complete"
    assert replay == decision
    assert len(persisted) == 1


def test_budget_exhaustion_keeps_verified_claims_as_partial(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'verifier-partial.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        _headers, run_id = create_run(client)
        with app.state.session_factory.begin() as session:
            tasks = session.scalars(
                select(ResearchTask).where(ResearchTask.run_id == UUID(run_id))
            ).all()
            for task in tasks:
                task.status = "completed"
                session.add(
                    TaskOutcome(
                        workspace_id=task.workspace_id,
                        run_id=task.run_id,
                        task_id=task.id,
                        fencing_epoch=task.fencing_epoch,
                        kind="completed",
                        outcome_ref=f"partial-outcome:{task.id}",
                    )
                )
        decision = RunVerifier(app.state.session_factory).verify(
            UUID(run_id), facts=facts(route="partial")
        )

    assert decision.route == "partial"
    assert decision.publishable_claim_refs == ("claim-1",)
    assert decision.gaps == ("missing source", "criterion-1")


def test_blocking_failure_cannot_be_complete() -> None:
    decision = decide_verification(
        VerificationFacts(
            coverage=(CoverageItem("criterion-1", "covered"),),
            claims=(ClaimVerification("claim-1", valid_citation=True),),
            blocking_failures=("task failed",),
            budget_remaining=False,
            replans_remaining=False,
        )
    )

    assert decision.route == "partial"
    assert decision.gaps == ("task failed",)


def test_cancelled_run_is_independent_of_scheduler_barrier(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'verifier-cancel.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        _headers, run_id = create_run(client)
        with app.state.session_factory.begin() as session:
            run = session.get(ResearchRun, UUID(run_id))
            assert run is not None
            run.cancel_requested_at = datetime.now(UTC)
            run.status = "cancelled"
        decision = RunVerifier(app.state.session_factory).verify(
            UUID(run_id), facts=facts(route="failed")
        )

    assert decision.route == "cancelled"
    assert decision.stop_reason == "run_cancelled"
