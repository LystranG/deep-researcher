from collections.abc import Mapping
from typing import Any
from uuid import UUID

from deep_researcher.event_log import RunEventLog


class RunEventProjector:
    """把已提交业务事实投影为稳定、受限的研究进度事件。"""

    def __init__(self, event_log: RunEventLog) -> None:
        self._event_log = event_log

    def project(
        self,
        run_id: UUID,
        node_name: str,
        update: dict[str, Any],
        *,
        lease_owner: str | None = None,
    ) -> None:
        """投影 Graph 回调中的明确提交回执，忽略推测性的 Graph 状态。"""
        committed = update.get("committed_facts")
        if isinstance(committed, Mapping):
            self.project_committed(run_id, committed, lease_owner=lease_owner)

    def project_committed(
        self,
        run_id: UUID,
        facts: Mapping[str, Any],
        *,
        lease_owner: str | None = None,
    ) -> None:
        """按业务提交顺序投影事实；事实本身必须已在调用方事务中提交。"""
        plan = facts.get("plan")
        if isinstance(plan, Mapping):
            plan_id = self._identifier(
                plan, "id", f"{run_id}:plan:{plan.get('version', 1)}"
            )
            event_key = "plan-created:v1" if plan_id.startswith(str(run_id)) else f"plan:{plan_id}"
            self._append(
                run_id,
                "plan_created",
                self._payload(plan, ("id", "version", "tasks", "parent_version")),
                event_key,
                lease_owner,
            )

        for task in self._mappings(facts.get("tasks")):
            task_id = self._identifier(task, "id", "")
            status = str(task.get("status", ""))
            if task_id and status in {
                "pending",
                "ready",
                "running",
                "waiting",
                "completed",
                "failed",
                "cancelled",
                "skipped",
                "superseded",
            }:
                self._append(
                    run_id,
                    f"task_{status}",
                    self._payload(task, ("id", "ordinal", "status", "outcome_ref", "reason")),
                    f"task:{task_id}:{status}",
                    lease_owner,
                )

        for turn in self._mappings(facts.get("model_turns")):
            turn_id = self._identifier(turn, "id", "")
            if turn_id:
                self._append(
                    run_id,
                    "model_turn_committed",
                    self._payload(
                        turn,
                        (
                            "id",
                            "task_id",
                            "turn_ordinal",
                            "output_kind",
                            "logical_call_ref",
                            "parameters_hash",
                            "safe_summary",
                            "usage",
                        ),
                    ),
                    f"model-turn:{turn_id}",
                    lease_owner,
                )

        for call in self._mappings(facts.get("tool_calls")):
            call_id = self._identifier(call, "id", "")
            if call_id:
                self._append(
                    run_id,
                    "tool_call_committed",
                    self._payload(
                        call,
                        ("id", "tool_name", "parameters_hash", "safe_summary", "status"),
                    ),
                    f"tool-call:{call_id}",
                    lease_owner,
                )

        for observation in self._mappings(facts.get("observations")):
            observation_id = self._identifier(observation, "id", "")
            if observation_id:
                self._append(
                    run_id,
                    "observation_committed",
                    self._payload(
                        observation,
                        (
                            "id",
                            "task_id",
                            "status",
                            "summary",
                            "result_reference",
                            "waiting_reference",
                        ),
                    ),
                    f"observation:{observation_id}",
                    lease_owner,
                )

        for revision in self._mappings(facts.get("file_revisions")):
            ref = revision.get("ref")
            if isinstance(ref, Mapping):
                ref_id = self._identifier(ref, "id", "")
                ref_revision = str(ref.get("revision", ""))
                if ref_id and ref_revision:
                    self._append(
                        run_id,
                        "file_revision_committed",
                        self._payload(
                            revision,
                            (
                                "ref",
                                "name",
                                "status",
                                "content_hash",
                                "size_bytes",
                                "media_type",
                                "parent_ref",
                                "failure_reason",
                            ),
                        ),
                        f"file-revision:{ref_id}:{ref_revision}",
                        lease_owner,
                    )

        for artifact in self._mappings(facts.get("artifacts")):
            ref = artifact.get("ref")
            if isinstance(ref, Mapping):
                ref_id = self._identifier(ref, "id", "")
                ref_revision = str(ref.get("revision", ""))
                if ref_id and ref_revision:
                    self._append(
                        run_id,
                        "artifact_published",
                        self._payload(
                            artifact,
                            (
                                "ref",
                                "source_ref",
                                "name",
                                "status",
                                "content_hash",
                                "size_bytes",
                                "media_type",
                                "failure_reason",
                            ),
                        ),
                        f"artifact:{ref_id}:{ref_revision}",
                        lease_owner,
                    )

        verifier = facts.get("verifier")
        if isinstance(verifier, Mapping):
            decision_id = self._identifier(verifier, "id", "")
            if decision_id:
                self._append(
                    run_id,
                    "verifier_decided",
                    self._payload(verifier, ("id", "status", "reason", "gap_count")),
                    f"verifier:{decision_id}",
                    lease_owner,
                )

        writer = facts.get("writer")
        if isinstance(writer, Mapping):
            message_id = self._identifier(writer, "message_id", "")
            if message_id:
                self._append(
                    run_id,
                    "writer_committed",
                    self._payload(writer, ("message_id", "status", "content_reference")),
                    f"writer:{message_id}",
                    lease_owner,
                )

        for citation in self._mappings(facts.get("citations")):
            citation_id = self._identifier(citation, "id", "")
            if citation_id:
                self._append(
                    run_id,
                    "citation_committed",
                    self._payload(citation, ("id", "label", "source_hash", "source_reference")),
                    f"citation:{citation_id}",
                    lease_owner,
                )

        terminal = facts.get("terminal")
        if isinstance(terminal, Mapping):
            status = str(terminal.get("status", ""))
            if status in {"completed", "partial", "cancelled", "failed"}:
                event_status = "completed" if status == "partial" else status
                self._append(
                    run_id,
                    f"run_{event_status}",
                    self._payload(terminal, ("status", "reason", "message_reference")),
                    f"run-terminal:{run_id}",
                    lease_owner,
                )

    def _append(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, object],
        event_key: str,
        lease_owner: str | None,
    ) -> None:
        self._event_log.append(
            run_id,
            event_type,
            payload,
            event_key=event_key,
            lease_owner=lease_owner,
        )

    @staticmethod
    def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _identifier(value: Mapping[str, Any], key: str, fallback: str) -> str:
        identifier = value.get(key)
        return str(identifier) if identifier is not None else fallback

    @staticmethod
    def _payload(value: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field in fields:
            if field not in value or value[field] is None:
                continue
            item = value[field]
            if isinstance(item, str):
                payload[field] = item[:1000]
            elif field == "tasks" and isinstance(item, list):
                payload[field] = [
                    RunEventProjector._payload(task, ("id", "ordinal", "status"))
                    for task in item[:16]
                    if isinstance(task, Mapping)
                ]
            elif field == "usage" and isinstance(item, Mapping):
                payload[field] = {
                    str(key): item[key]
                    for key in ("input_tokens", "output_tokens", "total_tokens", "cost_micros")
                    if key in item
                }
            elif isinstance(item, (int, float, bool, list, dict)):
                payload[field] = item
        return payload
