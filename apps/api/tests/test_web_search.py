import httpx
import pytest
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_search import (
    BraveWebSearchGateway,
    SearchResult,
    SearchUnavailableError,
)


def test_brave_search_returns_citable_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "test-key"
        assert request.url.params["q"] == "量子计算进展"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "量子计算年度报告",
                            "url": "https://example.com/report",
                            "description": "报告指出纠错能力在 2026 年继续提升。",
                        }
                    ]
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = BraveWebSearchGateway(api_key="test-key", client=client)

    results = gateway.search("量子计算进展", count=3)

    assert results == [
        {
            "title": "量子计算年度报告",
            "url": "https://example.com/report",
            "snippet": "报告指出纠错能力在 2026 年继续提升。",
        }
    ]


def test_brave_search_reports_upstream_failure() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"error": "limit"}))
    )
    gateway = BraveWebSearchGateway(api_key="test-key", client=client)

    with pytest.raises(SearchUnavailableError, match="Brave 搜索暂时不可用"):
        gateway.search("研究问题")


class FakeWebSearchGateway:
    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        assert query == "查找量子纠错最新进展"
        assert count == 5
        return [
            {
                "title": "量子纠错进展",
                "url": "https://example.com/quantum-error-correction",
                "snippet": "2026 年的实验把逻辑错误率降低了一半。",
            }
        ]


def test_research_without_local_evidence_uses_web_snapshot_and_citation(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(settings, web_search_gateway=FakeWebSearchGateway()) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "web@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "网页研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "量子计算"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "web-search-001"},
            json={"content": "查找量子纠错最新进展"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]
        citations = client.get(f"/api/v1/messages/{messages[-1]['id']}/citations", headers=headers)

    assert "event: tool_started" in events.text
    assert "event: tool_completed" in events.text
    assert messages[-1]["content"] == "根据资料：2026 年的实验把逻辑错误率降低了一半。 [1]"
    citation = citations.json()["items"][0]
    source_captured_at = citation.pop("source_captured_at")
    assert source_captured_at.endswith("Z")
    assert citation == {
        "id": citation["id"],
        "label": 1,
        "source_type": "web",
        "filename": "量子纠错进展",
        "source_url": "https://example.com/quantum-error-correction",
        "document_version": None,
        "page_number": None,
        "evidence_text": "2026 年的实验把逻辑错误率降低了一半。",
        "source_hash": citation["source_hash"],
    }
