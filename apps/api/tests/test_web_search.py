import httpx
import pytest
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_search import (
    BraveWebSearchGateway,
    DisabledWebSearchGateway,
    SearchResult,
    SearchUnavailableError,
)


class FailIfCalledModelGateway:
    """验证搜索失败后不得继续调用模型"""

    async def astream_answer(self, context):
        """模型被调用时立即暴露错误路径"""
        del context
        raise AssertionError("网页搜索不可用时不应调用模型")


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


class SingleResultWebPageGateway:
    """为单个搜索结果提供已读取的网页正文"""

    def fetch(self, url: str) -> dict[str, object] | None:
        """返回与搜索结果对应的完整证据"""
        assert url == "https://example.com/quantum-error-correction"
        return {
            "title": "量子纠错进展",
            "content": "2026 年的实验把逻辑错误率降低了一半。",
            "truncated": False,
        }


class YearQuestionWebSearchGateway:
    """为包含年份的普通研究问题返回网页证据"""

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        """返回可引用的年度研究资料"""
        assert query == "2026 年量子纠错有哪些重要进展？"
        assert count == 5
        return [
            {
                "title": "量子纠错年度进展",
                "url": "https://example.com/quantum-2026",
                "snippet": "研究团队报告了更低的逻辑错误率。",
            }
        ]


class MultipleResultWebSearchGateway:
    """返回多个网页候选以验证来源列表"""

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        """返回五条不同的搜索结果"""
        assert query == "解释 Pi Agent 的基本架构和运行方式"
        assert count == 5
        return [
            {
                "title": f"来源 {index}",
                "url": f"https://example.com/pi-agent/{index}",
                "snippet": f"来源 {index} 的搜索摘要。",
            }
            for index in range(1, 6)
        ]


class StaticWebPageGateway:
    """为前三个候选返回已提取的网页正文"""

    def fetch(self, url: str) -> dict[str, object] | None:
        """返回可查看的正文或表示未读取"""
        if url.endswith(("/1", "/2", "/3")):
            index = url.rsplit("/", maxsplit=1)[-1]
            return {
                "title": f"正文来源 {index}",
                "content": f"这是来源 {index} 被研究系统实际读取的网页正文。",
                "truncated": False,
            }
        return None


def test_research_without_local_evidence_uses_web_snapshot_and_citation(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings,
        web_search_gateway=FakeWebSearchGateway(),
        web_page_gateway=SingleResultWebPageGateway(),
    ) as client:
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


def test_missing_web_search_configuration_stops_before_model_conclusion(tmp_path) -> None:
    """验证搜索未配置时公开原因且不伪造研究结论"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        openai_api_key="configured-model-key",
    )
    with running_worker_client(
        settings,
        model_gateway=FailIfCalledModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "missing-search@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "网页研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "搜索配置"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "missing-web-search"},
            json={"content": "查找最新量子纠错进展"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert "event: tool_skipped" in events.text
    assert "event: run_failed" in events.text
    assert "event: run_completed" not in events.text
    assert messages[-1]["content"] == (
        "网页检索不可用：未配置 Brave Search API 凭证。"
        "请配置 DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY 后重试。"
    )


def test_year_in_research_question_does_not_create_python_sandbox_todo(tmp_path) -> None:
    """验证普通年份问题不会误触发 Python Sandbox"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings, web_search_gateway=YearQuestionWebSearchGateway()
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "year-search@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "年份研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "量子计算"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "year-without-computation"},
            json={"content": "2026 年量子纠错有哪些重要进展？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        todos = client.get(
            f"/api/v1/runs/{run['run_id']}/todos", headers=headers
        ).json()["items"]

    assert all(todo["kind"] != "python_sandbox" for todo in todos)


def test_research_exposes_multiple_sources_and_readable_page_snapshot(tmp_path) -> None:
    """验证用户可查看多条搜索来源及已读取正文"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings,
        web_search_gateway=MultipleResultWebSearchGateway(),
        web_page_gateway=StaticWebPageGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "multiple-sources@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "多来源研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "Pi Agent"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "multiple-web-sources"},
            json={"content": "解释 Pi Agent 的基本架构和运行方式"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        sources = client.get(f"/api/v1/runs/{run['run_id']}/sources", headers=headers)

        assert sources.status_code == 200
        items = sources.json()["items"]
        readable = next(item for item in items if item["content_kind"] == "web_page")
        snapshot = client.get(f"/api/v1/sources/{readable['id']}", headers=headers)

    assert "event: source_discovered" in events.text
    assert [item["title"] for item in items] == [
        "正文来源 1",
        "正文来源 2",
        "正文来源 3",
        "来源 4",
        "来源 5",
    ]
    assert [item["content_kind"] for item in items] == [
        "web_page",
        "web_page",
        "web_page",
        "search_snippet",
        "search_snippet",
    ]
    assert snapshot.json()["content"] == "这是来源 1 被研究系统实际读取的网页正文。"
