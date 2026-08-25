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
    """拒绝冻结 SourceSet 之外的引用并执行抽取式降级"""
    evidences = [evidence] if isinstance(evidence, str) else (evidence or [])
    labels = {int(label) for label in re.findall(r"\[(\d+)]", answer)}
    allowed_labels = set(range(1, len(evidences) + 1))
    if labels - allowed_labels or (evidences and not labels):
        answer = (
            f"根据资料：{evidences[0]} [1]" if evidences else "当前没有可核验资料。"
        )
        labels = {1} if evidences else set()
    citation_drafts: list[CitationDraft] = []
    for label in sorted(labels):
        citation_drafts.append(
            {
                "label": label,
                "answer_start": answer.index(f"[{label}]"),
                "answer_end": answer.index(f"[{label}]") + len(f"[{label}]"),
            }
        )
    return CitationValidationResult(answer=answer, citations=tuple(citation_drafts))
