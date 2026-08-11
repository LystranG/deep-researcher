from datetime import UTC, datetime
from uuid import UUID

import deep_researcher.retrieval as retrieval_module
import pytest
from deep_researcher.app import create_app
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import ResearchRecord
from deep_researcher.retrieval import HybridRetrieval, RetrievalCandidate
from deep_researcher.settings import Settings
from deep_researcher.web_search import DisabledWebSearchGateway
from fastapi.testclient import TestClient


class FixedRerankGateway:
    """返回固定顺序的精排结果"""

    def __init__(self, order: list[int]) -> None:
        self.order = order

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        del query, documents
        return [(index, float(top_n - position)) for position, index in enumerate(self.order)]


class FailingRerankGateway:
    """模拟精排 Provider 失败"""

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        del query, documents, top_n
        raise RuntimeError("rerank provider unavailable")


def _candidate(candidate_id: str, text: str, embedding: tuple[float, ...]) -> RetrievalCandidate:
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


def test_hybrid_retrieval_stops_before_exceeding_token_budget() -> None:
    """验证候选拼接不会超过调用方提供的上下文预算"""
    candidates = [
        _candidate("first", "abcd", (1.0, 0.0)),
        _candidate("second", "efgh", (0.9, 0.1)),
    ]

    result = HybridRetrieval(FixedRerankGateway([0, 1])).rank(
        "alpha",
        candidates,
        limit=2,
        query_embedding=(1.0, 0.0),
        token_budget=5,
    )

    assert [item.candidate.candidate_id for item in result] == ["first"]


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
                        evidence_refs=[],
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
                query="结论",
                query_embedding=(1.0, 1.0),
                limit=10,
            )

    assert {candidate.text for candidate in candidates} == {
        "可复用结论 VERIFIED-1",
        "冲突结论 DISPUTED-2",
    }
    assert {candidate.source_kind for candidate in candidates} == {"research_record"}
