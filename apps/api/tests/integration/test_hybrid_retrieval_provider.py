import hashlib
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from deep_researcher.database import build_engine, build_session_factory
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import Attachment, Conversation, SourceChunk, User, Workspace
from deep_researcher.retrieval import (
    ContextBudget,
    HybridRetrieval,
    LiteLLMEmbeddingGateway,
    LiteLLMRerankGateway,
    LiteLLMTokenEstimator,
    PostgresSourceChunkRetrievalAdapter,
    RetrievalRequest,
)
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from litellm import ServiceUnavailableError
from sqlalchemy import text

RUN_HYBRID_RETRIEVAL_LIVE = os.getenv("DEEP_RESEARCHER_RUN_HYBRID_RETRIEVAL_LIVE") == "1"
LIVE_DATABASE_URL = os.getenv("DEEP_RESEARCHER_HYBRID_RETRIEVAL_TEST_DATABASE_URL")
LIVE_EMBEDDING_KEY = os.getenv("DEEP_RESEARCHER_EMBEDDING_API_KEY")
LIVE_EMBEDDING_MODEL = os.getenv("DEEP_RESEARCHER_EMBEDDING_MODEL")
LIVE_RERANK_KEY = os.getenv("DEEP_RESEARCHER_RERANK_API_KEY")
LIVE_RERANK_MODEL = os.getenv("DEEP_RESEARCHER_RERANK_MODEL")
pytestmark = pytest.mark.skipif(
    not RUN_HYBRID_RETRIEVAL_LIVE
    or not LIVE_DATABASE_URL
    or not LIVE_EMBEDDING_KEY
    or not LIVE_EMBEDDING_MODEL
    or not LIVE_RERANK_KEY
    or not LIVE_RERANK_MODEL,
    reason="未显式配置真实 PostgreSQL、embedding 和 rerank Provider",
)


