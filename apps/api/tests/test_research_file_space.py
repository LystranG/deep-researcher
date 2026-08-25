from uuid import UUID

import pytest
from deep_researcher.app import create_app
from deep_researcher.models import ResearchTask, RunEvent
from deep_researcher.research_file_space import (
    ResearchFileAccessError,
    ResearchFileConflict,
    ResearchFileRef,
    ResearchFileStore,
)
from deep_researcher.settings import Settings
from deep_researcher.task_runtime import TaskRuntime, TaskToolCall
from fastapi.testclient import TestClient
from sqlalchemy import select


def register(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "files@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_conversation(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "File Space"}
    ).json()["id"]
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "研究文件"},
    ).json()["id"]
    return workspace_id, conversation_id


def test_run_freezes_source_revision_when_it_is_created(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'source-freeze.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        _, conversation_id = create_conversation(client, headers)
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("facts.txt", "frozen facts", "text/plain")},
        ).json()
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "source-freeze"},
            json={"content": "分析附件", "attachment_ids": [attachment["id"]]},
        ).json()
        before = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()
        with app.state.session_factory() as session:
            file_event = session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == UUID(created["run_id"]),
                    RunEvent.type == "file_revision_committed",
                )
            )

        with app.state.session_factory.begin() as session:
            from deep_researcher.models import Attachment

            upstream = session.get(Attachment, UUID(attachment["id"]))
            assert upstream is not None
            upstream.sha256 = "0" * 64

        after = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()

    assert before["run_id"] == created["run_id"]
    assert before["files"]["sources"] == after["files"]["sources"]
    assert before["files"]["sources"][0]["name"] == "facts.txt"
    assert before["files"]["sources"][0]["ref"]["kind"] == "source"
    assert before["files"]["sources"][0]["content_hash"] == attachment["sha256"]
    assert file_event is not None
    assert file_event.payload["ref"] == before["files"]["sources"][0]["ref"]


def test_work_revisions_are_isolated_cas_written_and_idempotently_published(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'work-files.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(settings, embedded_worker=False)

    with TestClient(app) as client:
        headers = register(client)
        workspace_id, conversation_id = create_conversation(client, headers)
        run_id = UUID(
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={**headers, "Idempotency-Key": "work-revisions"},
                json={"content": "生成研究产物"},
            ).json()["run_id"]
        )
        with app.state.session_factory() as session:
            tasks = session.scalars(
                select(ResearchTask)
                .where(ResearchTask.run_id == run_id)
                .order_by(ResearchTask.ordinal)
            ).all()
        store = ResearchFileStore(app.state.session_factory)

        first = store.write(
            workspace_id=UUID(workspace_id),
            run_id=run_id,
            task_id=tasks[0].id,
            name="reports/summary.md",
            content="revision one",
            idempotency_key="write-1",
        )
        replay = store.write(
            workspace_id=UUID(workspace_id),
            run_id=run_id,
            task_id=tasks[0].id,
            name="reports/summary.md",
            content="ignored replay payload",
            idempotency_key="write-1",
        )
        second = store.write(
            workspace_id=UUID(workspace_id),
            run_id=run_id,
            task_id=tasks[0].id,
            name="reports/summary.md",
            content="revision two",
            expected_revision=first.ref,
            idempotency_key="write-2",
        )

        with pytest.raises(ResearchFileConflict) as conflict:
            store.write(
                workspace_id=UUID(workspace_id),
                run_id=run_id,
                task_id=tasks[0].id,
                name="reports/summary.md",
                content="stale overwrite",
                expected_revision=first.ref,
                idempotency_key="write-stale",
            )
        with pytest.raises(ResearchFileAccessError):
            store.read(
                workspace_id=UUID(workspace_id),
                run_id=run_id,
                task_id=tasks[1].id,
                ref=second.ref,
            )
        shared = store.read(
            workspace_id=UUID(workspace_id),
            run_id=run_id,
            task_id=tasks[1].id,
            ref=second.ref,
            shared_refs=(second.ref,),
        )
        published = store.publish(
            workspace_id=UUID(workspace_id),
            run_id=run_id,
            task_id=tasks[0].id,
            work_ref=second.ref,
            name="summary.md",
            idempotency_key="publish-1",
        )
        publish_replay = store.publish(
            workspace_id=UUID(workspace_id),
            run_id=run_id,
            task_id=tasks[0].id,
            work_ref=second.ref,
            name="renamed-on-replay.md",
            idempotency_key="publish-1",
        )
        claim = TaskRuntime(app.state.session_factory).claim(
            tasks[0].id, lease_owner="file-tool-worker", lease_seconds=30
        )
        assert claim is not None
        file_write = app.state.task_tool_registry.get("file_write")
        assert file_write is not None and file_write.handler is not None
        observation = file_write.handler.execute(
            claim,
            TaskToolCall(
                tool_name="file_write",
                arguments={"name": "tool-notes.txt", "content": "from tool observation"},
                logical_call_ref="file-tool-write-1",
            ),
        )
        file_stat = app.state.task_tool_registry.get("file_stat")
        assert file_stat is not None and file_stat.handler is not None
        stat_observation = file_stat.handler.execute(
            claim,
            TaskToolCall(
                tool_name="file_stat",
                arguments={"ref": observation.file_refs[0]},
                logical_call_ref="file-tool-stat-1",
            ),
        )

    assert replay.ref == first.ref
    assert second.ref.revision == "2"
    assert second.parent_ref == first.ref
    assert conflict.value.current_ref == second.ref
    assert shared.content == "revision two"
    assert published.ref == publish_replay.ref
    assert published.source_ref == second.ref
    assert published.content_hash == second.content_hash
    assert observation.status == "succeeded"
    assert observation.file_refs[0]["kind"] == "work"
    assert observation.result_reference == (
        "research-file://work/"
        f"{observation.file_refs[0]['id']}/{observation.file_refs[0]['revision']}"
    )
    assert "from tool observation" not in (stat_observation.summary or "")
    with pytest.raises(ValueError):
        ResearchFileRef.parse(
            f"research-file://work/{observation.file_refs[0]['id']}/"
        )
