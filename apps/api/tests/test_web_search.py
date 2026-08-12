import hashlib
import json
from uuid import UUID

import httpx
import pytest
from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import EvidenceSpan, SourceChunk, SourceSnapshot
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_page import (
    JinaReaderWebPageAdapter,
    WebAcquisition,
    WebPageAttempt,
)
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


def test_jina_adapter_preserves_partial_extraction_provenance() -> None:
    """验证 Jina 截断正文被规范化为 partial 而非完整来源"""
    content = "页面正文用于验证 Jina 的截断与 warning 会进入统一获取结果。" * 4

    def handler(request: httpx.Request) -> httpx.Response:
        """返回确定性的 Jina JSON wire response"""
        assert str(request.url) == "https://r.jina.ai/https://example.com/report"
        assert request.headers["X-Preset"] == "research"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "data": {
                    "title": "Jina 报告",
                    "url": "https://example.com/report-final",
                    "content": content,
                    "contentType": "application/pdf",
                    "warning": "content was truncated",
                    "truncated": True,
                }
            },
        )

    adapter = JinaReaderWebPageAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    attempt = adapter.fetch("https://example.com/report")

    assert attempt.status == "success"
    assert attempt.final_url == "https://example.com/report-final"
    assert attempt.content_type == "application/pdf"
    assert attempt.warning_category == "provider_warning"
    assert attempt.completeness == "partial"
    assert attempt.truncated is True
    assert attempt.content_hash is not None


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


class SuccessfulJinaReader:
    """返回可引用的 Jina 正文"""

    def fetch(self, url: str) -> WebPageAttempt:
        """返回规范化的 Jina 成功结果"""
        return WebPageAttempt.success(
            adapter_id="jina_reader",
            adapter_version="hosted-v1",
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type="text/markdown",
            title="Jina 量子纠错进展",
            content="2026 年的实验把逻辑错误率降低了一半。",
            complete=True,
            truncated=False,
        )


class LocalReaderMustNotRun:
    """Jina 成功后禁止执行的本地 Reader"""

    def fetch(self, url: str) -> WebPageAttempt:
        """若 fallback 被错误触发则立即失败"""
        raise AssertionError(f"Jina 成功后不应执行 Local HTTP: {url}")


class LongPageWebSearchGateway:
    """为长页上下文测试返回单个网页候选"""

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        """返回需要先浏览 Manifest 的长页入口"""
        assert query == "浏览长篇研究报告"
        assert count == 5
        return [
            {
                "title": "长篇研究报告",
                "url": "https://example.com/long-report",
                "snippet": "报告包含方法、结果与附录。",
            }
        ]


class LongPageJinaReader:
    """返回带多个标题区段的完整长页"""

    def __init__(self, content: str) -> None:
        """保存要写入不可变 Snapshot 的长页正文"""
        self._content = content

    def fetch(self, url: str) -> WebPageAttempt:
        """返回可生成 Source Manifest 的 Jina 正文"""
        return WebPageAttempt.success(
            adapter_id="jina_reader",
            adapter_version="hosted-v1",
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type="text/markdown",
            title="长篇研究报告",
            content=self._content,
            complete=True,
            truncated=False,
        )


class CapturingResearcherGateway:
    """捕获 Researcher 可见的来源上下文"""

    def __init__(self) -> None:
        """初始化捕获列表"""
        self.inputs = []

    async def research(self, brief, sources):
        """记录公开输入并返回确定性研究结果"""
        self.inputs.append({"brief": brief, "sources": sources})
        return {
            "ordinal": brief["ordinal"],
            "status": "completed",
            "summary": "已浏览来源导航",
            "source_ids": [source["source_chunk_id"] for source in sources],
            "failure_impact": None,
        }


class CapturingGraphRunner:
    """捕获进入可恢复 Graph state 的研究上下文"""

    def __init__(self, researcher_gateway: CapturingResearcherGateway) -> None:
        """初始化真实 Graph 委托与上下文捕获"""
        self._delegate = ResearchGraphRunner(researcher_gateway=researcher_gateway)
        self.research_context = None

    def run(self, *args, **kwargs):
        """捕获 Graph 输入后执行真实确定性流程"""
        self.research_context = kwargs["research_context"]
        return self._delegate.run(*args, **kwargs)


