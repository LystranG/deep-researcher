import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Protocol, cast

from jsonschema import ValidationError, validate
from openai.types.shared.reasoning_effort import ReasoningEffort

from deep_researcher.research_context import SourceWindowContext
from deep_researcher.run_control import CancellationToken
from deep_researcher.source_manifest import SourceManifest
from deep_researcher.source_map import (
    MAP_RESPONSE_SCHEMA,
    SourceMapContext,
    SourceMapDigest,
    SourceMapSpanLocator,
    source_map_prompt,
)


class BudgetExceededError(RuntimeError):
    """模型调用达到当前任务预算时抛出"""


@dataclass(frozen=True)
class AnswerContext:
    question: str
    evidence: str | None
    correction: str | None
    memory: str | None = None
    skills: tuple[str, ...] = ()
    cancellation_token: CancellationToken | None = None
    evidences: tuple[str, ...] = ()
    conversation_leads: tuple[str, ...] = ()
    source_manifests: tuple[SourceManifest, ...] = ()
    source_windows: tuple[SourceWindowContext, ...] = ()


class ModelGateway(Protocol):
    """统一回答生成与 bounded source map 的模型 Adapter interface"""

    def astream_answer(self, context: AnswerContext) -> AsyncIterator[str]: ...

    def stream_answer(self, context: AnswerContext) -> Iterator[str]: ...

    async def acomplete_map_work(self, context: SourceMapContext) -> SourceMapDigest:
        """根据有界 Chunk group 返回派生 digest"""
        ...


class ExtractiveModelGateway:
    """无外部凭证时仍能基于实际可见资料工作的本地 Adapter。"""

    def stream_answer(self, context: AnswerContext) -> Iterator[str]:
        if context.cancellation_token is not None:
            context.cancellation_token.raise_if_cancelled()
        evidence = context.evidences[0] if context.evidences else context.evidence
        if "source-comparison" in context.skills and evidence is None:
            yield "来源比较：当前没有可定位资料，无法完成多来源对照。"
        elif evidence is not None:
            yield f"根据资料：{evidence} [1]"
        elif context.correction is not None:
            yield f"根据当前会话中的纠正：{context.correction}"
        elif context.memory is not None:
            yield f"根据长期记忆：{context.memory}"
        elif context.conversation_leads:
            yield f"根据历史会话线索：{context.conversation_leads[0]}"
        else:
            yield f"已完成对“{context.question}”的初步研究。"

    async def astream_answer(self, context: AnswerContext) -> AsyncIterator[str]:
        """异步返回确定性抽取式回答"""
        for delta in self.stream_answer(context):
            if context.cancellation_token is not None:
                context.cancellation_token.raise_if_cancelled()
            yield delta

    async def acomplete_map_work(self, context: SourceMapContext) -> SourceMapDigest:
        """从有界 Chunk group 产生只用于导航和后续核验的确定性 digest"""
        locators = [
            SourceMapSpanLocator(
                chunk_id=str(chunk.chunk_id),
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                content_hash=chunk.content_hash,
            )
            for chunk in context.chunks
        ]
        return {
            "summary": "\n\n".join(chunk.text[:240] for chunk in context.chunks),
            "candidate_claims": [],
            "candidate_span_locators": locators,
            "unresolved_questions": [],
        }


