from typing import TypedDict


class FrozenSource(TypedDict):
    """Run 启动时冻结的可引用来源片段"""

    source_chunk_id: str
    text: str
    start_offset: int
    end_offset: int
    content_hash: str


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
    skills: list[str]


def freeze_research_context(
    *,
    question: str,
    sources: list[FrozenSource],
    correction: str | None,
    memory: FrozenMemory | None,
    skills: list[str],
) -> FrozenResearchContext:
    """复制当前可见能力数据形成单次 Run 的冻结快照"""
    frozen_sources: list[FrozenSource] = [
        {
            "source_chunk_id": source["source_chunk_id"],
            "text": source["text"],
            "start_offset": source["start_offset"],
            "end_offset": source["end_offset"],
            "content_hash": source["content_hash"],
        }
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
        "skills": list(skills),
    }
