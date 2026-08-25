from uuid import uuid4

from deep_researcher.run_event_projector import RunEventProjector


class RecordingEventLog:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, run_id, event_type, payload, *, event_key=None, lease_owner=None):
        if event_key is not None:
            for index, event in enumerate(self.events, start=1):
                if event["run_id"] == run_id and event["event_key"] == event_key:
                    return index
        self.events.append(
            {
                "run_id": run_id,
                "type": event_type,
                "payload": payload,
                "event_key": event_key,
                "lease_owner": lease_owner,
            }
        )
        return len(self.events)


def test_graph_updates_without_committed_facts_are_not_product_events() -> None:
    log = RecordingEventLog()
    projector = RunEventProjector(log)

    projector.project(uuid4(), "writer", {"answer": "speculative graph output"})

    assert log.events == []


def test_committed_facts_project_in_stable_order_and_are_idempotent() -> None:
    log = RecordingEventLog()
    projector = RunEventProjector(log)
    run_id = uuid4()
    facts = {
        "plan": {"id": "plan-1", "version": 1, "tasks": [{"id": "task-1"}]},
        "tasks": [
            {"id": "task-1", "status": "running", "ordinal": 1},
            {"id": "task-1", "status": "waiting", "ordinal": 1},
        ],
        "model_turns": [{"id": "turn-1", "task_id": "task-1", "output_kind": "tool_call"}],
        "tool_calls": [{"id": "call-1", "tool_name": "search", "parameters_hash": "hash"}],
        "observations": [
            {
                "id": "observation-1",
                "task_id": "task-1",
                "status": "completed",
                "summary": "bounded",
            }
        ],
        "verifier": {"id": "decision-1", "status": "complete"},
        "writer": {"message_id": "message-1", "status": "committed"},
        "citations": [{"id": "citation-1", "source_hash": "source-1"}],
        "terminal": {"status": "completed", "run_id": str(run_id)},
    }

    projector.project_committed(run_id, facts, lease_owner="worker-1")
    projector.project_committed(run_id, facts, lease_owner="worker-1")

    assert [event["type"] for event in log.events] == [
        "plan_created",
        "task_running",
        "task_waiting",
        "model_turn_committed",
        "tool_call_committed",
        "observation_committed",
        "verifier_decided",
        "writer_committed",
        "citation_committed",
        "run_completed",
    ]
    assert [event["event_key"] for event in log.events] == [
        "plan:plan-1",
        "task:task-1:running",
        "task:task-1:waiting",
        "model-turn:turn-1",
        "tool-call:call-1",
        "observation:observation-1",
        "verifier:decision-1",
        "writer:message-1",
        "citation:citation-1",
        f"run-terminal:{run_id}",
    ]


def test_large_fact_payloads_are_reduced_to_safe_summary_and_reference() -> None:
    log = RecordingEventLog()
    projector = RunEventProjector(log)
    run_id = uuid4()

    projector.project_committed(
        run_id,
        {
            "observations": [
                {
                    "id": "observation-1",
                    "status": "completed",
                    "summary": "x" * 5000,
                    "stdout": "secret output",
                    "file_content": "private file",
                    "result_reference": "observation:1",
                }
            ]
        },
    )

    payload = log.events[0]["payload"]
    assert payload == {
        "id": "observation-1",
        "status": "completed",
        "summary": "x" * 1000,
        "result_reference": "observation:1",
    }


def test_partial_is_a_terminal_completed_event_and_unknown_task_status_is_ignored() -> None:
    log = RecordingEventLog()
    projector = RunEventProjector(log)
    run_id = uuid4()

    projector.project_committed(
        run_id,
        {
            "tasks": [{"id": "task-1", "status": "not-a-status"}],
            "terminal": {"status": "partial", "reason": "missing evidence"},
        },
    )

    assert len(log.events) == 1
    assert log.events[0]["type"] == "run_completed"
    assert log.events[0]["event_key"] == f"run-terminal:{run_id}"
