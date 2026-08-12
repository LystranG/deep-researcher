import hashlib
import re
from dataclasses import dataclass
from typing import TypedDict

from deep_researcher.models import SourceChunk, SourceSnapshot
from deep_researcher.retrieval import TokenEstimator


class HeadingNavigationItem(TypedDict):
    """描述 Manifest 中一个可导航标题范围"""

    heading_path: list[str]
    first_chunk_id: str
    chunk_count: int


class SourceTokenProfile(TypedDict):
    """描述来源分块的 token 概况"""

    total: int
    minimum: int
    maximum: int


class SourceManifest(TypedDict):
    """描述不可变来源的有界确定性导航投影"""

    snapshot_id: str
    title: str
    canonical_url: str
    captured_at: str
    content_hash: str
    adapter_id: str | None
    adapter_version: str | None
    completeness: str
    warnings: list[str]
    chunk_count: int
    token_profile: SourceTokenProfile
    heading_navigation: list[HeadingNavigationItem]
    omitted_heading_count: int


@dataclass(frozen=True)
class SourceChunkDraft:
    """表示写入 SourceChunk 前的确定性原文范围"""

    ordinal: int
    text: str
    start_offset: int
    end_offset: int
    content_hash: str


def split_source_content(content: str, *, max_chars: int = 4_000) -> list[SourceChunkDraft]:
    """按稳定段落边界拆分来源正文并保留精确 offset 与 hash"""
    if max_chars <= 0:
        raise ValueError("Source Chunk 最大字符数必须大于零")
    drafts: list[SourceChunkDraft] = []
    start = 0
    while start < len(content):
        end = min(start + max_chars, len(content))
        if end < len(content):
            paragraph_end = content.rfind("\n\n", start + max_chars // 2, end)
            if paragraph_end > start:
                end = paragraph_end + 2
        text = content[start:end]
        if text:
            drafts.append(
                SourceChunkDraft(
                    ordinal=len(drafts) + 1,
                    text=text,
                    start_offset=start,
                    end_offset=end,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                )
            )
        start = end
    return drafts


def build_source_manifest(
    snapshot: SourceSnapshot,
    chunks: list[SourceChunk],
    *,
    token_estimator: TokenEstimator,
    adapter_id: str | None,
    adapter_version: str | None,
    completeness: str,
    warnings: tuple[str, ...] = (),
    max_heading_entries: int = 32,
) -> SourceManifest:
    """从现有 Snapshot、Chunk 与获取事实生成有界 Manifest"""
    ordered_chunks = sorted(chunks, key=lambda chunk: chunk.ordinal)
    token_counts = [token_estimator.count_tokens(chunk.text) for chunk in ordered_chunks]
    navigation = _heading_navigation(snapshot.content, ordered_chunks)
    visible_navigation = navigation[:max_heading_entries]
    return {
        "snapshot_id": str(snapshot.id),
        "title": snapshot.title,
        "canonical_url": snapshot.url,
        "captured_at": snapshot.captured_at.isoformat(),
        "content_hash": snapshot.content_hash,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "completeness": completeness,
        "warnings": list(warnings),
        "chunk_count": len(ordered_chunks),
        "token_profile": {
            "total": sum(token_counts),
            "minimum": min(token_counts, default=0),
            "maximum": max(token_counts, default=0),
        },
        "heading_navigation": visible_navigation,
        "omitted_heading_count": len(navigation) - len(visible_navigation),
    }


def _heading_navigation(
    content: str, chunks: list[SourceChunk]
) -> list[HeadingNavigationItem]:
    """将 Markdown 标题范围确定性映射到稳定 Source Chunk"""
    headings: list[tuple[int, list[str]]] = []
    heading_stack: list[tuple[int, str]] = []
    for match in re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", content):
        level = len(match.group(1))
        title = match.group(2).strip()
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))
        headings.append((match.start(), [item[1] for item in heading_stack]))
    navigation: list[HeadingNavigationItem] = []
    for index, (heading_start, heading_path) in enumerate(headings):
        heading_end = headings[index + 1][0] if index + 1 < len(headings) else len(content)
        covered = [
            chunk
            for chunk in chunks
            if chunk.end_offset > heading_start and chunk.start_offset < heading_end
        ]
        if covered:
            navigation.append(
                {
                    "heading_path": heading_path,
                    "first_chunk_id": str(covered[0].id),
                    "chunk_count": len(covered),
                }
            )
    return navigation
