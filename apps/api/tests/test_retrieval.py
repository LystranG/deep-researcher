import pytest
from deep_researcher.retrieval import HybridRetrieval, RetrievalCandidate


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
