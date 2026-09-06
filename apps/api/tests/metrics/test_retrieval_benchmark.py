"""Synthetic retrieval benchmark with a versioned, fully deterministic dataset."""

from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class BenchmarkQuery:
    query_id: str
    lexical: tuple[str, ...]
    vector: tuple[str, ...]
    rerank: tuple[str, ...]
    relevant: frozenset[str]


DATASET_VERSION = "retrieval-synthetic-v1"
TOP_KS = (1, 5, 10)


def _query(
    query_id: str,
    lexical: tuple[str, ...],
    vector: tuple[str, ...],
    rerank: tuple[str, ...],
    relevant: set[str],
) -> BenchmarkQuery:
    return BenchmarkQuery(query_id, lexical, vector, rerank, frozenset(relevant))


DATASET = (
    _query(
        "q1",
        ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j"),
        ("c", "d", "a", "e", "f", "g", "h", "i", "j", "b"),
        ("a", "c", "d", "b", "e", "f", "g", "h", "i", "j"),
        {"a", "c"},
    ),
    _query(
        "q2",
        ("b", "c", "d", "e", "f", "g", "h", "i", "j", "a"),
        ("e", "f", "a", "b", "c", "d", "g", "h", "i", "j"),
        ("b", "a", "e", "c", "f", "d", "g", "h", "i", "j"),
        {"a", "b", "e"},
    ),
    _query(
        "q3",
        ("f", "g", "h", "i", "j", "a", "b", "c", "d", "e"),
        ("h", "i", "j", "c", "d", "e", "a", "b", "f", "g"),
        ("c", "e", "f", "a", "b", "d", "g", "h", "i", "j"),
        {"c", "e", "f"},
    ),
    _query(
        "q4",
        ("d", "e", "f", "g", "h", "i", "j", "a", "b", "c"),
        ("a", "d", "g", "h", "i", "j", "b", "c", "e", "f"),
        ("d", "a", "b", "e", "c", "f", "g", "h", "i", "j"),
        {"a", "d"},
    ),
    _query(
        "q5",
        ("j", "i", "h", "g", "f", "e", "d", "c", "b", "a"),
        ("f", "e", "d", "c", "b", "a", "g", "h", "i", "j"),
        ("b", "d", "f", "a", "e", "c", "g", "h", "i", "j"),
        {"b", "d", "f"},
    ),
    _query(
        "q6",
        ("g", "h", "i", "j", "a", "b", "c", "d", "e", "f"),
        ("b", "c", "d", "e", "f", "g", "h", "i", "j", "a"),
        ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j"),
        {"a", "b", "c"},
    ),
)


def _rrf(lexical: tuple[str, ...], vector: tuple[str, ...], rrf_k: int = 60) -> tuple[str, ...]:
    scores: dict[str, float] = {}
    for ranking in (lexical, vector):
        for rank, candidate_id in enumerate(ranking, start=1):
            scores[candidate_id] = scores.get(candidate_id, 0.0) + 1 / (rrf_k + rank)
    return tuple(sorted(scores, key=lambda candidate_id: (-scores[candidate_id], candidate_id)))


def _recall_at(ranking: tuple[str, ...], relevant: frozenset[str], k: int) -> float:
    return float(bool(set(ranking[:k]) & relevant))


def _mrr(ranking: tuple[str, ...], relevant: frozenset[str]) -> float:
    return next(
        (
            1 / rank
            for rank, candidate_id in enumerate(ranking, start=1)
            if candidate_id in relevant
        ),
        0.0,
    )


def evaluate() -> dict[str, dict[str, float]]:
    strategies = {
        "lexical": lambda item: item.lexical,
        "vector": lambda item: item.vector,
        "rrf": lambda item: _rrf(item.lexical, item.vector),
        "rrf+rerank": lambda item: item.rerank,
    }
    results: dict[str, dict[str, float]] = {}
    for name, ranking in strategies.items():
        rows = [(ranking(item), item.relevant) for item in DATASET]
        results[name] = {
            f"recall@{k}": sum(_recall_at(result, relevant, k) for result, relevant in rows)
            / len(rows)
            for k in TOP_KS
        }
        results[name]["mrr"] = sum(_mrr(result, relevant) for result, relevant in rows) / len(rows)
    return results


def test_dataset_is_stable_and_has_relevant_labels() -> None:
    assert DATASET_VERSION == "retrieval-synthetic-v1"
    assert len(DATASET) == 6
    assert all(item.relevant for item in DATASET)
    assert all(len(item.lexical) == len(item.vector) == len(item.rerank) == 10 for item in DATASET)


def test_synthetic_retrieval_metrics() -> None:
    results = evaluate()

    assert results["lexical"] == {
        "recall@1": pytest.approx(0.6666666666666666),
        "recall@5": pytest.approx(1.0),
        "recall@10": pytest.approx(1.0),
        "mrr": pytest.approx(0.7333333333333334),
    }
    assert results["vector"]["recall@1"] == pytest.approx(0.8333333333333334)
    assert results["vector"]["recall@5"] == pytest.approx(1.0)
    assert results["vector"]["recall@10"] == pytest.approx(1.0)
    assert results["vector"]["mrr"] == pytest.approx(0.875)
    assert results["rrf"]["recall@1"] == pytest.approx(0.8333333333333334)
    assert results["rrf"]["recall@5"] == pytest.approx(1.0)
    assert results["rrf"]["recall@10"] == pytest.approx(1.0)
    assert results["rrf"]["mrr"] == pytest.approx(0.875)
    assert results["rrf+rerank"]["recall@1"] == pytest.approx(1.0)
    assert results["rrf+rerank"]["mrr"] == pytest.approx(1.0)


if __name__ == "__main__":
    import json

    print(
        json.dumps(
            {"dataset": DATASET_VERSION, "queries": len(DATASET), "results": evaluate()},
            indent=2,
        )
    )