class LiteLLMModelGateway:
    """使用进程内 LiteLLM SDK 的受控模型 Adapter"""

    requires_web_research = True

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str | None = None,
        model: str,
        reasoning_effort: ReasoningEffort,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._api_base = api_base
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_retries = max_retries
        self._last_usage: dict[str, int | float] | None = None

    def last_usage(self) -> dict[str, int | float] | None:
        """返回最近一次调用的安全用量摘要"""
        return self._last_usage

    async def acomplete_map_work(self, context: SourceMapContext) -> SourceMapDigest:
        """使用 structured output 分析单个预算内 Chunk group"""
        answer_context = AnswerContext(
            question=source_map_prompt(context), evidence=None, correction=None
        )
        value = await self.acomplete_structured(answer_context, MAP_RESPONSE_SCHEMA)
        return cast(SourceMapDigest, value)

    async def acomplete_structured(
        self, context: AnswerContext, response_schema: dict[str, object]
    ) -> dict[str, object]:
        """调用 LiteLLM structured output 并解析 JSON 结果"""
        self._last_usage = None
        if context.cancellation_token is not None:
            context.cancellation_token.raise_if_cancelled()
        from litellm import acompletion

        request_args: dict[str, object] = {
            "model": self._model,
            "api_key": self._api_key,
            "messages": [{"role": "user", "content": context.question}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "structured_result", "schema": response_schema},
            },
            "timeout": 60,
        }
        if self._api_base:
            request_args["api_base"] = self._api_base
            request_args["custom_llm_provider"] = "openai"
        else:
            request_args["reasoning_effort"] = self._reasoning_effort
        response = await acompletion(**request_args)
        if context.cancellation_token is not None:
            context.cancellation_token.raise_if_cancelled()
        content = response.choices[0].message.content
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("structured output 不是对象")
        try:
            validate(value, response_schema)
        except ValidationError as exc:
            raise ValueError("structured output 校验失败") from exc
        self._last_usage = _usage_summary(response, self._model)
        return value

    def stream_answer(self, context: AnswerContext) -> Iterator[str]:
        """为同步调用方桥接 async LiteLLM Adapter"""
        async def collect() -> list[str]:
            return [delta async for delta in self.astream_answer(context)]

        yield from asyncio.run(collect())

    async def astream_answer(self, context: AnswerContext) -> AsyncIterator[str]:
        """通过 LiteLLM async SDK 流式生成回答"""
        self._last_usage = None
        if context.cancellation_token is not None:
            context.cancellation_token.raise_if_cancelled()
        evidence_block = (
            "\n\n".join(
                f"证据 [{index}]：{evidence}"
                for index, evidence in enumerate(context.evidences, start=1)
            )
            if context.evidences
            else context.evidence or "没有检索到可引用证据"
        )
        correction_block = context.correction or "没有相关会话纠正"
        memory_block = context.memory or "没有可用长期记忆"
        conversation_lead_block = (
            "\n\n".join(context.conversation_leads)
            if context.conversation_leads
            else "没有相关历史会话线索"
        )
        manifest_block = (
            "\n\n".join(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True)
                for manifest in context.source_manifests
            )
            if context.source_manifests
            else "没有需要继续浏览的长来源"
        )
        source_window_block = (
            "\n\n".join(
                json.dumps(window, ensure_ascii=False, sort_keys=True)
                for window in context.source_windows
            )
            if context.source_windows
            else "没有已读取的邻近原文窗口"
        )
        skill_block = ", ".join(context.skills) or "无"
        prompt = f"""目标：回答用户问题，并保持简体中文、简洁、可核验。

成功标准：
- 只能把下列证据和会话纠正当作已知事实
- 使用证据中的事实时，把对应的 [n] 紧跟在表述后
- 历史会话线索只能用于定位旧讨论，不能作为事实证据或引用来源
- Source Manifest 只用于选择后续读取位置，不能作为事实证据或引用来源
- 邻近原文窗口用于理解标题和解释，引用编号仍只对应可用证据
- 没有证据时明确说明证据不足，不编造来源
- 不输出思维链、工具过程或不存在的引用编号

用户问题：{context.question}
会话纠正：{correction_block}
长期记忆：{memory_block}
历史会话线索：{conversation_lead_block}
Source Manifest 导航：{manifest_block}
邻近原文窗口：{source_window_block}
已启用 Skill：{skill_block}
可用证据：
{evidence_block}
"""
        from litellm import acompletion

        yielded_delta = False
        for attempt in range(self._max_retries + 1):
            try:
                request_args: dict[str, object] = {
                    "model": self._model,
                    "api_key": self._api_key,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "timeout": 60,
                }
                if self._api_base:
                    request_args["api_base"] = self._api_base
                    request_args["custom_llm_provider"] = "openai"
                else:
                    request_args["reasoning_effort"] = self._reasoning_effort
                stream = await acompletion(**request_args)
                async for chunk in stream:
                    if context.cancellation_token is not None:
                        context.cancellation_token.raise_if_cancelled()
                    usage = _usage_summary(chunk, self._model)
                    if usage is not None:
                        self._last_usage = usage
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = getattr(chunk.choices[0].delta, "content", None)
                    if delta:
                        yielded_delta = True
                        if context.cancellation_token is not None:
                            context.cancellation_token.raise_if_cancelled()
                        yield delta
                return
            except Exception as exc:
                if yielded_delta or attempt >= self._max_retries or not _is_retryable(exc):
                    raise


def _is_retryable(error: Exception) -> bool:
    """判断错误是否属于有界重试的暂时性失败"""
    status_code = getattr(error, "status_code", None)
    return (
        isinstance(error, TimeoutError)
        or error.__class__.__name__ in {"Timeout", "APITimeoutError"}
        or status_code == 429
        or (isinstance(status_code, int)
        and 500 <= status_code < 600)
    )


def _numeric_field(value: object, *names: str) -> int | float | None:
    """读取对象或字典中的数值字段"""
    for name in names:
        candidate = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return candidate
    return None


def _usage_summary(response: object, model: str) -> dict[str, int | float] | None:
    """把 LiteLLM 响应转换为安全用量摘要"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    input_tokens = int(_numeric_field(usage, "prompt_tokens", "input_tokens") or 0)
    output_tokens = int(_numeric_field(usage, "completion_tokens", "output_tokens") or 0)
    total_tokens = int(
        _numeric_field(usage, "total_tokens") or input_tokens + output_tokens
    )
    cost = _numeric_field(usage, "cost", "cost_usd")
    if cost is None:
        cost = _numeric_field(response, "response_cost", "cost")
    hidden_params = getattr(response, "_hidden_params", None)
    if cost is None and isinstance(hidden_params, dict):
        cost = _numeric_field(hidden_params, "response_cost", "cost")
        headers = hidden_params.get("additional_headers")
        if cost is None and isinstance(headers, dict):
            cost = _numeric_field(headers, "llm_provider-x-litellm-response-cost")
    if cost is None:
        try:
            from litellm import completion_cost

            cost = float(completion_cost(completion_response=response, model=model))
        except Exception:
            cost = None
    summary: dict[str, int | float] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cost is not None:
        summary["cost_usd"] = float(cost)
    return summary


OpenAIResponsesModelGateway = LiteLLMModelGateway


def build_model_gateway(
    *, api_key: str | None, api_base: str | None, model: str, reasoning_effort: ReasoningEffort
) -> ModelGateway:
    if not api_key:
        return ExtractiveModelGateway()
    return OpenAIResponsesModelGateway(
        api_key=api_key,
        api_base=api_base,
        model=model,
        reasoning_effort=reasoning_effort,
    )
