from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient


def send_and_wait(
    client: TestClient,
    headers: dict[str, str],
    conversation_id: str,
    content: str,
    key: str,
) -> str:
    run = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={**headers, "Idempotency-Key": key},
        json={"content": content},
    ).json()
    client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
    return client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers).json()[
        "items"
    ][-1]["content"]


def test_correction_affects_only_followups_in_current_conversation(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "多轮研究"}
        ).json()["id"]
        conversation_a = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "会话 A"},
        ).json()["id"]
        conversation_b = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "会话 B"},
        ).json()["id"]

        send_and_wait(client, headers, conversation_a, "纠正：第二点是上海。", "correction")
        answer_in_a = send_and_wait(client, headers, conversation_a, "第二点是什么？", "followup-a")
        answer_in_b = send_and_wait(client, headers, conversation_b, "第二点是什么？", "followup-b")

    assert "第二点是上海" in answer_in_a
    assert "上海" not in answer_in_b
