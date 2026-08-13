import os
from uuid import uuid4

import httpx
import pytest
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_page import (
    HttpWebPageGateway,
    JinaReaderWebPageAdapter,
    WebAcquisition,
)
from deep_researcher.web_search import SearchResult

RUN_JINA_LIVE = os.getenv("DEEP_RESEARCHER_RUN_JINA_LIVE") == "1"
PUBLIC_PAGE_URL = "https://example.com/"
pytestmark = pytest.mark.skipif(
    not RUN_JINA_LIVE,
    reason="未显式启用真实 Jina Provider 验证",
)


class ExampleDomainSearchGateway:
    """把固定公开网页作为真实正文获取入口"""

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        """返回不作为 Citation 证据的固定发现线索"""
        assert query == "查找 Example Domain 的公开说明"
        assert count == 5
        return [
            {
                "title": "Example Domain",
                "url": PUBLIC_PAGE_URL,
                "snippet": "仅用于发现网页，不能直接成为 Citation",
            }
        ]


class LocalReaderMustNotRun:
    """暴露不应发生的 Local HTTP fallback"""

    def fetch(self, url: str):
        """Jina 成功或约束停止后调用本方法即使测试失败"""
        raise AssertionError(f"Local HTTP 不应读取 {url}")


def _run_research(tmp_path, acquisition: WebAcquisition) -> tuple[list[dict], list[dict]]:
    """执行真实网页获取的完整 Research Run 并返回来源与引用"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'jina-live.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=ExampleDomainSearchGateway(),
        web_page_gateway=acquisition,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"jina-live-{uuid4()}@example.com",
                "password": "correct horse battery",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "真实 Jina 验证"},
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "公开网页读取"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": f"jina-live-{uuid4()}"},
            json={"content": "查找 Example Domain 的公开说明"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
        ).json()["items"]
        sources = client.get(
            f"/api/v1/runs/{run['run_id']}/sources",
            headers=headers,
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{messages[-1]['id']}/citations",
            headers=headers,
        ).json()["items"]
    return sources, citations


def test_real_jina_content_is_persisted_before_citation(tmp_path) -> None:
    """验证真实 Jina 正文先形成不可变 Snapshot 再被 Citation 引用"""
    with httpx.Client(timeout=30.0, follow_redirects=False) as jina_client:
        sources, citations = _run_research(
            tmp_path,
            WebAcquisition(
                jina_reader=JinaReaderWebPageAdapter(client=jina_client),
                local_reader=LocalReaderMustNotRun(),
            ),
        )

    source = sources[0]
    attempt = source["acquisition_attempts"][0]
    assert source["content_kind"] == "web_page"
    assert "Example Domain" in source["content_preview"]
    assert citations[0]["source_url"] == PUBLIC_PAGE_URL
    assert "Example Domain" in citations[0]["evidence_text"]
    assert attempt["adapter_id"] == "jina_reader"
    assert attempt["status"] == "success"
    assert attempt["selected_for_snapshot"] is True
    assert attempt["content_hash"] == source["content_hash"]


def test_real_jina_timeout_falls_back_to_local_http_with_two_attempts(tmp_path) -> None:
    """验证真实 Jina 超时后 Local HTTP 成功且两次尝试分别落账"""
    with (
        httpx.Client(timeout=0.000001, follow_redirects=False) as jina_client,
        httpx.Client(timeout=10.0, follow_redirects=False) as local_client,
    ):
        sources, citations = _run_research(
            tmp_path,
            WebAcquisition(
                jina_reader=JinaReaderWebPageAdapter(client=jina_client),
                local_reader=HttpWebPageGateway(client=local_client),
            ),
        )

    source = sources[0]
    attempts = source["acquisition_attempts"]
    assert [attempt["adapter_id"] for attempt in attempts] == [
        "jina_reader",
        "local_http",
    ]
    assert [attempt["status"] for attempt in attempts] == ["failed", "success"]
    assert [attempt["selected_for_snapshot"] for attempt in attempts] == [False, True]
    assert attempts[0]["error_category"] == "provider_unavailable"
    assert attempts[1]["content_hash"] == source["content_hash"]
    assert citations[0]["source_url"] == PUBLIC_PAGE_URL
    assert "Example Domain" in citations[0]["evidence_text"]


def test_real_jina_token_budget_failure_stops_without_fallback() -> None:
    """验证真实 Jina 超预算错误规范化后不能绕过约束触发 fallback"""
    with httpx.Client(
        headers={"X-Token-Budget": "1"},
        timeout=30.0,
        follow_redirects=False,
    ) as jina_client:
        result = WebAcquisition(
            jina_reader=JinaReaderWebPageAdapter(client=jina_client),
            local_reader=LocalReaderMustNotRun(),
        ).acquire(PUBLIC_PAGE_URL)

    assert result.selected is None
    assert result.stopped_reason == "constraint_exhausted"
    assert [attempt.adapter_id for attempt in result.attempts] == ["jina_reader"]
    assert result.attempts[0].http_status == 409
    assert result.attempts[0].fallback_allowed is False
