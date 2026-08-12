from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import deep_researcher.retrieval as retrieval_module
import pytest
from deep_researcher.app import create_app
from deep_researcher.database import build_engine, build_session_factory
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import (
    Attachment,
    ConversationSegment,
    EvidenceSpan,
    ResearchRecord,
    SourceChunk,
)
from deep_researcher.retrieval import (
    ContextBudget,
    HybridRetrieval,
    LiteLLMTokenEstimator,
    RetrievalCandidate,
    RetrievalRequest,
)
from deep_researcher.settings import Settings
from deep_researcher.web_search import DisabledWebSearchGateway
from fastapi.testclient import TestClient


class FixedRerankGateway:
    """返回固定顺序的精排结果"""

    def __init__(self, order: list[int]) -> None:
        """保存测试期望的候选下标顺序"""
        self.order = order

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """按固定下标顺序返回确定性精排结果"""
        del query, documents
        return [(index, float(top_n - position)) for position, index in enumerate(self.order)]


class FailingRerankGateway:
    """模拟精排 Provider 失败"""

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """始终抛出 Provider 不可用错误"""
        del query, documents, top_n
        raise RuntimeError("rerank provider unavailable")


class RecordingRerankGateway:
    """记录真正进入精排的候选正文"""

    def __init__(self) -> None:
        """初始化精排调用记录"""
        self.documents: list[list[str]] = []

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """记录可见候选并保持原顺序"""
        del query
        self.documents.append(list(documents))
        return [(index, 1.0) for index in range(min(len(documents), top_n))]


class ControlledTokenEstimator:
    """按测试词表返回模型兼容 token 数"""

    def __init__(self, token_counts: dict[str, int]) -> None:
        """保存每段输入的确定性 token 数"""
        self._token_counts = token_counts

    def count_tokens(self, text: str) -> int:
        """返回文本对应的受控 token 数"""
        return self._token_counts[text]


class RecordingEmbeddingGateway:
    """记录查询并返回固定向量"""

    def __init__(self, embedding: tuple[float, ...]) -> None:
        """保存测试使用的查询向量"""
        self._embedding = embedding
        self.queries: list[str] = []

    @property
    def model_name(self) -> str:
        """返回测试 embedding 模型名"""
        return "test-embedding"

    def embed_documents(self, texts) -> list[tuple[float, ...]]:
        """返回文档数量对应的固定向量"""
        return [self._embedding for _ in texts]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """记录检索查询并返回固定向量"""
        self.queries.append(text)
        return self._embedding


class FailingEmbeddingGateway(RecordingEmbeddingGateway):
    """模拟查询 embedding Provider 失败"""

    def embed_query(self, text: str) -> tuple[float, ...]:
        """始终抛出 Provider 不可用错误"""
        del text
        raise RuntimeError("embedding provider unavailable")


class StaticSourceChunkAdapter:
    """返回已通过可见性过滤的固定候选"""

    def __init__(self, candidates: list[RetrievalCandidate]) -> None:
        """保存统一 Retrieval Interface 的候选输入"""
        self._candidates = candidates
        self.query_embeddings: list[tuple[float, ...]] = []

    def recall(self, session, **kwargs) -> list[RetrievalCandidate]:
        """返回不依赖数据库内容的确定性候选"""
        del session
        self.query_embeddings.append(kwargs["query_embedding"])
        return self._candidates


def _candidate(candidate_id: str, text: str, embedding: tuple[float, ...]) -> RetrievalCandidate:
    """构造混合排序测试使用的候选"""
    return RetrievalCandidate(
        candidate_id=candidate_id,
        text=text,
        source_kind="document",
        content_hash=candidate_id,
        embedding=embedding,
    )


