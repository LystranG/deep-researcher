from typing import TypedDict

from deep_researcher.agents.researcher import ResearchFinding
from deep_researcher.agents.verifier import VerificationResult
from deep_researcher.model_gateway import AnswerContext, ModelGateway
from deep_researcher.research_context import FrozenResearchContext, citable_sources
from deep_researcher.run_control import CancellationToken


class WriterOutput(TypedDict):
    """Writer 返回的待校验回答和原始增量"""

    draft_answer: str
    draft_deltas: list[str]
    usage: dict[str, int | float] | None


async def write(
    context: FrozenResearchContext,
    findings: list[ResearchFinding],
    verification: VerificationResult,
    model_gateway: ModelGateway,
    cancellation_token: CancellationToken | None = None,
) -> WriterOutput:
    """通过受控模型 Adapter 生成待 Citation 校验的回答"""
    sources = citable_sources(context)
    source = sources[0] if sources else None
    memory = context["memory"]
    answer_context = AnswerContext(
        question=context["question"],
        evidence=source.get("text") if source is not None else None,
        correction=context["correction"],
        memory=memory["content"] if memory is not None else None,
        skills=tuple(context["skills"]),
        cancellation_token=cancellation_token,
        evidences=tuple(source["text"] for source in sources if "text" in source),
        conversation_leads=tuple(context["conversation_leads"]),
        source_manifests=tuple(
            source["manifest"]
            for source in context["sources"]
            if "manifest" in source
        ),
        source_windows=tuple(
            source["source_window"]
            for source in sources
            if "source_window" in source
        ),
    )
    if hasattr(model_gateway, "astream_answer"):
        deltas = [delta async for delta in model_gateway.astream_answer(answer_context)]
    else:
        deltas = list(model_gateway.stream_answer(answer_context))
    if verification["status"] == "insufficient":
        deltas.insert(0, f"证据不足：{verification['summary']}。")
    usage = getattr(model_gateway, "last_usage", lambda: None)()
    return {"draft_answer": "".join(deltas), "draft_deltas": deltas, "usage": usage}
