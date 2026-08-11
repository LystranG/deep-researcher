import time

from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient


class FailingEmbeddingGateway:
    """模拟文档 embedding Provider 不可用"""

    model_name = "test-embedding-v1"

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        """模拟 Provider 在文档索引阶段失败"""
        del texts
        raise RuntimeError("provider unavailable")

    def embed_query(self, text: str) -> tuple[float, ...]:
        """模拟 Provider 在查询阶段失败"""
        del text
        raise RuntimeError("provider unavailable")


def setup_conversation(client: TestClient) -> tuple[dict[str, str], str, str]:
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": "researcher@example.com", "password": "correct horse battery"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "研究空间"}
    ).json()["id"]
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "资料分析"},
    ).json()["id"]
    return headers, workspace_id, conversation_id


def test_uploaded_text_file_becomes_available_with_stable_source_metadata(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers, _, conversation_id = setup_conversation(client)
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("market.txt", "2026 年市场规模为 120 亿元。", "text/plain")},
        )
        attachment_id = uploaded.json()["id"]

        current = uploaded
        for _ in range(50):
            current = client.get(f"/api/v1/attachments/{attachment_id}", headers=headers)
            if current.json()["status"] != "processing":
                break
            time.sleep(0.01)

    assert uploaded.status_code == 202
    assert current.json()["status"] == "ready"
    assert current.json()["filename"] == "market.txt"
    assert current.json()["size_bytes"] == len("2026 年市场规模为 120 亿元。".encode())
    assert len(current.json()["sha256"]) == 64
    assert current.json()["failure_reason"] is None


def test_embedding_failure_is_visible_without_losing_uploaded_document(tmp_path) -> None:
    """验证索引失败会显式暴露且原上传文档仍可回读"""
    content = "该文档会保留，但索引 Provider 当前不可用。"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings, embedding_gateway=FailingEmbeddingGateway()
    ) as client:
        headers, _, conversation_id = setup_conversation(client)
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("provider-failure.txt", content, "text/plain")},
        ).json()
        for _ in range(50):
            current = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if current["status"] != "processing":
                break
            time.sleep(0.01)

    assert current["status"] == "failed"
    assert current["failure_reason"] == "文档 embedding 索引失败"
    assert current["filename"] == "provider-failure.txt"
    assert current["sha256"] == uploaded["sha256"]


def test_attachment_becomes_workspace_document_only_after_explicit_promotion(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers, workspace_id, conversation_id = setup_conversation(client)
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("private.txt", "只在明确提升后共享", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(f"/api/v1/attachments/{uploaded['id']}", headers=headers).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)

        before = client.get(f"/api/v1/workspaces/{workspace_id}/documents", headers=headers)
        promoted = client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)
        after = client.get(f"/api/v1/workspaces/{workspace_id}/documents", headers=headers)

    assert before.json()["items"] == []
    assert promoted.status_code == 201
    assert promoted.json()["filename"] == "private.txt"
    assert promoted.json()["version"] == 1
    assert [item["filename"] for item in after.json()["items"]] == ["private.txt"]


def test_private_attachment_cannot_be_attached_to_message_in_another_conversation(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        headers, workspace_id, conversation_a = setup_conversation(client)
        conversation_b = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "另一个会话"},
        ).json()["id"]
        attachment_id = client.post(
            f"/api/v1/conversations/{conversation_a}/attachments",
            headers=headers,
            files={"file": ("private.txt", "私有资料", "text/plain")},
        ).json()["id"]

        attempted = client.post(
            f"/api/v1/conversations/{conversation_b}/messages",
            headers={**headers, "Idempotency-Key": "cross-conversation-attachment"},
            json={"content": "读取附件", "attachment_ids": [attachment_id]},
        )

    assert attempted.status_code == 404