def test_hybrid_retrieval_uses_rerank_order_for_final_candidates() -> None:
    """验证精排结果会决定最终候选顺序"""
    candidates = [
        _candidate("first", "alpha evidence", (1.0, 0.0)),
        _candidate("second", "alpha beta evidence", (0.8, 0.2)),
    ]

    result = HybridRetrieval(FixedRerankGateway([1, 0])).rank(
        "alpha",
        candidates,
        limit=2,
        query_embedding=(1.0, 0.0),
    )

    assert [item.candidate.candidate_id for item in result] == ["second", "first"]


def test_retrieval_page_reserves_context_and_uses_injected_token_estimator() -> None:
    """验证固定上下文预留后只用剩余 token 组装 evidence page"""
    candidates = [
        _candidate("first", "证据甲", (1.0, 0.0)),
        _candidate("second", "evidence beta", (0.9, 0.1)),
    ]
    estimator = ControlledTokenEstimator(
        {
            "policy": 2,
            "conversation": 3,
            "tools": 2,
            "证据甲": 4,
            "evidence beta": 5,
        }
    )
    embedding_gateway = RecordingEmbeddingGateway((1.0, 0.0))
    source_adapter = StaticSourceChunkAdapter(candidates)
    retrieval = HybridRetrieval(
        FixedRerankGateway([0, 1]),
        embedding_gateway=embedding_gateway,
        token_estimator=estimator,
        session_factory=build_session_factory(build_engine("sqlite://")),
        source_chunk_adapter=source_adapter,
    )

    request = RetrievalRequest(
        workspace_id=UUID(int=1),
        user_id=UUID(int=2),
        conversation_id=UUID(int=3),
        query="evidence 证据",
        source_kinds=("source_chunk",),
        candidate_limit=10,
        result_limit=2,
        context_budget=ContextBudget(
            model_context_tokens=20,
            policy_and_prompt="policy",
            compact_conversation="conversation",
            tool_schema="tools",
            requested_output_reserve=4,
            safety_margin=2,
        ),
    )
    page = retrieval.retrieve(request)

    assert embedding_gateway.queries == ["evidence 证据"]
    assert source_adapter.query_embeddings == [(1.0, 0.0)]
    assert [item.candidate.candidate_id for item in page.items] == ["first"]
    assert page.consumed_tokens == 4
    assert page.remaining_tokens == 3
    assert page.cursor == "1"
    assert page.omitted_ids == ("second",)
    assert page.completeness == "partial"

    continued = retrieval.retrieve(replace(request, cursor=page.cursor))

    assert [item.candidate.candidate_id for item in continued.items] == ["second"]
    assert continued.consumed_tokens == 5
    assert continued.remaining_tokens == 2
    assert continued.cursor is None
    assert continued.omitted_ids == ()
    assert continued.completeness == "complete"


def test_hybrid_retrieval_exposes_rerank_failure() -> None:
    """验证精排失败不会静默返回未精排结果"""
    candidates = [_candidate("first", "alpha evidence", (1.0, 0.0))]

    with pytest.raises(RuntimeError, match="rerank provider unavailable"):
        HybridRetrieval(FailingRerankGateway()).rank(
            "alpha",
            candidates,
            limit=1,
            query_embedding=(1.0, 0.0),
        )


def test_retrieve_exposes_embedding_failure_without_lexical_fallback() -> None:
    """验证 embedding 失败不会静默返回词项召回结果"""
    retrieval = HybridRetrieval(
        FixedRerankGateway([0]),
        embedding_gateway=FailingEmbeddingGateway((1.0, 0.0)),
        token_estimator=ControlledTokenEstimator({}),
        session_factory=build_session_factory(build_engine("sqlite://")),
        source_chunk_adapter=StaticSourceChunkAdapter(
            [_candidate("lexical", "alpha evidence", (1.0, 0.0))]
        ),
    )

    with pytest.raises(RuntimeError, match="embedding provider unavailable"):
        retrieval.retrieve(
            RetrievalRequest(
                workspace_id=UUID(int=1),
                user_id=UUID(int=2),
                conversation_id=UUID(int=3),
                query="alpha",
                source_kinds=("source_chunk",),
                context_budget=ContextBudget(
                    model_context_tokens=20,
                    policy_and_prompt="",
                    compact_conversation="",
                    tool_schema="",
                    requested_output_reserve=0,
                    safety_margin=0,
                ),
            )
        )


