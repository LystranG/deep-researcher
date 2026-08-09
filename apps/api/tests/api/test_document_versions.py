import time

from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client


def test_replacing_document_keeps_old_citation_on_original_version(tmp_path) -> None:
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
            "/api/v1/workspaces", headers=headers, json={"name": "版本研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "版本核验"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "核心指标旧值为 10。", "text/plain")},
        ).json()
        for _ in range(50):
            ready = client.get(f"/api/v1/attachments/{attachment['id']}", headers=headers).json()
            if ready["status"] != "processing":
                break
            time.sleep(0.01)
        document = client.post(
            f"/api/v1/attachments/{attachment['id']}/promote", headers=headers
        ).json()

        first_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "version-one-query"},
            json={"content": "核心指标旧值是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{first_run['run_id']}/events", headers=headers)
        old_citation = client.get(
            f"/api/v1/messages/{first_run['assistant_message_id']}/citations", headers=headers
        ).json()["items"][0]

        replaced = client.post(
            f"/api/v1/documents/{document['id']}/versions",
            headers=headers,
            files={"file": ("metric.txt", "核心指标新值为 20。", "text/plain")},
        )
        old_citation_after_replace = client.get(
            f"/api/v1/citations/{old_citation['id']}", headers=headers
        )

    assert replaced.status_code == 201
    assert replaced.json()["version"] == 2
    assert old_citation_after_replace.json()["document_version"] == 1
    assert old_citation_after_replace.json()["evidence_text"] == "核心指标旧值为 10。"
