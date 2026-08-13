import hashlib
import time
from io import BytesIO
from uuid import UUID

from deep_researcher.model_gateway import AnswerContext, ExtractiveModelGateway
from deep_researcher.models import ConversationSegment
from deep_researcher.settings import Settings
from deep_researcher.source_map import SourceMapContext, SourceMapDigest
from deep_researcher.testing import running_worker_client
from deep_researcher.web_page import WebAcquisition
from deep_researcher.web_search import DisabledWebSearchGateway
from reportlab.pdfgen import canvas
from sqlalchemy import select


class UnknownCitationGateway:
    """模拟模型输出冻结来源之外的引用编号"""

    def stream_answer(self, context):
        """返回一个无法由当前 SourceSet 支持的回答"""
        yield "未经证据支持的结论 [2]"


class DeterministicEmbeddingGateway:
    """为端到端检索测试生成稳定向量"""

    model_name = "test-embedding-v1"

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        """按输入顺序返回固定维度向量"""
        return [(1.0, float(index + 1)) for index, _ in enumerate(texts)]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """为查询返回同维度向量"""
        del text
        return (1.0, 1.0)


class StableRerankGateway:
    """按召回顺序稳定返回精排结果"""

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """返回预算内的候选下标和稳定分数"""
        del query
        return [(index, 1.0 - index / 100) for index in range(min(len(documents), top_n))]


class CombinedSourceWebSearchGateway:
    """为组合 Research Run 返回可审计的网页发现结果"""

    def __init__(self) -> None:
        """初始化查询记录"""
        self.queries: list[str] = []

    def search(self, query: str, *, count: int = 5) -> list[dict[str, str]]:
        """记录查询并返回不具备 Citation 资格的发现 snippet"""
        self.queries.append(query)
        assert count == 5
        return [
            {
                "title": "组合来源公告",
                "url": "https://example.com/combined-source",
                "snippet": "BRAVE-SNIPPET 只能用于发现",
            }
        ]


class ExternalExtractiveGateway:
    """模拟已配置外部模型但保留确定性回答内容的 Gateway"""

    requires_web_research = True

    def __init__(self) -> None:
        """初始化内部确定性委托"""
        self._delegate = ExtractiveModelGateway()

    def stream_answer(self, context: AnswerContext):
        """转发同步回答生成"""
        yield from self._delegate.stream_answer(context)

    async def astream_answer(self, context: AnswerContext):
        """转发异步回答生成"""
        async for delta in self._delegate.astream_answer(context):
            yield delta

    async def acomplete_map_work(self, context: SourceMapContext) -> SourceMapDigest:
        """转发有界整页分析"""
        return await self._delegate.acomplete_map_work(context)


class CombinedSourceWebPageGateway:
    """为组合 Research Run 返回可持久化网页正文"""

    def fetch(self, url: str) -> dict[str, object]:
        """返回与 discovery snippet 不同的网页证据"""
        assert url == "https://example.com/combined-source"
        return {
            "title": "组合来源公告",
            "content": "网页证据编号为 WEB-2048。",
            "truncated": False,
        }


class ConversationLeadGateway:
    """将召回到的历史会话线索原样展示给端到端测试"""

    def stream_answer(self, context):
        """返回第一条明确标注为低信任的历史会话线索"""
        leads = getattr(context, "conversation_leads", ())
        if leads:
            yield f"根据历史会话线索：{leads[0]}"
            return
        yield "未召回历史会话线索"


class EvidenceAndConversationLeadGateway:
    """优先展示证据，否则展示召回的历史会话线索"""

    def stream_answer(self, context):
        """返回带引用的证据或明确标注的低信任线索"""
        evidences = getattr(context, "evidences", ())
        evidence = evidences[0] if evidences else getattr(context, "evidence", None)
        if evidence is not None:
            yield f"根据资料：{evidence} [1]"
            return
        leads = getattr(context, "conversation_leads", ())
        if leads:
            yield f"根据历史会话线索：{leads[0]}"
            return
        yield "未召回历史会话线索"


