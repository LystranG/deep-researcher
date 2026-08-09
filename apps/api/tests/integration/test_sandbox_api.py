import time
from pathlib import Path

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

        created = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-executions",
            headers=headers,
            json={
                "purpose": "验证授权输入并生成结果",
                "code": """
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
""",
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
            f"/api/v1/runs/{other_run['run_id']}/sandbox-executions",
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
        created = client.post(
            f"/api/v1/runs/{run['run_id']}/sandbox-executions",
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
                f"/api/v1/sandbox-executions/{created['id']}", headers=headers
            ).json()
            if current["status"] == "running":
                break
            time.sleep(0.02)
        cancelled = client.post(
            f"/api/v1/sandbox-executions/{created['id']}/cancel", headers=headers
        )
        assert cancelled.status_code == 200, cancelled.text
        execution = wait_for_execution(client, headers, created["id"])

    assert execution["status"] == "cancelled"
    assert execution["artifacts"] == []


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
        response = client.get(f"/api/v1/sandbox-executions/{execution_id}", headers=headers)
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
