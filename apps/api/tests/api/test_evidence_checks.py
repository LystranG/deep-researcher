import time

from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client


def test_selected_claim_is_checked_against_the_answer_source_span(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        token = client.post(
            "/api/v1/auth/register",
            json={"email": "verify@example.com", "password": "correct horse battery"},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "证据核验"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "核验数字"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={
                "file": (
                    "market.txt",
                    "权威报告显示，2025年市场规模为100亿元。".encode(),
                    "text/plain",
                )
            },
        ).json()
        for _ in range(100):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] == "ready":
                break
            time.sleep(0.02)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "verify-claim"},
            json={
                "content": "2025年市场规模是多少？",
                "attachment_ids": [attachment["id"]],
            },
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        assistant = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-1]
        selected = "2025年市场规模为100亿元"
        start = assistant["content"].index(selected)
        checked = client.post(
            f"/api/v1/messages/{assistant['id']}/evidence-checks",
            headers=headers,
            json={
                "message_version": assistant["version"],
                "start_char": start,
                "end_char": start + len(selected),
                "text": selected,
            },
        )

    assert checked.status_code == 201
    assert checked.json()["verdict"] == "supported"
    assert checked.json()["evidence"][0]["filename"] == "market.txt"
    assert selected in checked.json()["evidence"][0]["evidence_text"]
    assert "当前可获得证据的支持度" in checked.json()["disclaimer"]
