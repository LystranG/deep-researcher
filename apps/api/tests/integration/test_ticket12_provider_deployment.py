import json
import os
import time
from uuid import uuid4

import pytest
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client

RUN_TICKET12_LIVE = os.getenv("DEEP_RESEARCHER_RUN_TICKET12_LIVE") == "1"
LIVE_DATABASE_URL = os.getenv("DEEP_RESEARCHER_TICKET12_TEST_DATABASE_URL")
LIVE_SETTINGS = Settings()
pytestmark = pytest.mark.skipif(
    not RUN_TICKET12_LIVE
    or not LIVE_DATABASE_URL
    or not LIVE_SETTINGS.openai_api_key
    or not LIVE_SETTINGS.brave_search_api_key
    or not LIVE_SETTINGS.embedding_api_key
    or not LIVE_SETTINGS.embedding_model
    or not LIVE_SETTINGS.rerank_api_key
    or not LIVE_SETTINGS.rerank_model,
    reason="未显式配置 ticket #12 真实 Provider 与隔离 PostgreSQL",
)


def _wait_for_attachment(client, headers: dict[str, str], attachment_id: str) -> dict:
    """等待真实 embedding 索引进入终态"""
    current: dict = {}
    for _ in range(2_400):
        current = client.get(
            f"/api/v1/attachments/{attachment_id}", headers=headers
        ).json()
        if current["status"] != "processing":
            return current
        time.sleep(0.05)
    raise AssertionError(f"附件索引未在预期时间内完成: {current}")


def _create_conversation(client, headers: dict[str, str], workspace_id: str) -> str:
    """创建 ticket #12 smoke 使用的会话"""
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "真实 Provider 组合"},
    ).json()["id"]


def _upload_ready_document(
    client,
    headers: dict[str, str],
    conversation_id: str,
    *,
    filename: str,
    content: str,
) -> dict:
    """上传并提升真实 Hybrid Retrieval 使用的 Workspace 文档"""
    uploaded = client.post(
        f"/api/v1/conversations/{conversation_id}/attachments",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
    ).json()
    ready = _wait_for_attachment(client, headers, uploaded["id"])
    assert ready["status"] == "ready", ready
    promoted = client.post(
        f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers
    )
    assert promoted.status_code == 201, promoted.text
    return ready


def test_real_provider_deployment_combination_completes_auditable_research_run(
    tmp_path,
) -> None:
    """验证真实模型、Brave、Jina 与 Hybrid Retrieval 组合运行"""
    assert LIVE_DATABASE_URL is not None
    settings = Settings(
        database_url=LIVE_DATABASE_URL,
        object_store_root=tmp_path / "ticket12-objects",
        sandbox_output_root=tmp_path / "ticket12-sandbox",
    )
    visible_evidence = (
        "Workspace provider verification code is TICKET12-VISIBLE-4096. "
        "This note is visible only to the ticket #12 research workspace."
    )
    hidden_evidence = (
        "Workspace provider verification code is TICKET12-HIDDEN-LEAK. "
        "This text must never influence another workspace."
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"ticket12-{uuid4()}@example.com",
                "password": "correct horse battery",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        visible_workspace_id = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": f"ticket12-visible-{uuid4()}"},
        ).json()["id"]
        hidden_workspace_id = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": f"ticket12-hidden-{uuid4()}"},
        ).json()["id"]
        visible_conversation_id = _create_conversation(
            client, headers, visible_workspace_id
        )
        hidden_conversation_id = _create_conversation(
            client, headers, hidden_workspace_id
        )
        _upload_ready_document(
            client,
            headers,
            visible_conversation_id,
            filename="visible-provider-note.txt",
            content=visible_evidence,
        )
        _upload_ready_document(
            client,
            headers,
            hidden_conversation_id,
            filename="hidden-provider-note.txt",
            content=hidden_evidence,
        )

        question = (
            "Use the Workspace provider verification note and public web evidence to "
            "explain the purpose of IANA example domains. Include the Workspace code."
        )
        run = client.post(
            f"/api/v1/conversations/{visible_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": f"ticket12-live-{uuid4()}"},
            json={"content": question},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        detail = client.get(
            f"/api/v1/conversations/{visible_conversation_id}/latest-run",
            headers=headers,
        ).json()
        messages = client.get(
            f"/api/v1/conversations/{visible_conversation_id}/messages",
            headers=headers,
        ).json()["items"]
        answer = messages[-1]["content"]
        sources = client.get(
            f"/api/v1/runs/{run['run_id']}/sources", headers=headers
        ).json()["items"]
        source_details = {
            source["id"]: client.get(
                f"/api/v1/sources/{source['id']}", headers=headers
            ).json()
            for source in sources
        }
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations",
            headers=headers,
        ).json()["items"]
        ledger = client.get(
            f"/api/v1/runs/{run['run_id']}/ledger", headers=headers
        ).json()

    readable_sources = [item for item in sources if item["content_kind"] == "web_page"]
    selected_attempts = [
        attempt
        for source in readable_sources
        for attempt in source["acquisition_attempts"]
        if attempt["selected_for_snapshot"]
    ]
    event_payloads = [
        json.loads(json.loads(line.removeprefix("data: ")))
        for line in events.text.splitlines()
        if line.startswith("data: ")
    ]
    assert detail["status"] in {"completed", "partial"}
    assert detail["usage"] is not None
    assert detail["usage"]["total_tokens"] > 0
    assert [task["role"] for task in detail["tasks"][:3]] == [
        "researcher",
        "verifier",
        "writer",
    ]
    for stage in ("planning", "researching", "verifying", "writing"):
        assert any(payload.get("stage") == stage for payload in event_payloads)
    retrieval_event = next(
        payload
        for payload in event_payloads
        if "consumed_tokens" in payload and "remaining_tokens" in payload
    )
    assert retrieval_event["consumed_tokens"] > 0
    assert retrieval_event["remaining_tokens"] >= 0
    assert retrieval_event["result_count"] > 0
    assert readable_sources
    assert any(attempt["adapter_id"] == "jina_reader" for attempt in selected_attempts)
    assert citations
    assert all(citation["source_hash"] for citation in citations)
    assert any(citation["evidence_text"] == visible_evidence for citation in citations)
    web_citations = [citation for citation in citations if citation["source_type"] == "web"]
    assert web_citations
    for citation in web_citations:
        matching_source = next(
            source
            for source in readable_sources
            if source["url"] == citation["source_url"]
        )
        assert matching_source["content_kind"] == "web_page"
        assert citation["source_hash"] == matching_source["content_hash"]
        source_detail = source_details[matching_source["id"]]
        assert citation["evidence_text"] in source_detail["content"]
    assert "TICKET12-HIDDEN-LEAK" not in answer
    assert all("TICKET12-HIDDEN-LEAK" not in citation["evidence_text"] for citation in citations)
    assert all(
        "snippet" not in citation["evidence_text"].casefold() for citation in citations
    )
    assert ledger["coverage"]["citation_count"] == len(citations)
    assert ledger["coverage"]["verified_claim_count"] == 1
    assert ledger["gaps"] == []
    assert ledger["stop_decision"]["reason"] == "evidence_complete"
    assert ledger["coverage"]["complete"] is (
        ledger["stop_decision"]["completeness"] == "complete"
    )
    assert ledger["missing_chunk_ids"] == []
