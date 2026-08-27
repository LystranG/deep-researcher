import re
from dataclasses import dataclass
from typing import TypedDict


class CitationValidationError(ValueError):
    """Raised when a citation draft cannot be bound to frozen evidence."""


class CitationDraft(TypedDict):
    """通过校验后可原子固化的引用草稿"""

    label: int
    answer_start: int
    answer_end: int


@dataclass(frozen=True)
class CitationValidationResult:
    """Citation Validator 返回的安全回答和引用草稿"""

    answer: str
    citations: tuple[CitationDraft, ...]


def validate_citation_drafts(
    answer: str,
    drafts: list[CitationDraft] | tuple[CitationDraft, ...],
    evidence_count: int,
) -> tuple[CitationDraft, ...]:
    """Validate citation markers before they cross the persistence boundary."""
    valid: list[CitationDraft] = []
    seen_labels: set[int] = set()
    marker_labels = [int(label) for label in re.findall(r"\[(\d+)]", answer)]
    if len(marker_labels) != len(set(marker_labels)):
        raise CitationValidationError("重复 Citation 标记未被逐一绑定")
    for draft in drafts:
        label = draft["label"]
        start = draft["answer_start"]
        end = draft["answer_end"]
        if (
            label < 1
            or label > evidence_count
            or label in seen_labels
            or start < 0
            or end <= start
            or end > len(answer)
            or answer[start:end] != f"[{label}]"
        ):
            raise CitationValidationError(f"无效 Citation：[{label}]")
        seen_labels.add(label)
        valid.append(draft)
    if set(marker_labels) != seen_labels:
        raise CitationValidationError("回答包含未绑定的 Citation 标记")
    return tuple(valid)


def validate_answer(
    answer: str, evidence: str | list[str] | None
) -> CitationValidationResult:
    """拒绝冻结 SourceSet 之外的引用，不改写 Writer 草稿。"""
    evidences = [evidence] if isinstance(evidence, str) else (evidence or [])
    citation_drafts: list[CitationDraft] = []
    for marker in re.finditer(r"\[(\d+)]", answer):
        label = int(marker.group(1))
        citation_drafts.append(
            {
                "label": label,
                "answer_start": marker.start(),
                "answer_end": marker.end(),
            }
        )
    citations = validate_citation_drafts(answer, citation_drafts, len(evidences))
    return CitationValidationResult(answer=answer, citations=citations)