def test_token_estimation_does_not_enable_real_provider_for_next_app(
    monkeypatch,
) -> None:
    """验证本地 token 统计不会让后续确定性应用意外启用真实 Provider"""
    for key in (
        "DEEP_RESEARCHER_OPENAI_API_KEY",
        "DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    import dotenv

    def enable_providers_if_dotenv_is_loaded(*args, **kwargs) -> bool:
        """模拟 dotenv 被读取后把真实 Provider 配置写入进程环境"""
        del args, kwargs
        monkeypatch.setenv("DEEP_RESEARCHER_OPENAI_API_KEY", "must-not-load")
        monkeypatch.setenv("DEEP_RESEARCHER_BRAVE_SEARCH_API_KEY", "must-not-load")
        return True

    monkeypatch.setattr(dotenv, "load_dotenv", enable_providers_if_dotenv_is_loaded)

    assert LiteLLMTokenEstimator("gpt-5.6-sol").count_tokens("检索证据 test") > 0

    settings = Settings(
        database_url="sqlite://",
        object_store_root="/private/tmp/objects",
        _env_file=None,
    )
    assert settings.openai_api_key is None
    assert settings.brave_search_api_key is None


def test_retrieve_deduplicates_same_content_across_sources_before_single_rerank() -> None:
    """验证跨来源相同内容只消费一次精排与上下文空间"""
    source = RetrievalCandidate(
        candidate_id="source",
        text="alpha shared evidence",
        source_kind="source_chunk",
        content_hash="shared-hash",
        embedding=(1.0, 0.0),
    )
    record = replace(
        source,
        candidate_id="record",
        source_kind="research_record",
    )
    rerank_gateway = RecordingRerankGateway()
    retrieval = HybridRetrieval(
        rerank_gateway,
        embedding_gateway=RecordingEmbeddingGateway((1.0, 0.0)),
        token_estimator=ControlledTokenEstimator({"": 0, "alpha shared evidence": 4}),
        session_factory=build_session_factory(build_engine("sqlite://")),
        source_chunk_adapter=StaticSourceChunkAdapter([source]),
        research_record_adapter=StaticSourceChunkAdapter([record]),
    )

    page = retrieval.retrieve(
        RetrievalRequest(
            workspace_id=UUID(int=1),
            user_id=UUID(int=2),
            conversation_id=UUID(int=3),
            query="alpha",
            source_kinds=("source_chunk", "research_record"),
            context_budget=ContextBudget(
                model_context_tokens=20,
                policy_and_prompt="",
                compact_conversation="",
                tool_schema="",
                requested_output_reserve=0,
                safety_margin=0,
            ),
        )
    )

    assert rerank_gateway.documents == [["alpha shared evidence"]]
    assert [item.candidate.candidate_id for item in page.items] == ["source"]
    assert page.consumed_tokens == 4


def test_result_limit_returns_cursor_and_continuation_without_duplicates() -> None:
    """验证结果条数受限时调用方可从 cursor 继续且不会重复上一页"""
    candidates = [
        _candidate("first", "alpha first", (1.0, 0.0)),
        _candidate("second", "alpha second", (0.9, 0.1)),
    ]
    retrieval = HybridRetrieval(
        FixedRerankGateway([0, 1]),
        embedding_gateway=RecordingEmbeddingGateway((1.0, 0.0)),
        token_estimator=ControlledTokenEstimator({"": 0, "alpha first": 2, "alpha second": 2}),
        session_factory=build_session_factory(build_engine("sqlite://")),
        source_chunk_adapter=StaticSourceChunkAdapter(candidates),
    )
    request = RetrievalRequest(
        workspace_id=UUID(int=1),
        user_id=UUID(int=2),
        conversation_id=UUID(int=3),
        query="alpha",
        source_kinds=("source_chunk",),
        result_limit=1,
        context_budget=ContextBudget(
            model_context_tokens=20,
            policy_and_prompt="",
            compact_conversation="",
            tool_schema="",
            requested_output_reserve=0,
            safety_margin=0,
        ),
    )

    first_page = retrieval.retrieve(request)
    second_page = retrieval.retrieve(replace(request, cursor=first_page.cursor))

    assert [item.candidate.candidate_id for item in first_page.items] == ["first"]
    assert first_page.cursor == "1"
    assert first_page.omitted_ids == ("second",)
    assert first_page.completeness == "partial"
    assert [item.candidate.candidate_id for item in second_page.items] == ["second"]
    assert second_page.cursor is None
    assert second_page.omitted_ids == ()
    assert second_page.completeness == "complete"


def test_oversized_item_is_omitted_without_blocking_smaller_evidence() -> None:
    """验证单项超过整页容量时不会让 cursor 永久停在空页"""
    candidates = [
        _candidate("oversized", "alpha oversized", (1.0, 0.0)),
        _candidate("usable", "alpha usable", (0.9, 0.1)),
    ]
    retrieval = HybridRetrieval(
        FixedRerankGateway([0, 1]),
        embedding_gateway=RecordingEmbeddingGateway((1.0, 0.0)),
        token_estimator=ControlledTokenEstimator({"": 0, "alpha oversized": 21, "alpha usable": 4}),
        session_factory=build_session_factory(build_engine("sqlite://")),
        source_chunk_adapter=StaticSourceChunkAdapter(candidates),
    )

    page = retrieval.retrieve(
        RetrievalRequest(
            workspace_id=UUID(int=1),
            user_id=UUID(int=2),
            conversation_id=UUID(int=3),
            query="alpha",
            source_kinds=("source_chunk",),
            context_budget=ContextBudget(
                model_context_tokens=20,
                policy_and_prompt="",
                compact_conversation="",
                tool_schema="",
                requested_output_reserve=0,
                safety_margin=0,
            ),
        )
    )

    assert [item.candidate.candidate_id for item in page.items] == ["usable"]
    assert page.omitted_ids == ("oversized",)
    assert page.cursor is None
    assert page.completeness == "partial"


def test_research_record_recall_filters_workspace_index_and_lifecycle(tmp_path) -> None:
    """验证研究记录召回先执行空间、索引和生命周期过滤"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    )
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "retrieval-records@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = UUID(
            client.post(
                "/api/v1/workspaces", headers=headers, json={"name": "目标空间"}
            ).json()["id"]
        )
        other_workspace_id = UUID(
            client.post(
                "/api/v1/workspaces", headers=headers, json={"name": "隔离空间"}
            ).json()["id"]
        )
        conversation_id = UUID(
            client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "研究记录召回"},
            ).json()["id"]
        )
        queued_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-lifecycle-run"},
            json={"content": "结论是什么？"},
        ).json()
        now = datetime.now(UTC)
        records = [
            (workspace_id, "verified", "ready", None, "可复用结论 VERIFIED-1"),
            (workspace_id, "disputed", "ready", None, "冲突结论 DISPUTED-2"),
            (workspace_id, "verified", "pending", None, "未索引结论 PENDING-3"),
            (workspace_id, "superseded", "ready", None, "旧版结论 OLD-4"),
            (workspace_id, "verified", "ready", now, "已删除结论 DELETED-5"),
            (other_workspace_id, "verified", "ready", None, "跨空间结论 OTHER-6"),
        ]
        with client.app.state.session_factory.begin() as session:
            attachment = Attachment(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                filename="record-source.txt",
                storage_key="record-lifecycle/source",
                mime_type="text/plain",
                size_bytes=4,
                sha256="record-source-attachment",
                status="ready",
            )
            session.add(attachment)
            session.flush()
            chunk = SourceChunk(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                attachment_id=attachment.id,
                ordinal=0,
                text="记录证据",
                start_offset=0,
                end_offset=4,
                content_hash="record-source-hash",
                embedding=[1.0, 1.0],
                embedding_model="test-record-embedding-v1",
                embedding_dimensions=2,
                embedding_status="ready",
                indexed_at=now,
            )
            session.add(chunk)
            session.flush()
            span = EvidenceSpan(
                workspace_id=workspace_id,
                run_id=UUID(queued_run["run_id"]),
                source_chunk_id=chunk.id,
                start_offset=0,
                end_offset=4,
                content_hash=chunk.content_hash,
            )
            session.add(span)
            session.flush()
            for index, (scope_id, status, embedding_status, deleted_at, text) in enumerate(
                records, start=1
            ):
                session.add(
                    ResearchRecord(
                        workspace_id=scope_id,
                        record_key=f"record-{index}",
                        claim_text=text,
                        status=status,
                        content_hash=f"hash-{index}",
                        evidence_refs=[
                            {
                                "evidence_span_id": str(span.id),
                                "source_hash": chunk.content_hash,
                            }
                        ],
                        embedding=[1.0, float(index)],
                        embedding_model="test-record-embedding-v1",
                        embedding_dimensions=2,
                        embedding_status=embedding_status,
                        indexed_at=now,
                        deleted_at=deleted_at,
                    )
                )

        retrieval = HybridRetrieval(
            FixedRerankGateway([]),
            research_record_adapter=retrieval_module.PostgresResearchRecordRetrievalAdapter(),
        )
        with client.app.state.session_factory() as session:
            candidates = retrieval.recall_research_records(
                session,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                query="结论",
                query_embedding=(1.0, 1.0),
                limit=10,
            )

    assert {candidate.text for candidate in candidates} == {
        "可复用结论 VERIFIED-1",
        "冲突结论 DISPUTED-2",
    }
    assert {candidate.source_kind for candidate in candidates} == {"research_record"}


def test_hidden_research_record_cannot_starve_visible_candidate_before_rerank(
    tmp_path,
) -> None:
    """验证其他会话私有证据衍生的记录不会占用可见候选限额"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    )
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "record-visibility@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = UUID(
            client.post("/api/v1/workspaces", headers=headers, json={"name": "记录可见性"}).json()[
                "id"
            ]
        )
        current_conversation_id = UUID(
            client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "当前会话"},
            ).json()["id"]
        )
        other_conversation_id = UUID(
            client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "其他会话"},
            ).json()["id"]
        )
        queued_run = client.post(
            f"/api/v1/conversations/{current_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-visibility-run"},
            json={"content": "alpha 证据是什么？"},
        ).json()
        run_id = UUID(queued_run["run_id"])
        now = datetime.now(UTC)
        with client.app.state.session_factory.begin() as session:
            records: list[ResearchRecord] = []
            for index, (conversation_id, marker) in enumerate(
                [
                    (other_conversation_id, "隐藏记录 HIDDEN-1"),
                    (current_conversation_id, "可见记录 VISIBLE-2"),
                ]
            ):
                attachment = Attachment(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    filename=f"record-{index}.txt",
                    storage_key=f"record-visibility/{index}",
                    mime_type="text/plain",
                    size_bytes=len(marker),
                    sha256=f"attachment-{index}",
                    status="ready",
                )
                session.add(attachment)
                session.flush()
                chunk = SourceChunk(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    attachment_id=attachment.id,
                    ordinal=0,
                    text=marker,
                    start_offset=0,
                    end_offset=len(marker),
                    content_hash=f"source-{index}",
                    embedding=[1.0, 0.0],
                    embedding_model="test-embedding",
                    embedding_dimensions=2,
                    embedding_status="ready",
                    indexed_at=now,
                )
                session.add(chunk)
                session.flush()
                span = EvidenceSpan(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    source_chunk_id=chunk.id,
                    start_offset=0,
                    end_offset=len(marker),
                    content_hash=chunk.content_hash,
                )
                session.add(span)
                session.flush()
                records.append(
                    ResearchRecord(
                        workspace_id=workspace_id,
                        record_key=f"record-{index}",
                        claim_text=f"alpha {marker}",
                        status="verified",
                        content_hash=f"record-hash-{index}",
                        evidence_refs=[
                            {
                                "evidence_span_id": str(span.id),
                                "source_hash": chunk.content_hash,
                            }
                        ],
                        embedding=[1.0, 0.0],
                        embedding_model="test-embedding",
                        embedding_dimensions=2,
                        embedding_status="ready",
                        indexed_at=now,
                        created_at=now + timedelta(seconds=index),
                    )
                )
            session.add_all(records)

        rerank_gateway = RecordingRerankGateway()
        retrieval = HybridRetrieval(
            rerank_gateway,
            embedding_gateway=RecordingEmbeddingGateway((1.0, 0.0)),
            token_estimator=ControlledTokenEstimator(
                {
                    "policy": 1,
                    "conversation": 1,
                    "tools": 1,
                    "alpha 可见记录 VISIBLE-2": 4,
                }
            ),
            session_factory=client.app.state.session_factory,
            research_record_adapter=retrieval_module.PostgresResearchRecordRetrievalAdapter(),
        )
        page = retrieval.retrieve(
            RetrievalRequest(
                workspace_id=workspace_id,
                user_id=UUID(int=2),
                conversation_id=current_conversation_id,
                query="alpha",
                source_kinds=("research_record",),
                candidate_limit=1,
                result_limit=1,
                context_budget=ContextBudget(
                    model_context_tokens=20,
                    policy_and_prompt="policy",
                    compact_conversation="conversation",
                    tool_schema="tools",
                    requested_output_reserve=2,
                    safety_margin=2,
                ),
            )
        )

    assert rerank_gateway.documents == [["alpha 可见记录 VISIBLE-2"]]
    assert [item.candidate.text for item in page.items] == ["alpha 可见记录 VISIBLE-2"]


