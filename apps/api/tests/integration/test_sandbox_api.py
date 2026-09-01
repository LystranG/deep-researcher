import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

from deep_researcher.models import (
    Citation,
    DerivedEvidence,
    EvidenceSpan,
    ResearchArtifactRevision,
    ResearchRun,
    ResearchTask,
    SandboxAttempt,
    SandboxJob,
    SandboxObservation,
    SourceChunk,
    StopDecision,
    WorkspaceMember,
)
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient


def test_authorized_sandbox_input_and_artifact_are_workspace_scoped(tmp_path) -> None:
    test_root = Path("var/sandbox-api-tests") / tmp_path.name
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=test_root / "objects",
        sandbox_output_root=test_root / "sandbox-output",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "sandbox-api@example.com")
        workspace_id, conversation_id = create_workspace_conversation(client, headers, "沙箱 A")
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("input.txt", b"AUTHORIZED_INPUT", "text/plain")},
        ).json()
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "sandbox-source-run"},
            json={"content": "分析输入文件", "attachment_ids": [attachment["id"]]},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        sandbox_code = """
from pathlib import Path
import socket

print(Path('/inputs/input.txt').read_text())
print('HOST_BLOCKED' if not Path('/Users/lystran/.ssh/id_rsa').exists() else 'HOST_LEAKED')
try:
    socket.create_connection(('example.com', 443), timeout=1)
    print('NETWORK_LEAKED')
except OSError:
    print('NETWORK_BLOCKED')
Path('/output/result.txt').write_text('可下载研究产物')
"""
        created = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-jobs",
            headers=headers,
            json={
                "purpose": "验证授权输入并生成结果",
                "code": sandbox_code,
                "attachment_ids": [attachment["id"]],
                "timeout_seconds": 10,
            },
        )
        execution = wait_for_execution(client, headers, created.json()["id"])
        assert execution["status"] == "completed", execution
        artifact = execution["artifacts"][0]
        downloaded = client.get(f"/api/v1/artifacts/{artifact['id']}/download", headers=headers)

        other_workspace_id, other_conversation_id = create_workspace_conversation(
            client, headers, "沙箱 B"
        )
        other_run = client.post(
            f"/api/v1/conversations/{other_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "other-sandbox-run"},
            json={"content": "尝试读取其他空间附件"},
        ).json()
        client.get(f"/api/v1/runs/{other_run['run_id']}/events", headers=headers)
        cross_workspace = client.post(
            f"/api/v1/runs/{other_run['run_id']}/sandbox-jobs",
            headers=headers,
            json={
                "purpose": f"空间 {other_workspace_id} 不应读取空间 {workspace_id}",
                "code": "print('never runs')",
                "attachment_ids": [attachment["id"]],
                "timeout_seconds": 5,
            },
        )

    assert created.status_code == 202
    assert execution["status"] == "completed"
    assert execution["stdout"].splitlines() == [
        "AUTHORIZED_INPUT",
        "HOST_BLOCKED",
        "NETWORK_BLOCKED",
    ]
    assert artifact["filename"] == "result.txt"
    assert artifact["sha256"]
    assert downloaded.content == "可下载研究产物".encode()
    assert cross_workspace.status_code == 404


