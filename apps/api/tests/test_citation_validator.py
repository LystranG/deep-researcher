import pytest
from deep_researcher.citation_validator import (
    CitationValidationError,
    validate_answer,
    validate_citation_drafts,
)


def test_validate_citation_drafts_accepts_exact_marker_ranges() -> None:
    answer = "事实 [1]"
    drafts = [{"label": 1, "answer_start": 3, "answer_end": 6}]

    assert validate_citation_drafts(answer, drafts, evidence_count=1) == tuple(drafts)


@pytest.mark.parametrize(
    "draft",
    [
        {"label": 0, "answer_start": 3, "answer_end": 6},
        {"label": 2, "answer_start": 3, "answer_end": 6},
        {"label": 1, "answer_start": 0, "answer_end": 3},
        {"label": 1, "answer_start": 3, "answer_end": 5},
    ],
)
def test_validate_citation_drafts_rejects_unknown_or_invalid_ranges(draft) -> None:
    with pytest.raises(CitationValidationError):
        validate_citation_drafts("事实 [1]", [draft], evidence_count=1)


def test_validate_citation_drafts_rejects_duplicate_labels() -> None:
    drafts = [
        {"label": 1, "answer_start": 3, "answer_end": 6},
        {"label": 1, "answer_start": 3, "answer_end": 6},
    ]

    with pytest.raises(CitationValidationError):
        validate_citation_drafts("事实 [1]", drafts, evidence_count=1)


def test_validate_citation_drafts_rejects_repeated_visible_markers() -> None:
    drafts = [{"label": 1, "answer_start": 3, "answer_end": 6}]

    with pytest.raises(CitationValidationError):
        validate_citation_drafts("事实 [1]，补充 [1]", drafts, evidence_count=1)


def test_validate_answer_rejects_unknown_markers_instead_of_rewriting_the_draft() -> None:
    with pytest.raises(CitationValidationError, match="无效 Citation"):
        validate_answer("不存在的引用 [2]", ["唯一证据"])
