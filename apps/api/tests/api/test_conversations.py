from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient


def register(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_conversations_are_visible_only_to_workspace_members(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        owner = register(client, "owner@example.com")
        outsider = register(client, "outsider@example.com")
        workspace_id = client.post(
            "/api/v1/workspaces", headers=owner, json={"name": "空间"}
        ).json()["id"]

        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=owner,
            json={"title": "市场规模"},
        )
        owner_list = client.get(f"/api/v1/workspaces/{workspace_id}/conversations", headers=owner)
        outsider_list = client.get(
            f"/api/v1/workspaces/{workspace_id}/conversations", headers=outsider
        )

    assert created.status_code == 201
    assert [item["title"] for item in owner_list.json()["items"]] == ["市场规模"]
    assert outsider_list.status_code == 404


def test_conversation_can_be_renamed_archived_restored_and_deleted(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "researcher@example.com")
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "空间"}
        ).json()["id"]
        conversation = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "旧标题"},
        ).json()

        renamed = client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=headers,
            json={"title": "新标题"},
        )
        client.post(f"/api/v1/conversations/{conversation['id']}/archive", headers=headers)
        active = client.get(f"/api/v1/workspaces/{workspace_id}/conversations", headers=headers)
        client.post(f"/api/v1/conversations/{conversation['id']}/restore", headers=headers)
        restored = client.get(f"/api/v1/workspaces/{workspace_id}/conversations", headers=headers)
        deleted = client.delete(f"/api/v1/conversations/{conversation['id']}", headers=headers)
        after_delete = client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
        )

    assert renamed.json()["title"] == "新标题"
    assert active.json()["items"] == []
    assert restored.json()["items"][0]["title"] == "新标题"
    assert deleted.status_code == 204
    assert after_delete.status_code == 404