def test_successful_sandbox_persists_reproducible_derived_evidence(tmp_path) -> None:
    """验证成功执行产生可回读且哈希稳定的派生证据"""
    test_root = Path("var/sandbox-api-tests") / tmp_path.name
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=test_root / "objects",
        sandbox_output_root=test_root / "sandbox-output",
    )
    sandbox_code = "print('DERIVED_RESULT')"

    with running_worker_client(settings) as client:
        headers = register(client, "derived-success@example.com")
        workspace_id, conversation_id = create_workspace_conversation(
            client, headers, "派生证据"
        )
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("input.txt", b"AUTHORIZED_INPUT", "text/plain")},
        ).json()
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "derived-success"},
            json={"content": "生成派生证据", "attachment_ids": [attachment["id"]]},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        trigger_message_id = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-2]["id"]
        with client.app.state.session_factory.begin() as session:
            source_chunk = session.query(SourceChunk).filter_by(
                attachment_id=UUID(attachment["id"])
            ).one()
            evidence_span = EvidenceSpan(
                workspace_id=UUID(workspace_id),
                run_id=UUID(run["run_id"]),
                source_chunk_id=source_chunk.id,
                start_offset=source_chunk.start_offset,
                end_offset=source_chunk.end_offset,
                content_hash=source_chunk.content_hash,
            )
            session.add(evidence_span)
            session.flush()
            evidence_span_id = evidence_span.id
        created = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-jobs",
            headers=headers,
            json={
                "purpose": "验证派生结果",
                "code": sandbox_code,
                "attachment_ids": [attachment["id"]],
                "evidence_span_ids": [str(evidence_span_id)],
                "timeout_seconds": 10,
            },
        ).json()
        execution = wait_for_execution(client, headers, created["id"])
        derived_evidence = execution["derived_evidence"]
        readback = client.get(
            f"/api/v1/derived-evidence/{derived_evidence['id']}", headers=headers
        ).json()
        ledger = client.get(
            f"/api/v1/runs/{run['run_id']}/ledger", headers=headers
        ).json()
    expected_result = json.dumps(
        {
            "artifact_hashes": [],
            "stdout_hash": derived_evidence["stdout_hash"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert derived_evidence["input_message_ids"] == [trigger_message_id]
    assert derived_evidence["input_attachment_ids"] == [attachment["id"]]
    assert derived_evidence["input_evidence_span_ids"] == [str(evidence_span_id)]
    assert derived_evidence["code_hash"] == hashlib.sha256(sandbox_code.encode()).hexdigest()
    assert derived_evidence["stdout_hash"] == hashlib.sha256(
        execution["stdout"].encode()
    ).hexdigest()
    assert derived_evidence["result_hash"] == hashlib.sha256(
        expected_result.encode()
    ).hexdigest()
    assert readback == derived_evidence
    assert ledger["derived_evidence"] == [derived_evidence]


def test_derived_evidence_readback_requires_workspace_membership(tmp_path) -> None:
    """验证失去 Workspace 成员资格后不能回读派生证据"""
    test_root = Path("var/sandbox-api-tests") / tmp_path.name
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=test_root / "objects",
        sandbox_output_root=test_root / "sandbox-output",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "derived-acl@example.com")
        workspace_id, conversation_id = create_workspace_conversation(
            client, headers, "派生证据 ACL"
        )
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "derived-acl"},
            json={"content": "准备 ACL 回读"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        current_user_id = UUID(client.get("/api/v1/auth/me", headers=headers).json()["id"])
        with client.app.state.session_factory.begin() as session:
            task = session.query(ResearchTask).filter_by(
                run_id=UUID(run["run_id"])
            ).order_by(ResearchTask.ordinal).first()
            assert task is not None
            execution = SandboxJob(
                workspace_id=UUID(workspace_id),
                run_id=UUID(run["run_id"]),
                task_id=task.id,
                invocation_key=f"test:{run['run_id']}",
                parameters_hash=hashlib.sha256(b"safe").hexdigest(),
                purpose="inspect_data",
                code="print('safe')",
                timeout_seconds=5,
                status="completed",
            )
            session.add(execution)
            session.flush()
            attempt = SandboxAttempt(
                job_id=execution.id,
                attempt_number=1,
                lease_owner="test",
                lease_expires_at=execution.created_at,
                status="completed",
            )
            session.add(attempt)
            session.flush()
            session.add(
                SandboxObservation(
                    job_id=execution.id,
                    attempt_id=attempt.id,
                    status="completed",
                    attempt_count=1,
                    stdout_preview="safe\n",
                )
            )
            evidence = DerivedEvidence(
                workspace_id=UUID(workspace_id),
                run_id=UUID(run["run_id"]),
                sandbox_job_id=execution.id,
                purpose=execution.purpose,
                code_hash=hashlib.sha256(execution.code.encode()).hexdigest(),
                stdout="safe\n",
                stdout_hash=hashlib.sha256(b"safe\n").hexdigest(),
                result_hash=hashlib.sha256(b"stable-result").hexdigest(),
            )
            session.add(evidence)
            session.flush()
            evidence_id = evidence.id
        owner_readback = client.get(
            f"/api/v1/derived-evidence/{evidence_id}", headers=headers
        )
        with client.app.state.session_factory.begin() as session:
            session.query(WorkspaceMember).filter_by(
                workspace_id=UUID(workspace_id), user_id=current_user_id
            ).delete()
        forbidden_readback = client.get(
            f"/api/v1/derived-evidence/{evidence_id}", headers=headers
        )

    assert owner_readback.status_code == 200
    assert "code" not in owner_readback.json()
    assert "stdout" not in owner_readback.json()
    assert forbidden_readback.status_code == 404


def test_user_can_cancel_running_sandbox_execution(tmp_path) -> None:
    test_root = Path("var/sandbox-api-tests") / tmp_path.name
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=test_root / "objects",
        sandbox_output_root=test_root / "sandbox-output",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "sandbox-cancel@example.com")
        _, conversation_id = create_workspace_conversation(client, headers, "取消沙箱")
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "cancel-source-run"},
            json={"content": "准备执行"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        with client.app.state.session_factory() as session:
            citations_before = session.query(Citation).filter_by(
                message_id=UUID(run["assistant_message_id"])
            ).count()
            artifacts_before = session.query(ResearchArtifactRevision).filter_by(
                run_id=UUID(run["run_id"])
            ).count()
            evidence_before = session.query(DerivedEvidence).filter_by(
                run_id=UUID(run["run_id"])
            ).count()
        created = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-jobs",
            headers=headers,
            json={
                "purpose": "验证取消传播",
                "code": "import time; time.sleep(20)",
                "attachment_ids": [],
                "timeout_seconds": 30,
            },
        ).json()
        for _ in range(100):
            current = client.get(
                f"/api/v1/sandbox-jobs/{created['id']}", headers=headers
            ).json()
            if current["status"] == "running":
                break
            time.sleep(0.02)
        cancelled = client.post(
            f"/api/v1/sandbox-jobs/{created['id']}/cancel", headers=headers
        )
        assert cancelled.status_code == 200, cancelled.text
        execution = wait_for_execution(client, headers, created["id"])
        with client.app.state.session_factory() as session:
            citations_after = session.query(Citation).filter_by(
                message_id=UUID(run["assistant_message_id"])
            ).count()
            artifacts_after = session.query(ResearchArtifactRevision).filter_by(
                run_id=UUID(run["run_id"])
            ).count()
            evidence_after = session.query(DerivedEvidence).filter_by(
                run_id=UUID(run["run_id"])
            ).count()

    assert execution["status"] == "cancelled"
    assert execution["artifacts"] == []
    assert citations_after == citations_before
    assert artifacts_after == artifacts_before
    assert evidence_after == evidence_before


