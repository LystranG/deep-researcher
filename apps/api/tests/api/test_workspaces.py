from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient


def register(client: TestClient, email: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_users_can_create_same_named_workspaces_without_seeing_each_other(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        alice = register(client, "alice@example.com")
        bob = register(client, "bob@example.com")

        alice_workspace = client.post(
            "/api/v1/workspaces",
            headers=alice,
            json={"name": "新能源汽车研究", "description": "Alice 的空间"},
        )
        bob_workspace = client.post(
            "/api/v1/workspaces",
            headers=bob,
            json={"name": "新能源汽车研究", "description": "Bob 的空间"},
        )

        alice_list = client.get("/api/v1/workspaces", headers=alice)
        bob_list = client.get("/api/v1/workspaces", headers=bob)

    assert alice_workspace.status_code == 201
    assert bob_workspace.status_code == 201
    assert alice_workspace.json()["id"] != bob_workspace.json()["id"]
    assert [workspace["description"] for workspace in alice_list.json()["items"]] == [
        "Alice 的空间"
    ]
    assert [workspace["description"] for workspace in bob_list.json()["items"]] == ["Bob 的空间"]


def test_archived_workspace_is_hidden_and_can_be_restored(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "researcher@example.com")
        created = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "长期研究"},
        ).json()

        archived = client.post(f"/api/v1/workspaces/{created['id']}/archive", headers=headers)
        active = client.get("/api/v1/workspaces", headers=headers)
        including_archived = client.get("/api/v1/workspaces?include_archived=true", headers=headers)
        restored = client.post(f"/api/v1/workspaces/{created['id']}/restore", headers=headers)
        active_after_restore = client.get("/api/v1/workspaces", headers=headers)

    assert archived.status_code == 200
    assert active.json()["items"] == []
    assert including_archived.json()["items"][0]["archived"] is True
    assert restored.status_code == 200
    assert active_after_restore.json()["items"][0]["id"] == created["id"]


def test_deleting_workspace_requires_name_confirmation_and_revokes_access(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers = register(client, "researcher@example.com")
        workspace = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "待删除空间"},
        ).json()
        conversation = client.post(
            f"/api/v1/workspaces/{workspace['id']}/conversations",
            headers=headers,
            json={"title": "已有会话"},
        ).json()

        preview = client.get(
            f"/api/v1/workspaces/{workspace['id']}/delete-preview", headers=headers
        )
        wrong_confirmation = client.request(
            "DELETE",
            f"/api/v1/workspaces/{workspace['id']}",
            headers=headers,
            json={"confirm_name": "错误名称"},
        )
        deleted = client.request(
            "DELETE",
            f"/api/v1/workspaces/{workspace['id']}",
            headers=headers,
            json={"confirm_name": "待删除空间"},
        )
        conversation_after_delete = client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=headers
        )

    assert preview.json()["conversations"] == 1
    assert preview.json()["documents"] == 0
    assert wrong_confirmation.status_code == 409
    assert deleted.status_code == 204
    assert conversation_after_delete.status_code == 404


def test_workspace_information_can_be_updated(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(settings) as client:
        headers = register(client, "researcher@example.com")
        workspace = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "旧名称"}
        ).json()
        updated = client.patch(
            f"/api/v1/workspaces/{workspace['id']}",
            headers=headers,
            json={
                "name": "新名称",
                "description": "行业跟踪",
                "instructions": "只采用公开来源",
            },
        )

    assert updated.status_code == 200
    assert updated.json()["name"] == "新名称"
    assert updated.json()["description"] == "行业跟踪"
    assert updated.json()["instructions"] == "只采用公开来源"
