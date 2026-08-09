from typing import Literal, Protocol, TypedDict

from deep_researcher.agents.planner import ResearchBrief
from deep_researcher.research_context import FrozenSource


class ResearchFinding(TypedDict):
    """Researcher 分支返回的确定性研究结果"""

    ordinal: int
    status: Literal["completed", "failed"]
    summary: str
    source_ids: list[str]
    failure_impact: str | None


def research(brief: ResearchBrief, sources: list[FrozenSource]) -> ResearchFinding:
    """在限定工具和深度内执行一个只读研究分支"""
    return {
        "ordinal": brief["ordinal"],
        "status": "completed",
        "summary": f"已准备研究分支：{brief['focus']}",
        "source_ids": [source["source_chunk_id"] for source in sources],
        "failure_impact": None,
    }


class ResearcherGateway(Protocol):
    """执行单个受限 Researcher 分支的 interface"""

    async def research(
        self, brief: ResearchBrief, sources: list[FrozenSource]
    ) -> ResearchFinding: ...


class DeterministicResearcherGateway:
    """使用本地确定性逻辑执行 Researcher 分支"""

    async def research(
        self, brief: ResearchBrief, sources: list[FrozenSource]
    ) -> ResearchFinding:
        """执行一个只读 Researcher 分支"""
        return research(brief, sources)
