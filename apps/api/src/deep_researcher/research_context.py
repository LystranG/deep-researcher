from typing import NotRequired, TypedDict

from deep_researcher.source_manifest import SourceManifest


class FrozenSource(TypedDict):
    """Run 启动时冻结的来源引用、Manifest 或可引用片段"""

    source_chunk_id: str
    source_snapshot_id: NotRequired[str]
    manifest: NotRequired[SourceManifest]
    text: NotRequired[str]
    start_offset: NotRequired[int]
    end_offset: NotRequired[int]
    content_hash: NotRequired[str]
    source_window: NotRequired["SourceWindowContext"]


class SourceWindowContext(TypedDict):
    """描述模型可见但不直接成为 Citation 的邻近原文窗口"""

    snapshot_id: str
    selected_chunk_id: str
    chunk_ids: list[str]
    heading_path: list[str]
    text: str
    start_offset: int
    end_offset: int
    snapshot_hash: str
    selected_chunk_hash: str


class FrozenMemory(TypedDict):
    """Run 启动时冻结的有效长期记忆"""

    memory_id: str
    scope: str
    content: str


class FrozenResearchContext(TypedDict):
    """Graph 和最终固化共享的不可变业务上下文"""

    question: str
    sources: list[FrozenSource]
    correction: str | None
    memory: FrozenMemory | None
    conversation_leads: list[str]
    skills: list[str]


def freeze_research_context(
    *,
    question: str,
    sources: list[FrozenSource],
    correction: str | None,
    memory: FrozenMemory | None,
    conversation_leads: list[str],
    skills: list[str],
) -> FrozenResearchContext:
    """复制当前可见能力数据形成单次 Run 的冻结快照"""
    frozen_sources: list[FrozenSource] = [
        FrozenSource(**source)
        for source in sources
    ]
    frozen_memory: FrozenMemory | None = None
    if memory is not None:
        frozen_memory = {
            "memory_id": memory["memory_id"],
            "scope": memory["scope"],
            "content": memory["content"],
        }
    return {
        "question": question,
        "sources": frozen_sources,
        "correction": correction,
        "memory": frozen_memory,
        "conversation_leads": list(conversation_leads),
        "skills": list(skills),
    }


def citable_sources(context: FrozenResearchContext) -> list[FrozenSource]:
    """返回包含精确原文范围与 hash 的可引用来源"""
    return [
        source
        for source in context["sources"]
        if all(
            key in source
            for key in ("text", "start_offset", "end_offset", "content_hash")
        )
    ]
