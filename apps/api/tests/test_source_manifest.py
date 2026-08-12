from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from deep_researcher.source_manifest import build_source_manifest


class WordTokenEstimator:
    """按空白词项提供可控 token 计数"""

    def count_tokens(self, text: str) -> int:
        """返回确定性词项数量"""
        return len(text.split())


def test_rebuilding_manifest_for_same_snapshot_version_is_stable() -> None:
    """验证同一不可变 Snapshot 与 Chunk 集合重建结果一致"""
    snapshot = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        title="稳定报告",
        url="https://example.com/stable-report",
        captured_at=datetime(2026, 8, 12, 5, 0, tzinfo=UTC),
        content_hash="snapshot-hash",
        content="## 方法\nalpha beta\n\n## 结果\ngamma delta epsilon",
    )
    result_offset = snapshot.content.index("## 结果")
    chunks = [
        SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000011"),
            ordinal=1,
            text=snapshot.content[:result_offset],
            start_offset=0,
            end_offset=result_offset,
        ),
        SimpleNamespace(
            id=UUID("00000000-0000-0000-0000-000000000012"),
            ordinal=2,
            text=snapshot.content[result_offset:],
            start_offset=result_offset,
            end_offset=len(snapshot.content),
        ),
    ]

    first = build_source_manifest(
        snapshot,
        chunks,
        token_estimator=WordTokenEstimator(),
        adapter_id="jina_reader",
        adapter_version="hosted-v1",
        completeness="partial",
        warnings=("provider_warning",),
    )
    second = build_source_manifest(
        snapshot,
        list(reversed(chunks)),
        token_estimator=WordTokenEstimator(),
        adapter_id="jina_reader",
        adapter_version="hosted-v1",
        completeness="partial",
        warnings=("provider_warning",),
    )

    assert second == first
    assert first["completeness"] == "partial"
    assert first["warnings"] == ["provider_warning"]
    assert first["heading_navigation"] == [
        {
            "heading_path": ["方法"],
            "first_chunk_id": "00000000-0000-0000-0000-000000000011",
            "chunk_count": 1,
        },
        {
            "heading_path": ["结果"],
            "first_chunk_id": "00000000-0000-0000-0000-000000000012",
            "chunk_count": 1,
        },
    ]
