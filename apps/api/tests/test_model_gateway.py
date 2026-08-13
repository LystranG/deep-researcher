import asyncio

import pytest
from deep_researcher.model_gateway import AnswerContext, LiteLLMModelGateway


def _context() -> AnswerContext:
    return AnswerContext(question="测试问题", evidence="测试证据", correction=None)


def test_async_litellm_retries_transient_failure_before_first_delta(monkeypatch) -> None:
    """验证首个增量前的暂时性失败最多按策略重试"""
    attempts = 0

    class RetryableError(RuntimeError):
        status_code = 503

    async def fake_acompletion(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RetryableError()

        async def stream():
            yield type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"delta": type("Delta", (), {"content": "完成"})()},
                        )()
                    ]
                },
            )()

        return stream()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    async def consume():
        return [delta async for delta in gateway.astream_answer(_context())]

    assert asyncio.run(consume()) == ["完成"]
    assert attempts == 3


def test_async_litellm_does_not_retry_after_first_delta(monkeypatch) -> None:
    """验证首个增量后失败不会透明重放请求"""
    attempts = 0

    class RetryableError(RuntimeError):
        status_code = 503

    async def fake_acompletion(**kwargs):
        nonlocal attempts
        attempts += 1

        async def stream():
            yield type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"delta": type("Delta", (), {"content": "首个"})()},
                        )()
                    ]
                },
            )()
            raise RetryableError()

        return stream()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    async def consume():
        return [delta async for delta in gateway.astream_answer(_context())]

    with pytest.raises(RetryableError):
        asyncio.run(consume())
    assert attempts == 1


def test_async_litellm_returns_structured_json(monkeypatch) -> None:
    """验证结构化模型响应只返回可解析业务对象"""

    async def fake_acompletion(**kwargs):
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message", (), {"content": '{"status":"supported"}'})()
                        },
                    )()
                ],
                "usage": type(
                    "Usage",
                    (),
                    {
                        "prompt_tokens": 4,
                        "completion_tokens": 2,
                        "total_tokens": 6,
                        "cost": 0.001,
                    },
                )(),
            },
        )()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    assert asyncio.run(
        gateway.acomplete_structured(_context(), {"type": "object"})
    ) == {"status": "supported"}
    assert gateway.last_usage() == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
        "cost_usd": 0.001,
    }


def test_custom_api_base_uses_openai_compatible_provider(monkeypatch) -> None:
    """验证自定义模型端点通过 OpenAI-compatible 路由调用"""
    captured_calls: list[dict[str, object]] = []

    async def fake_acompletion(**kwargs):
        captured_calls.append(kwargs)
        if kwargs.get("stream"):
            async def stream():
                yield type(
                    "Chunk",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {"delta": type("Delta", (), {"content": "完成"})()},
                            )()
                        ]
                    },
                )()

            return stream()
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": "{}"})()},
                    )()
                ]
            },
        )()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(
        api_key="test",
        api_base="https://models.example.com/v1",
        model="custom-model",
        reasoning_effort="low",
    )

    async def consume():
        return [delta async for delta in gateway.astream_answer(_context())]

    assert asyncio.run(consume()) == ["完成"]
    assert asyncio.run(gateway.acomplete_structured(_context(), {"type": "object"})) == {}
    assert all(call["custom_llm_provider"] == "openai" for call in captured_calls)
    assert all(call["api_base"] == "https://models.example.com/v1" for call in captured_calls)
    assert all("reasoning_effort" not in call for call in captured_calls)


def test_async_litellm_records_final_stream_usage_and_cost(monkeypatch) -> None:
    """验证最终 usage chunk 的 token 和费用摘要可供业务层读取"""
    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)

        async def stream():
            yield type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"delta": type("Delta", (), {"content": "完成"})()},
                        )()
                    ],
                    "usage": None,
                },
            )()
            yield type(
                "UsageChunk",
                (),
                {
                    "choices": [],
                    "usage": type(
                        "Usage",
                        (),
                        {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "total_tokens": 15,
                            "cost": 0.0042,
                        },
                    )(),
                },
            )()

        return stream()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    async def consume():
        return [delta async for delta in gateway.astream_answer(_context())]

    assert asyncio.run(consume()) == ["完成"]
    assert captured["stream_options"] == {"include_usage": True}
    assert gateway.last_usage() == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "cost_usd": 0.0042,
    }


def test_async_litellm_clears_usage_when_next_call_has_no_usage(monkeypatch) -> None:
    """验证下一次调用缺少 usage 时不会沿用上次计量"""
    calls = 0

    async def fake_acompletion(**kwargs):
        nonlocal calls
        calls += 1

        async def stream():
            usage = (
                type(
                    "Usage",
                    (),
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                )()
                if calls == 1
                else None
            )
            yield type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"delta": type("Delta", (), {"content": "完成"})()},
                        )()
                    ],
                    "usage": usage,
                },
            )()

        return stream()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    async def consume_twice():
        await anext(gateway.astream_answer(_context()))
        first_usage = gateway.last_usage()
        await anext(gateway.astream_answer(_context()))
        return first_usage, gateway.last_usage()

    first_usage, second_usage = asyncio.run(consume_twice())
    assert first_usage == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    assert second_usage is None


def test_async_litellm_structured_validation_failure_is_not_retried(monkeypatch) -> None:
    """验证结构化输出校验失败直接暴露，不透明重试"""
    responses = iter(['{"status":"broken"}', '{"status":"supported"}'])

    async def fake_acompletion(**kwargs):
        content = next(responses)
        return type(
            "Response",
            (),
            {
                "choices": [
                    type(
                        "Choice",
                        (),
                        {"message": type("Message", (), {"content": content})()},
                    )()
                ]
            },
        )()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    with pytest.raises(ValueError, match="structured output"):
        asyncio.run(
            gateway.acomplete_structured(
                _context(),
                {
                    "type": "object",
                    "properties": {"status": {"enum": ["supported"]}},
                    "required": ["status"],
                },
            )
        )


def test_async_litellm_retries_rate_limit_before_first_delta(monkeypatch) -> None:
    """验证限流错误在首个增量前可恢复"""
    recovered = False

    class RateLimitError(RuntimeError):
        status_code = 429

    async def fake_acompletion(**kwargs):
        nonlocal recovered
        if not recovered:
            recovered = True
            raise RateLimitError()

        async def stream():
            yield type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"delta": type("Delta", (), {"content": "恢复"})()},
                        )()
                    ]
                },
            )()

        return stream()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    async def consume():
        return [delta async for delta in gateway.astream_answer(_context())]

    assert asyncio.run(consume()) == ["恢复"]


def test_async_litellm_retries_timeout_before_first_delta(monkeypatch) -> None:
    """验证超时错误在首个增量前可恢复"""
    recovered = False

    async def fake_acompletion(**kwargs):
        nonlocal recovered
        if not recovered:
            recovered = True
            raise TimeoutError()

        async def stream():
            yield type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"delta": type("Delta", (), {"content": "恢复"})()},
                        )()
                    ]
                },
            )()

        return stream()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    gateway = LiteLLMModelGateway(api_key="test", model="test", reasoning_effort="low")

    async def consume():
        return [delta async for delta in gateway.astream_answer(_context())]

    assert asyncio.run(consume()) == ["恢复"]
