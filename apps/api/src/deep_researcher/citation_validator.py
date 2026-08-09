import re
from dataclasses import dataclass
from typing import TypedDict


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


def validate_answer(answer: str, evidence: str | None) -> CitationValidationResult:
    """拒绝冻结 SourceSet 之外的引用并执行抽取式降级"""
    labels = {int(label) for label in re.findall(r"\[(\d+)]", answer)}
    allowed_labels = {1} if evidence is not None else set()
    if labels - allowed_labels or (evidence is not None and 1 not in labels):
        answer = f"根据资料：{evidence} [1]" if evidence is not None else "当前没有可核验资料。"
        labels = {1} if evidence is not None else set()
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