def test_indexed_workspace_document_is_cited_from_another_conversation(tmp_path) -> None:
    """验证异步索引后的空间文档可跨会话回读并引用原文"""
    evidence = "混合检索验收编号为 VECTOR-2048。"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "混合检索"}
        ).json()["id"]
        upload_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料上传"},
        ).json()["id"]
        query_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料查询"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{upload_conversation_id}/attachments",
            headers=headers,
            files={"file": ("retrieval.txt", evidence, "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        promoted = client.post(
            f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers
        )

        run = client.post(
            f"/api/v1/conversations/{query_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "hybrid-retrieval-query"},
            json={"content": "混合检索验收编号是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{query_conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
        )
        ledger = client.get(f"/api/v1/runs/{run['run_id']}/ledger", headers=headers)

    assert attachment["status"] == "ready"
    assert promoted.status_code == 201
    assert "VECTOR-2048" in answer
    assert citations[0]["source_type"] == "workspace_document"
    assert citations[0]["evidence_text"] == evidence
    assert citations[0]["source_hash"] == hashlib.sha256(evidence.encode()).hexdigest()
    assert records.status_code == 200
    assert len(records.json()["items"]) == 1
    assert records.json()["items"][0]["status"] == "verified"
    assert "VECTOR-2048" in records.json()["items"][0]["claim_text"]
    assert records.json()["items"][0]["evidence"][0]["source_hash"] == citations[0]["source_hash"]
    assert ledger.status_code == 200
    assert ledger.json()["status"] == "completed"
    assert ledger.json()["stop_decision"]["completeness"] == "complete"
    assert ledger.json()["coverage"]["citation_count"] == 1


def test_external_research_combines_workspace_retrieval_with_web_acquisition(tmp_path) -> None:
    """验证外部模型运行同时保留 Workspace 检索与网页正文来源"""
    workspace_evidence = "Workspace 证据编号为 VECTOR-4096。"
    web_search = CombinedSourceWebSearchGateway()
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
        openai_api_key="configured-for-external-run",
    )

    with running_worker_client(
        settings,
        model_gateway=ExternalExtractiveGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=web_search,
        web_page_gateway=WebAcquisition(
            jina_reader=CombinedSourceWebPageGateway(),
            local_reader=None,
            url_validator=lambda _: None,
        ),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "combined@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "组合来源"}
        ).json()["id"]
        upload_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料上传"},
        ).json()["id"]
        query_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "组合检索"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{upload_conversation_id}/attachments",
            headers=headers,
            files={"file": ("workspace.txt", workspace_evidence, "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)

        question = "同时核验 Workspace 与网页证据编号"
        run = client.post(
            f"/api/v1/conversations/{query_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "combined-research"},
            json={"content": question},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        sources = client.get(
            f"/api/v1/runs/{run['run_id']}/sources", headers=headers
        ).json()["items"]
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert web_search.queries == [question]
    assert "event: tool_completed" in events.text
    assert sources[0]["content_kind"] == "web_page"
    assert "WEB-2048" in sources[0]["content_preview"]
    assert citations[0]["source_type"] == "workspace_document"
    assert citations[0]["evidence_text"] == workspace_evidence
    assert "BRAVE-SNIPPET" not in citations[0]["evidence_text"]


def test_other_conversation_is_recalled_only_as_lead_without_citation(tmp_path) -> None:
    """验证同空间旧会话只能作为低信任线索且不能形成 Citation"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ConversationLeadGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "lead@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "历史线索"}
        ).json()["id"]
        source_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "旧会话"},
        ).json()["id"]
        query_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "新会话"},
        ).json()["id"]
        source_run = client.post(
            f"/api/v1/conversations/{source_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "conversation-lead-source"},
            json={"content": "历史会话验收编号为 LEAD-731。"},
        ).json()
        client.get(f"/api/v1/runs/{source_run['run_id']}/events", headers=headers)
        ready_segment = None
        for _ in range(50):
            with client.app.state.session_factory() as session:
                ready_segment = session.scalar(
                    select(ConversationSegment).where(
                        ConversationSegment.conversation_id == UUID(source_conversation_id),
                        ConversationSegment.embedding_status == "ready",
                    )
                )
            if ready_segment is not None:
                break
            time.sleep(0.01)

        run = client.post(
            f"/api/v1/conversations/{query_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "conversation-lead-query"},
            json={"content": "LEAD-731 是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{query_conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert ready_segment is not None
    assert "历史会话线索" in answer
    assert "LEAD-731" in answer
    assert citations == []


def test_private_attachment_answer_stays_out_of_workspace_retrieval(tmp_path) -> None:
    """验证私有附件回答不会提升为研究记录或泄漏到其他会话线索"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=EvidenceAndConversationLeadGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "private-lead@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "私有附件隔离"}
        ).json()["id"]
        source_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "私有资料"},
        ).json()["id"]
        query_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "隔离验证"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{source_conversation_id}/attachments",
            headers=headers,
            files={"file": ("private.txt", "私有验收码为 PRIVATE-887。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)

        source_run = client.post(
            f"/api/v1/conversations/{source_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "private-lead-source"},
            json={"content": "私有验收码是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{source_run['run_id']}/events", headers=headers)
        source_answer = client.get(
            f"/api/v1/conversations/{source_conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]
        source_citations = client.get(
            f"/api/v1/messages/{source_run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]
        ready_segment = None
        for _ in range(50):
            with client.app.state.session_factory() as session:
                ready_segment = session.scalar(
                    select(ConversationSegment).where(
                        ConversationSegment.conversation_id == UUID(source_conversation_id),
                        ConversationSegment.embedding_status == "ready",
                        ConversationSegment.text.contains("PRIVATE-887"),
                    )
                )
            if ready_segment is not None:
                break
            time.sleep(0.01)
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
        ).json()["items"]

        query_run = client.post(
            f"/api/v1/conversations/{query_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "private-lead-query"},
            json={"content": "PRIVATE-887 是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{query_run['run_id']}/events", headers=headers)
        query_answer = client.get(
            f"/api/v1/conversations/{query_conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert "PRIVATE-887" in source_answer
    assert source_citations
    assert ready_segment is not None
    assert ready_segment.visibility_scope == "conversation"
    assert records == []
    assert "PRIVATE-887" not in query_answer


def test_unknown_citation_falls_back_to_frozen_evidence(tmp_path) -> None:
    """验证未知引用不会进入最终消息和 Citation"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=UnknownCitationGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "引用校验"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "冻结来源"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "可验证指标为 42。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)

        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "unknown-citation"},
            json={"content": "可验证指标是多少？"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert answer == "根据资料：可验证指标为 42。 [1]"
    assert "[2]" not in events.text
    assert [citation["label"] for citation in citations] == [1]


def test_promoting_attachment_changes_cross_conversation_retrieval_and_adds_citation(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "口径研究"}
        ).json()["id"]
        conversation_a = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料上传"},
        ).json()["id"]
        conversation_b = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料问答"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_a}/attachments",
            headers=headers,
            files={"file": ("scope.txt", "秘密统计口径为 Alpha-42。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(f"/api/v1/attachments/{uploaded['id']}", headers=headers).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)

        private_run = client.post(
            f"/api/v1/conversations/{conversation_b}/messages",
            headers={**headers, "Idempotency-Key": "private-query"},
            json={"content": "秘密统计口径是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{private_run['run_id']}/events", headers=headers)
        private_messages = client.get(
            f"/api/v1/conversations/{conversation_b}/messages", headers=headers
        ).json()["items"]

        client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)
        shared_run = client.post(
            f"/api/v1/conversations/{conversation_b}/messages",
            headers={**headers, "Idempotency-Key": "shared-query"},
            json={"content": "秘密统计口径是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{shared_run['run_id']}/events", headers=headers)
        shared_messages = client.get(
            f"/api/v1/conversations/{conversation_b}/messages", headers=headers
        ).json()["items"]
        citation_list = client.get(
            f"/api/v1/messages/{shared_run['assistant_message_id']}/citations",
            headers=headers,
        )
        citation = client.get(
            f"/api/v1/citations/{citation_list.json()['items'][0]['id']}", headers=headers
        )

    assert "Alpha-42" not in private_messages[-1]["content"]
    assert "Alpha-42" in shared_messages[-1]["content"]
    assert shared_messages[-1]["content"].endswith("[1]")
    assert citation_list.status_code == 200
    assert citation.json()["filename"] == "scope.txt"
    assert citation.json()["document_version"] == 1
    assert citation.json()["evidence_text"] == "秘密统计口径为 Alpha-42。"


def test_same_named_documents_never_cross_workspace_boundary(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        conversations: list[str] = []
        for workspace_name, content in [
            ("空间 A", "唯一隔离标记为 A-ONLY。"),
            ("空间 B", "唯一隔离标记为 B-SECRET。"),
        ]:
            workspace_id = client.post(
                "/api/v1/workspaces", headers=headers, json={"name": workspace_name}
            ).json()["id"]
            conversation_id = client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "隔离验证"},
            ).json()["id"]
            conversations.append(conversation_id)
            uploaded = client.post(
                f"/api/v1/conversations/{conversation_id}/attachments",
                headers=headers,
                files={"file": ("same-name.txt", content, "text/plain")},
            ).json()
            for _ in range(50):
                attachment = client.get(
                    f"/api/v1/attachments/{uploaded['id']}", headers=headers
                ).json()
                if attachment["status"] != "processing":
                    break
                time.sleep(0.01)
            client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)

        run = client.post(
            f"/api/v1/conversations/{conversations[0]}/messages",
            headers={**headers, "Idempotency-Key": "workspace-a-query"},
            json={"content": "唯一隔离标记是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversations[0]}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert "A-ONLY" in answer
    assert "B-SECRET" not in answer


def test_pdf_citation_opens_exact_page_and_evidence_span(tmp_path) -> None:
    pdf = BytesIO()
    writer = canvas.Canvas(pdf)
    writer.drawString(72, 720, "The 2025 market size was 30 billion dollars.")
    writer.showPage()
    writer.drawString(72, 720, "This is unrelated second-page material.")
    writer.save()

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "PDF 研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "PDF 引用"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("market.pdf", pdf.getvalue(), "application/pdf")},
        ).json()
        for _ in range(50):
            attachment = client.get(f"/api/v1/attachments/{uploaded['id']}", headers=headers).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "pdf-query"},
            json={"content": "What was the 2025 market size?"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert citations[0]["filename"] == "market.pdf"
    assert citations[0]["page_number"] == 1
    assert "30 billion dollars" in citations[0]["evidence_text"]