class TamperingGraphRunner:
    """模拟恢复状态中的来源 hash 被篡改"""

    def __init__(self) -> None:
        """初始化真实 Graph 委托"""
        self._delegate = ResearchGraphRunner()

    def run(self, *args, **kwargs):
        """执行 Graph 后篡改冻结 Citation locator"""
        state = self._delegate.run(*args, **kwargs)
        for source in kwargs["research_context"]["sources"]:
            if "content_hash" in source:
                source["content_hash"] = "tampered-source-hash"
        return state


class CapturingModelGateway(ExtractiveModelGateway):
    """捕获 Writer 最终可见的模型输入"""

    def __init__(self) -> None:
        """初始化模型输入捕获"""
        self.contexts = []

    async def astream_answer(self, context):
        """记录输入并复用确定性抽取回答"""
        self.contexts.append(context)
        async for delta in super().astream_answer(context):
            yield delta


class TailFactWebSearchGateway:
    """返回事实位于长页尾部的网页候选"""

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        """返回需要 descriptor 检索才能定位的长页"""
        assert query == "查找 TAIL-FACT"
        assert count == 5
        return [
            {
                "title": "尾部事实报告",
                "url": "https://example.com/tail-report",
                "snippet": "报告包含多个长章节。",
            }
        ]


def test_research_run_discovers_tail_fact_and_cites_exact_snapshot_span(tmp_path) -> None:
    """验证长页尾部事实经邻近窗口读取后形成精确 Citation"""
    long_page = "\n\n".join(
        [
            "## 概览\n" + "背景材料 " * 900,
            "## 方法\n" + "实验步骤 " * 900,
            "## 结果\n" + "结果说明 " * 900,
            "## 附录\nTAIL-FACT 页面尾部的关键结论。" + " 附录说明" * 300,
        ]
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    model_gateway = CapturingModelGateway()
    with running_worker_client(
        settings,
        model_gateway=model_gateway,
        web_search_gateway=TailFactWebSearchGateway(),
        web_page_gateway=WebAcquisition(
            jina_reader=LongPageJinaReader(long_page),
            local_reader=LocalReaderMustNotRun(),
            url_validator=lambda _: None,
        ),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "tail-fact@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "尾部事实"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "长页尾部"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "tail-fact"},
            json={"content": "查找 TAIL-FACT"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{messages[-1]['id']}/citations", headers=headers
        ).json()["items"]
        with client.app.state.session_factory() as session:
            span = session.query(EvidenceSpan).filter_by(run_id=UUID(run["run_id"])).one()
            chunk = session.get(SourceChunk, span.source_chunk_id)
            snapshot = session.get(SourceSnapshot, chunk.source_snapshot_id)
            persisted = {
                "span_start": span.start_offset,
                "span_end": span.end_offset,
                "span_hash": span.content_hash,
                "chunk_text": chunk.text,
                "chunk_hash": chunk.content_hash,
                "snapshot_content": snapshot.content,
                "snapshot_hash": snapshot.content_hash,
            }

    assert "TAIL-FACT" in messages[-1]["content"]
    assert len(citations) == 1
    assert "TAIL-FACT" in citations[0]["evidence_text"]
    assert persisted["snapshot_content"][
        persisted["span_start"] : persisted["span_end"]
    ] == persisted["chunk_text"]
    assert persisted["span_hash"] == persisted["chunk_hash"]
    assert hashlib.sha256(persisted["chunk_text"].encode()).hexdigest() == persisted[
        "chunk_hash"
    ]
    assert hashlib.sha256(persisted["snapshot_content"].encode()).hexdigest() == persisted[
        "snapshot_hash"
    ]
    captured = json.dumps(
        [
            {
                "evidence": context.evidence,
                "source_manifests": context.source_manifests,
                "source_windows": context.source_windows,
            }
            for context in model_gateway.contexts
        ],
        ensure_ascii=False,
    )
    assert long_page not in captured
    assert "## 结果" in captured
    assert "TAIL-FACT" in captured


def test_long_page_enters_graph_as_bounded_manifest_without_snapshot_text(tmp_path) -> None:
    """验证长页只以稳定引用和有界 Manifest 进入 Agent 输入"""
    hidden_snapshot_text = "不可泄漏的完整正文标记"
    long_page = "\n\n".join(
        [
            f"## 第 {index} 节\n{hidden_snapshot_text}-{index} " + "研究内容" * 450
            for index in range(1, 9)
        ]
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    researcher = CapturingResearcherGateway()
    graph_runner = CapturingGraphRunner(researcher)
    model_gateway = CapturingModelGateway()

    with running_worker_client(
        settings,
        graph_runner=graph_runner,
        model_gateway=model_gateway,
        web_search_gateway=LongPageWebSearchGateway(),
        web_page_gateway=WebAcquisition(
            jina_reader=LongPageJinaReader(long_page),
            local_reader=LocalReaderMustNotRun(),
            url_validator=lambda _: None,
        ),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "manifest-first@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "长页 Manifest"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "长文上下文"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "manifest-first"},
            json={"content": "浏览长篇研究报告"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)

    assert graph_runner.research_context is not None
    graph_input = json.dumps(graph_runner.research_context, ensure_ascii=False)
    researcher_input = json.dumps(researcher.inputs, ensure_ascii=False)
    model_input = json.dumps(
        [
            {
                "evidence": context.evidence,
                "evidences": context.evidences,
                "conversation_leads": context.conversation_leads,
                "source_manifests": context.source_manifests,
            }
            for context in model_gateway.contexts
        ],
        ensure_ascii=False,
    )
    assert long_page not in graph_input
    assert long_page not in researcher_input
    assert long_page not in model_input
    manifest = graph_runner.research_context["sources"][0]["manifest"]
    assert model_gateway.contexts[0].source_manifests == (manifest,)
    assert manifest["snapshot_id"]
    assert manifest["completeness"] == "complete"
    assert manifest["chunk_count"] > 1
    assert manifest["token_profile"]["total"] > 0
    assert manifest["heading_navigation"][0]["heading_path"] == ["第 1 节"]
    assert len(graph_input) < len(long_page)


def test_jina_success_creates_cited_snapshot_without_local_fallback(tmp_path) -> None:
    """验证 Jina 正文成为唯一获取尝试及最终引用来源"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    acquisition = WebAcquisition(
        jina_reader=SuccessfulJinaReader(),
        local_reader=LocalReaderMustNotRun(),
        url_validator=lambda _: None,
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=FakeWebSearchGateway(),
        web_page_gateway=acquisition,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "jina-primary@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "Jina 主读取"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "量子纠错"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "jina-primary-success"},
            json={"content": "查找量子纠错最新进展"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{messages[-1]['id']}/citations", headers=headers
        ).json()["items"]
        sources = client.get(
            f"/api/v1/runs/{run['run_id']}/sources", headers=headers
        ).json()["items"]

    assert citations[0]["evidence_text"] == "2026 年的实验把逻辑错误率降低了一半。"
    assert sources[0]["content_preview"] == "2026 年的实验把逻辑错误率降低了一半。"
    assert sources[0]["acquisition_attempts"] == [
        {
            "adapter_id": "jina_reader",
            "adapter_version": "hosted-v1",
            "requested_url": "https://example.com/quantum-error-correction",
            "final_url": "https://example.com/quantum-error-correction",
            "status": "success",
            "http_status": 200,
            "content_type": "text/markdown",
            "warning_category": None,
            "error_category": None,
            "retryable": False,
            "completeness": "complete",
            "truncated": False,
            "content_hash": sources[0]["content_hash"],
            "selected_for_snapshot": True,
        }
    ]


def test_tampered_frozen_source_hash_cannot_create_citation(tmp_path) -> None:
    """验证 Citation 固化会回读并拒绝被篡改的来源 locator"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    acquisition = WebAcquisition(
        jina_reader=SuccessfulJinaReader(),
        local_reader=LocalReaderMustNotRun(),
        url_validator=lambda _: None,
    )

    with running_worker_client(
        settings,
        graph_runner=TamperingGraphRunner(),
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=FakeWebSearchGateway(),
        web_page_gateway=acquisition,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "tampered-source@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "来源校验"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "篡改检测"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "tampered-source"},
            json={"content": "查找量子纠错最新进展"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{messages[-1]['id']}/citations", headers=headers
        ).json()["items"]

    assert citations == []


class UnavailableJinaReader:
    """返回允许本地 fallback 的 Jina 网络失败"""

    def fetch(self, url: str) -> WebPageAttempt:
        """返回规范化的 Provider 暂时不可用结果"""
        return WebPageAttempt.failure(
            adapter_id="jina_reader",
            adapter_version="hosted-v1",
            requested_url=url,
            error_category="provider_unavailable",
            retryable=True,
            fallback_allowed=True,
        )


class SuccessfulLocalReader:
    """在 Jina 失败后提供本地静态正文"""

    def fetch(self, url: str) -> WebPageAttempt:
        """返回规范化的 Local HTTP 成功结果"""
        return WebPageAttempt.success(
            adapter_id="local_http",
            adapter_version="stdlib-html-v1",
            requested_url=url,
            final_url="https://example.com/quantum-error-correction-final",
            http_status=200,
            content_type="text/html; charset=utf-8",
            title="Local 量子纠错进展",
            content="2026 年的本地资料确认逻辑错误率降低了一半。",
            complete=True,
            truncated=False,
        )


class FailedLocalReader:
    """返回不可引用的 Local HTTP 网络失败"""

    def fetch(self, url: str) -> WebPageAttempt:
        """返回规范化的本地网络失败结果"""
        return WebPageAttempt.failure(
            adapter_id="local_http",
            adapter_version="stdlib-html-v1",
            requested_url=url,
            error_category="network_failure",
            retryable=True,
            fallback_allowed=False,
        )


class MisclassifiedStopJinaReader:
    """模拟把 must-stop 错标为可 fallback 的 Provider Adapter"""

    def __init__(self, error_category: str) -> None:
        """设置需要由 Web Acquisition 强制停止的错误类别"""
        self._error_category = error_category

    def fetch(self, url: str) -> WebPageAttempt:
        """返回错误标记为可 fallback 的失败结果"""
        return WebPageAttempt.failure(
            adapter_id="jina_reader",
            adapter_version="hosted-v1",
            requested_url=url,
            error_category=self._error_category,
            retryable=False,
            fallback_allowed=True,
        )


class UnsafeFinalUrlJinaReader:
    """模拟 Provider 将公开地址重定向到私网"""

    def fetch(self, url: str) -> WebPageAttempt:
        """返回正文成功但 final URL 不安全的结果"""
        return WebPageAttempt.success(
            adapter_id="jina_reader",
            adapter_version="hosted-v1",
            requested_url=url,
            final_url="http://127.0.0.1/private",
            http_status=200,
            content_type="text/markdown",
            title="不安全来源",
            content="这段正文不能越过最终地址的安全校验进入研究账本。" * 3,
            complete=True,
            truncated=False,
        )


@pytest.mark.parametrize(
    "error_category",
    ["policy_rejected", "acl_rejected", "constraint_exhausted", "unsafe_url"],
)
def test_must_stop_failure_cannot_trigger_local_fallback(error_category: str) -> None:
    """验证安全与约束停止类别优先于 Adapter fallback 标记"""
    acquisition = WebAcquisition(
        jina_reader=MisclassifiedStopJinaReader(error_category),
        local_reader=LocalReaderMustNotRun(),
        url_validator=lambda _: None,
    )

    result = acquisition.acquire("https://example.com/public")

    assert result.selected is None
    assert result.stopped_reason == error_category
    assert [attempt.error_category for attempt in result.attempts] == [error_category]


def test_cancellation_after_jina_failure_stops_before_local_fallback() -> None:
    """验证 Provider 返回后到 fallback 前的取消边界"""
    cancellation_checks = iter([False, True])
    acquisition = WebAcquisition(
        jina_reader=UnavailableJinaReader(),
        local_reader=LocalReaderMustNotRun(),
        url_validator=lambda _: None,
    )

    result = acquisition.acquire(
        "https://example.com/public",
        should_stop=lambda: next(cancellation_checks),
    )

    assert result.selected is None
    assert result.stopped_reason == "cancelled"
    assert [attempt.adapter_id for attempt in result.attempts] == ["jina_reader"]


def test_unsafe_url_stops_before_any_reader_attempt() -> None:
    """验证公共 URL 校验失败时不向任何 Reader 发送地址"""
    acquisition = WebAcquisition(
        jina_reader=LocalReaderMustNotRun(),
        local_reader=LocalReaderMustNotRun(),
        url_validator=lambda _: (_ for _ in ()).throw(ValueError("unsafe")),
    )

    result = acquisition.acquire("http://127.0.0.1/private")

    assert result.selected is None
    assert result.stopped_reason == "unsafe_url"
    assert result.attempts == ()


def test_unsafe_final_url_rejects_jina_content_without_local_fallback() -> None:
    """验证 Provider 重定向后的私网正文不能生成 Snapshot"""

    def validate_public_url(url: str) -> None:
        """只拒绝测试中的私网最终地址"""
        if "127.0.0.1" in url:
            raise ValueError("unsafe")

    acquisition = WebAcquisition(
        jina_reader=UnsafeFinalUrlJinaReader(),
        local_reader=LocalReaderMustNotRun(),
        url_validator=validate_public_url,
    )

    result = acquisition.acquire("https://example.com/public")

    assert result.selected is None
    assert result.stopped_reason == "unsafe_url"
    assert result.attempts[0].status == "failed"
    assert result.attempts[0].error_category == "unsafe_url"


def test_jina_failure_then_local_success_cites_local_snapshot(tmp_path) -> None:
    """验证 fallback 成功时保留两次尝试并引用本地正文"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    acquisition = WebAcquisition(
        jina_reader=UnavailableJinaReader(),
        local_reader=SuccessfulLocalReader(),
        url_validator=lambda _: None,
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=FakeWebSearchGateway(),
        web_page_gateway=acquisition,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "local-fallback@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "Local fallback"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "量子纠错"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "local-fallback-success"},
            json={"content": "查找量子纠错最新进展"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{messages[-1]['id']}/citations", headers=headers
        ).json()["items"]
        sources = client.get(
            f"/api/v1/runs/{run['run_id']}/sources", headers=headers
        ).json()["items"]

    assert citations[0]["source_url"] == (
        "https://example.com/quantum-error-correction-final"
    )
    assert citations[0]["evidence_text"] == "2026 年的本地资料确认逻辑错误率降低了一半。"
    assert [attempt["adapter_id"] for attempt in sources[0]["acquisition_attempts"]] == [
        "jina_reader",
        "local_http",
    ]
    assert [
        attempt["selected_for_snapshot"]
        for attempt in sources[0]["acquisition_attempts"]
    ] == [False, True]
    assert sources[0]["acquisition_attempts"][0]["error_category"] == (
        "provider_unavailable"
    )
    assert sources[0]["acquisition_attempts"][1]["content_hash"] == sources[0][
        "content_hash"
    ]


def test_both_readers_failing_creates_gap_without_citing_search_snippet(tmp_path) -> None:
    """验证正文均失败时搜索摘要只保留为发现线索"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    acquisition = WebAcquisition(
        jina_reader=UnavailableJinaReader(),
        local_reader=FailedLocalReader(),
        url_validator=lambda _: None,
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=FakeWebSearchGateway(),
        web_page_gateway=acquisition,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "readers-failed@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "Reader 失败"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "量子纠错"},
        ).json()["id"]
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "both-readers-failed"},
            json={"content": "查找量子纠错最新进展"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{messages[-1]['id']}/citations", headers=headers
        ).json()["items"]
        sources = client.get(
            f"/api/v1/runs/{run['run_id']}/sources", headers=headers
        ).json()["items"]
        ledger = client.get(
            f"/api/v1/runs/{run['run_id']}/ledger", headers=headers
        ).json()

    assert citations == []
    assert sources[0]["content_kind"] == "search_snippet"
    assert [attempt["status"] for attempt in sources[0]["acquisition_attempts"]] == [
        "failed",
        "failed",
    ]
    assert any(
        gap["description"]
        == "网页正文获取失败：jina_reader=provider_unavailable, local_http=network_failure"
        for gap in ledger["gaps"]
    )


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
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=FakeWebSearchGateway(),
        web_page_gateway=WebAcquisition(
            jina_reader=SingleResultWebPageGateway(),
            local_reader=None,
            url_validator=lambda _: None,
        ),
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
        web_page_gateway=WebAcquisition(
            jina_reader=StaticWebPageGateway(),
            local_reader=None,
            url_validator=lambda _: None,
        ),
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