def test_live_hybrid_retrieval_persists_vectors_and_applies_acl() -> None:
    """验证真实向量、FTS、精排和 Workspace ACL 的统一检索行为"""
    assert LIVE_DATABASE_URL is not None
    assert LIVE_EMBEDDING_KEY is not None
    assert LIVE_EMBEDDING_MODEL is not None
    assert LIVE_RERANK_KEY is not None
    assert LIVE_RERANK_MODEL is not None

    engine = build_engine(LIVE_DATABASE_URL)
    with engine.connect() as connection:
        extension = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        fts_indexes = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM pg_indexes
                WHERE tablename = 'source_chunks'
                  AND indexdef ILIKE '%to_tsvector%'
                """
            )
        )
    assert extension is not None
    assert fts_indexes == 1

    embedding_gateway = LiteLLMEmbeddingGateway(
        api_key=LIVE_EMBEDDING_KEY,
        model=LIVE_EMBEDDING_MODEL,
        api_base=os.getenv("DEEP_RESEARCHER_EMBEDDING_API_BASE") or None,
    )
    rerank_gateway = LiteLLMRerankGateway(
        api_key=LIVE_RERANK_KEY,
        model=LIVE_RERANK_MODEL,
        api_base=os.getenv("DEEP_RESEARCHER_RERANK_API_BASE") or None,
    )
    visible_text = "Rerank semantic evidence for workspace alpha"
    second_visible_text = "Vector retrieval context budget for workspace alpha"
    hidden_text = "Rerank semantic evidence for workspace beta"
    vectors = embedding_gateway.embed_documents(
        [visible_text, second_visible_text, hidden_text]
    )
    assert len(vectors) == 3
    assert len(vectors[0]) > 0
    assert len({len(vector) for vector in vectors}) == 1

    session_factory = build_session_factory(engine)
    workspace_id = uuid4()
    hidden_workspace_id = uuid4()
    user_id = uuid4()
    conversation_id = uuid4()
    hidden_conversation_id = uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        session.add_all(
            [
                User(id=user_id, email=f"ticket11-{user_id}@example.com", password_hash="test"),
                Workspace(id=workspace_id, name="ticket11-visible"),
                Workspace(id=hidden_workspace_id, name="ticket11-hidden"),
            ]
        )

        session.flush()
        session.add_all(
            [
                Conversation(
                    id=conversation_id,
                    workspace_id=workspace_id,
                    title="visible",
                    created_at=now,
                    updated_at=now,
                ),
                Conversation(
                    id=hidden_conversation_id,
                    workspace_id=hidden_workspace_id,
                    title="hidden",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        session.add(
            Attachment(
                id=uuid4(),
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                filename="visible.txt",
                storage_key=f"ticket11/{workspace_id}/visible.txt",
                mime_type="text/plain",
                size_bytes=len(visible_text),
                sha256=hashlib.sha256(visible_text.encode()).hexdigest(),
                status="ready",
                created_at=now,
            )
        )
        session.flush()
        visible_attachment = session.query(Attachment).filter_by(workspace_id=workspace_id).one()
        hidden_attachment = Attachment(
            id=uuid4(),
            workspace_id=hidden_workspace_id,
            conversation_id=hidden_conversation_id,
            filename="hidden.txt",
            storage_key=f"ticket11/{hidden_workspace_id}/hidden.txt",
            mime_type="text/plain",
            size_bytes=len(hidden_text),
            sha256=hashlib.sha256(hidden_text.encode()).hexdigest(),
            status="ready",
            created_at=now,
        )
        session.add(hidden_attachment)
        session.flush()
        session.add_all(
            [
                SourceChunk(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    attachment_id=visible_attachment.id,
                    ordinal=1,
                    text=visible_text,
                    start_offset=0,
                    end_offset=len(visible_text),
                    content_hash=hashlib.sha256(visible_text.encode()).hexdigest(),
                    embedding=list(vectors[0]),
                    embedding_model=embedding_gateway.model_name,
                    embedding_dimensions=len(vectors[0]),
                    embedding_status="ready",
                    indexed_at=now,
                ),
                SourceChunk(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    attachment_id=visible_attachment.id,
                    ordinal=2,
                    text=visible_text,
                    start_offset=0,
                    end_offset=len(visible_text),
                    content_hash=hashlib.sha256(visible_text.encode()).hexdigest(),
                    embedding=list(vectors[0]),
                    embedding_model=embedding_gateway.model_name,
                    embedding_dimensions=len(vectors[0]),
                    embedding_status="ready",
                    indexed_at=now,
                ),
                SourceChunk(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    attachment_id=visible_attachment.id,
                    ordinal=3,
                    text=second_visible_text,
                    start_offset=0,
                    end_offset=len(second_visible_text),
                    content_hash=hashlib.sha256(second_visible_text.encode()).hexdigest(),
                    embedding=list(vectors[1]),
                    embedding_model=embedding_gateway.model_name,
                    embedding_dimensions=len(vectors[1]),
                    embedding_status="ready",
                    indexed_at=now,
                ),
                SourceChunk(
                    workspace_id=hidden_workspace_id,
                    conversation_id=hidden_conversation_id,
                    attachment_id=hidden_attachment.id,
                    ordinal=1,
                    text=hidden_text,
                    start_offset=0,
                    end_offset=len(hidden_text),
                    content_hash=hashlib.sha256(hidden_text.encode()).hexdigest(),
                    embedding=list(vectors[2]),
                    embedding_model=embedding_gateway.model_name,
                    embedding_dimensions=len(vectors[2]),
                    embedding_status="ready",
                    indexed_at=now,
                ),
            ]
        )

    with session_factory() as session:
        persisted = session.query(SourceChunk).filter_by(workspace_id=workspace_id).all()
        persisted_dimensions = session.execute(
            text(
                """
                SELECT DISTINCT vector_dims(embedding)
                FROM source_chunks
                WHERE workspace_id = :workspace_id AND embedding IS NOT NULL
                """
            ),
            {"workspace_id": workspace_id},
        ).scalars().all()

    assert len(persisted) == 3
    assert {chunk.embedding_status for chunk in persisted} == {"ready"}
    assert {chunk.embedding_model for chunk in persisted} == {LIVE_EMBEDDING_MODEL}
    assert {chunk.embedding_dimensions for chunk in persisted} == {len(vectors[0])}
    assert persisted_dimensions == [len(vectors[0])]

    retrieval = HybridRetrieval(
        rerank_gateway,
        embedding_gateway=embedding_gateway,
        token_estimator=LiteLLMTokenEstimator("gpt-5.6-terra"),
        session_factory=session_factory,
        source_chunk_adapter=PostgresSourceChunkRetrievalAdapter(),
    )
    request = RetrievalRequest(
        workspace_id=workspace_id,
        user_id=user_id,
        conversation_id=conversation_id,
        query="semantic evidence context budget",
        source_kinds=("source_chunk",),
        candidate_limit=10,
        result_limit=1,
        context_budget=ContextBudget(
            model_context_tokens=4_000,
            policy_and_prompt="",
            compact_conversation="",
            tool_schema="",
            requested_output_reserve=500,
            safety_margin=100,
        ),
    )
    page = retrieval.retrieve(request)
    evidence_capacity = request.context_budget.evidence_capacity(
        LiteLLMTokenEstimator("gpt-5.6-terra")
    )

    assert len(page.items) == 1
    assert all(item.candidate.candidate_id for item in page.items)
    assert all("workspace beta" not in item.candidate.text for item in page.items)
    assert page.consumed_tokens > 0
    assert page.remaining_tokens == evidence_capacity - page.consumed_tokens
    assert page.cursor == "1"
    assert len(page.omitted_ids) == 1
    assert page.completeness == "partial"

    continued = retrieval.retrieve(replace(request, cursor=page.cursor))

    assert len(continued.items) == 1
    assert continued.items[0].candidate.candidate_id == page.omitted_ids[0]
    assert continued.items[0].candidate.content_hash != page.items[0].candidate.content_hash
    assert "workspace beta" not in continued.items[0].candidate.text
    assert continued.consumed_tokens > 0
    assert continued.remaining_tokens == evidence_capacity - continued.consumed_tokens
    assert continued.cursor is None
    assert continued.omitted_ids == ()
    assert continued.completeness == "complete"
    assert rerank_gateway.last_metadata() == {
        "model_alias": LIVE_RERANK_MODEL,
        "provider": "litellm_proxy",
        "response_id": None,
        "provider_meta": None,
    }


def test_live_rerank_failure_is_exposed_without_lexical_success() -> None:
    """验证真实 Provider 拒绝无效精排模型时不会返回词项成功"""
    assert LIVE_RERANK_KEY is not None

    gateway = LiteLLMRerankGateway(
        api_key=LIVE_RERANK_KEY,
        model=f"ticket11-missing-{uuid4()}",
        api_base=os.getenv("DEEP_RESEARCHER_RERANK_API_BASE") or None,
    )

    with pytest.raises(ServiceUnavailableError):
        gateway.rerank("semantic retrieval", ["semantic evidence"], 1)

    assert gateway.last_metadata() is None


def test_live_attachment_indexing_persists_provider_vector(tmp_path) -> None:
    """验证附件公共 API 使用真实 Provider 持久化正确维度向量"""
    assert LIVE_DATABASE_URL is not None
    assert LIVE_EMBEDDING_KEY is not None
    assert LIVE_EMBEDDING_MODEL is not None

    settings = Settings(
        _env_file=None,
        database_url=LIVE_DATABASE_URL,
        object_store_root=tmp_path / "success-objects",
        openai_api_key=None,
        embedding_api_key=None,
        embedding_model=None,
        rerank_api_key=None,
        rerank_model=None,
    )
    embedding_gateway = LiteLLMEmbeddingGateway(
        api_key=LIVE_EMBEDDING_KEY,
        model=LIVE_EMBEDDING_MODEL,
        api_base=os.getenv("DEEP_RESEARCHER_EMBEDDING_API_BASE") or None,
    )
    content = "真实 Qwen embedding 应形成可检索向量"
    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        embedding_gateway=embedding_gateway,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"ticket11-success-{uuid4()}@example.com",
                "password": "correct horse battery",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "ticket11-provider-success"},
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "embedding success"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("provider-success.txt", content, "text/plain")},
        ).json()
        current = uploaded
        for _ in range(100):
            current = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if current["status"] != "processing":
                break
            time.sleep(0.02)

    engine = build_engine(LIVE_DATABASE_URL)
    with engine.connect() as connection:
        persisted = connection.execute(
            text(
                """
                SELECT embedding_model, embedding_dimensions, embedding_status,
                       vector_dims(embedding)
                FROM source_chunks
                WHERE attachment_id = :attachment_id
                """
            ),
            {"attachment_id": uploaded["id"]},
        ).one()

    assert current["status"] == "ready"
    assert persisted.embedding_model == LIVE_EMBEDDING_MODEL
    assert persisted.embedding_dimensions == 4096
    assert persisted.embedding_status == "ready"
    assert persisted.vector_dims == persisted.embedding_dimensions


def test_live_embedding_failure_is_persisted_as_indexing_failure(tmp_path) -> None:
    """验证真实 Provider 拒绝无效模型时索引失败会显式记录"""
    assert LIVE_DATABASE_URL is not None
    assert LIVE_EMBEDDING_KEY is not None

    settings = Settings(
        _env_file=None,
        database_url=LIVE_DATABASE_URL,
        object_store_root=tmp_path / "objects",
        openai_api_key=None,
        embedding_api_key=None,
        embedding_model=None,
        rerank_api_key=None,
        rerank_model=None,
    )
    failing_gateway = LiteLLMEmbeddingGateway(
        api_key=LIVE_EMBEDDING_KEY,
        model=f"openai/ticket11-missing-{uuid4()}",
        api_base=os.getenv("DEEP_RESEARCHER_EMBEDDING_API_BASE") or None,
    )
    content = "真实 Provider 失败后仍应保留附件元数据"
    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        embedding_gateway=failing_gateway,
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"ticket11-failure-{uuid4()}@example.com",
                "password": "correct horse battery",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "ticket11-provider-failure"},
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "embedding failure"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("provider-failure.txt", content, "text/plain")},
        ).json()
        current = uploaded
        for _ in range(100):
            current = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if current["status"] != "processing":
                break
            time.sleep(0.02)

    assert current["status"] == "failed"
    assert current["failure_reason"] == "文档 embedding 索引失败"
    assert current["filename"] == "provider-failure.txt"
    assert current["sha256"] == uploaded["sha256"]