def test_conversation_segment_recall_excludes_conversation_private_content(tmp_path) -> None:
    """验证跨会话召回只返回明确标记为空间可见的历史分段"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    )
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "segment-scope@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = UUID(
            client.post(
                "/api/v1/workspaces", headers=headers, json={"name": "分段隔离"}
            ).json()["id"]
        )
        source_conversation_id = UUID(
            client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "历史会话"},
            ).json()["id"]
        )
        query_conversation_id = UUID(
            client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "当前会话"},
            ).json()["id"]
        )
        now = datetime.now(UTC)
        with client.app.state.session_factory.begin() as session:
            session.add_all(
                [
                    ConversationSegment(
                        workspace_id=workspace_id,
                        conversation_id=source_conversation_id,
                        ordinal=1,
                        text="可共享历史线索 SHARED-11",
                        content_hash="shared-segment",
                        visibility_scope="workspace",
                        embedding=[1.0, 1.0],
                        embedding_model="test-segment-v1",
                        embedding_dimensions=2,
                        embedding_status="ready",
                        indexed_at=now,
                    ),
                    ConversationSegment(
                        workspace_id=workspace_id,
                        conversation_id=source_conversation_id,
                        ordinal=2,
                        text="私有附件线索 PRIVATE-22",
                        content_hash="private-segment",
                        visibility_scope="conversation",
                        embedding=[1.0, 2.0],
                        embedding_model="test-segment-v1",
                        embedding_dimensions=2,
                        embedding_status="ready",
                        indexed_at=now,
                    ),
                ]
            )

        retrieval = HybridRetrieval(
            FixedRerankGateway([]),
            conversation_segment_adapter=(
                retrieval_module.PostgresConversationSegmentRetrievalAdapter()
            ),
        )
        with client.app.state.session_factory() as session:
            candidates = retrieval.recall_conversation_segments(
                session,
                workspace_id=workspace_id,
                conversation_id=query_conversation_id,
                query="线索",
                query_embedding=(1.0, 1.0),
                limit=10,
            )

    assert {candidate.text for candidate in candidates} == {"可共享历史线索 SHARED-11"}
