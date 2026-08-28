import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from deep_researcher.database import Base, build_engine, build_session_factory
from deep_researcher.models import ResearchPlan, ResearchRun
from deep_researcher.replan import ReplanGate, ReplanLimits, ReplanRequest
from sqlalchemy import select


def request(**overrides: object) -> ReplanRequest:
    values: dict[str, object] = {
        "expected_plan_revision": 1,
        "trigger_type": "evidence_gap",
        "trigger_ref": "gap:missing-source",
        "goal": "Answer the question",
        "tasks": (
            {
                "ordinal": 2,
                "title": "Find source",
                "goal": "Find a source",
                "success_criteria": ["source found"],
                "dependencies": [],
                "allowed_tools": ["web_search"],
            },
        ),
        "allowed_tools": frozenset({"web_search"}),
    }
    values.update(overrides)
    return ReplanRequest(**values)


def gate_context() -> tuple[Mock, SimpleNamespace, SimpleNamespace]:
    session = Mock()
    session.scalars.return_value.all.return_value = []
    run_id = uuid4()
    run = SimpleNamespace(
        id=run_id,
        workspace_id=uuid4(),
        reservation_status="reserved",
    )
    current = SimpleNamespace(
        id=uuid4(),
        run_id=run_id,
        version=1,
        plan_hash="old",
        snapshot={"tasks": [{"ordinal": 1}]},
    )
    return session, run, current


@pytest.mark.parametrize("trigger_type", ["blocking_failure", "evidence_gap", "evidence_conflict"])
def test_gate_accepts_only_actionable_trigger_types(trigger_type: str) -> None:
    session, run, current = gate_context()
    reason = ReplanGate().validate(
        session,
        run,
        request(trigger_type=trigger_type),
        current,
    )
    assert reason is None


def test_gate_rejects_observation_and_budget_exhaustion() -> None:
    session, run, current = gate_context()
    assert (
        ReplanGate().validate(session, run, request(trigger_type="observation"), current)
        == "invalid_trigger_type"
    )
    assert (
        ReplanGate().validate(session, run, request(remaining_tokens=0), current)
        == "token_budget_exhausted"
    )


def test_gate_rejects_stale_worker_fence() -> None:
    session, run, current = gate_context()
    run.lease_owner = "worker-current"
    run.attempt = 3

    assert (
        ReplanGate().validate(
            session,
            run,
            request(lease_owner="worker-old", fencing_epoch=2),
            current,
        )
        == "stale_worker_fence"
    )


def test_gate_requires_owner_and_epoch_together() -> None:
    session, run, current = gate_context()

    assert (
        ReplanGate().validate(
            session,
            run,
            request(lease_owner="worker-current"),
            current,
        )
        == "missing_worker_fence"
    )


def test_gate_rejects_equivalent_plan_and_tool_expansion() -> None:
    session, run, current = gate_context()
    request_data = request()
    snapshot = {
        "version": 1,
        "goal": request_data.goal,
        "tasks": [dict(task) for task in request_data.tasks],
    }
    canonical = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    current.plan_hash = hashlib.sha256(canonical.encode()).hexdigest()
    assert (
        ReplanGate().validate(session, run, request_data, current)
        == "equivalent_plan"
    )
    current.plan_hash = "different"
    assert (
        ReplanGate().validate(
            session,
            run,
            request(allowed_tools=frozenset()),
            current,
        )
        == "tool_allowlist_violation"
    )


def test_gate_enforces_revision_task_limit() -> None:
    session, run, current = gate_context()
    tasks = tuple(
        {"ordinal": ordinal, "allowed_tools": []}
        for ordinal in range(2, 6)
    )
    reason = ReplanGate(ReplanLimits(max_tasks_per_revision=2)).validate(
        session,
        run,
        request(tasks=tasks, allowed_tools=frozenset()),
        current,
    )
    assert reason == "revision_task_limit"


def test_submit_persists_revision_provenance_and_returns_cas_winner() -> None:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    run_id = uuid4()
    workspace_id = uuid4()
    initial_snapshot = {
        "version": 1,
        "goal": "Answer",
        "tasks": [{"ordinal": 1, "allowed_tools": []}],
    }
    with session_factory.begin() as session:
        session.add(
            ResearchRun(
                id=run_id,
                workspace_id=workspace_id,
                conversation_id=uuid4(),
                trigger_message_id=uuid4(),
                assistant_message_id=uuid4(),
                idempotency_key="replan-cas",
                reservation_status="reserved",
            )
        )
        session.add(
            ResearchPlan(
                workspace_id=workspace_id,
                run_id=run_id,
                version=1,
                goal="Answer",
                plan_hash="initial",
                snapshot=initial_snapshot,
            )
        )

    replan_request = request(trigger_type="blocking_failure", trigger_ref="failure:1")
    first = ReplanGate().submit(session_factory, run_id, replan_request)
    second = ReplanGate().submit(session_factory, run_id, replan_request)

    assert first.accepted is True
    assert first.plan is not None
    assert first.plan.version == 2
    assert first.plan.parent_version == 1
    assert first.plan.trigger_type == "blocking_failure"
    assert first.plan.trigger_ref == "failure:1"
    assert second.accepted is False
    assert second.reason == "plan_revision_conflict"
    assert second.existing_version == 2
    with session_factory() as session:
        plans = session.scalars(
            select(ResearchPlan).where(ResearchPlan.run_id == run_id)
        ).all()
    assert len(plans) == 2