def test_run_cancellation_prevents_new_sandbox_ledger_facts(tmp_path) -> None:
    """验证研究运行取消后不会发布新的派生证据、引用或产物"""
    test_root = Path("var/sandbox-api-tests") / tmp_path.name
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=test_root / "objects",
        sandbox_output_root=test_root / "sandbox-output",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "run-cancel@example.com")
        _, conversation_id = create_workspace_conversation(client, headers, "取消研究运行")
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "cancel-entire-run"},
            json={"content": "准备执行后取消"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        with client.app.state.session_factory.begin() as session:
            persisted_run = session.get(ResearchRun, UUID(run["run_id"]))
            assert persisted_run is not None
            persisted_run.status = "running"
            persisted_run.completed_at = None
            session.query(StopDecision).filter_by(
                workspace_id=persisted_run.workspace_id
            ).delete()
        with client.app.state.session_factory() as session:
            citations_before = session.query(Citation).filter_by(
                message_id=UUID(run["assistant_message_id"])
            ).count()
            artifacts_before = session.query(ResearchArtifactRevision).filter_by(
                run_id=UUID(run["run_id"])
            ).count()
            evidence_before = session.query(DerivedEvidence).filter_by(
                run_id=UUID(run["run_id"])
            ).count()
        created = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-jobs",
            headers=headers,
            json={
                "purpose": "取消后不能发布结果",
                "code": (
                    "import time\n"
                    "from pathlib import Path\n"
                    "time.sleep(1)\n"
                    "print('late result')\n"
                    "Path('/output/late.txt').write_text('late artifact')\n"
                ),
                "attachment_ids": [],
                "timeout_seconds": 10,
            },
        ).json()
        for _ in range(100):
            current = client.get(
                f"/api/v1/sandbox-jobs/{created['id']}", headers=headers
            ).json()
            if current["status"] == "running":
                break
            time.sleep(0.02)
        cancelled = client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=headers)
        rejected_after_cancel = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-jobs",
            headers=headers,
            json={
                "purpose": "取消后不能再创建",
                "code": "print('never runs')",
                "attachment_ids": [],
                "timeout_seconds": 5,
            },
        )
        execution = wait_for_execution(client, headers, created["id"])
        with client.app.state.session_factory() as session:
            citations_after = session.query(Citation).filter_by(
                message_id=UUID(run["assistant_message_id"])
            ).count()
            artifacts_after = session.query(ResearchArtifactRevision).filter_by(
                run_id=UUID(run["run_id"])
            ).count()
            evidence_after = session.query(DerivedEvidence).filter_by(
                run_id=UUID(run["run_id"])
            ).count()

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert rejected_after_cancel.status_code == 409
    assert execution["status"] == "cancelled"
    assert evidence_after == evidence_before
    assert citations_after == citations_before
    assert artifacts_after == artifacts_before


def register(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_workspace_conversation(
    client: TestClient, headers: dict[str, str], name: str
) -> tuple[str, str]:
    workspace_id = client.post("/api/v1/workspaces", headers=headers, json={"name": name}).json()[
        "id"
    ]
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "沙箱执行"},
    ).json()["id"]
    return workspace_id, conversation_id


def wait_for_execution(
    client: TestClient, headers: dict[str, str], execution_id: str
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for _ in range(300):
        response = client.get(
            f"/api/v1/sandbox-jobs/{execution_id}/detail", headers=headers
        )
        payload = response.json()
        if payload["status"] in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "unavailable",
        }:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"沙箱执行未在预期时间内结束: {payload}")
