import asyncio
from collections.abc import Sequence
from copy import deepcopy
from typing import cast
from urllib.parse import urlsplit

from httpx import HTTPError
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from deep_researcher.task_runtime import (
    TaskClaim,
    TaskObservationResult,
    TaskToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolRegistrySnapshot,
    _validate_tool_schema,
)
from deep_researcher.tool_execution import (
    McpGatewayError,
    McpProtocolError,
    McpResultValidationError,
    McpToolDescriptor,
    McpToolError,
    McpTransportError,
)


class LocalTrustedHttpMcpAdapter:
    """通过官方 MCP SDK 连接本机受信 Streamable HTTP Server"""

    def __init__(self, url: str, *, timeout_seconds: float = 30.0) -> None:
        """校验本地受信 URL 并配置单次请求超时"""
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("本地受信 MCP 只允许 http://127.0.0.1 或 http://localhost")
        self._url = url
        self._timeout_seconds = timeout_seconds

    async def list_tools(self) -> list[McpToolDescriptor]:
        """初始化 MCP Session 并读取完整分页工具目录"""
        async with streamable_http_client(self._url) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=self._timeout_seconds,
            ) as session:
                await session.initialize()
                return await self._list_all_tools(session)

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        """在同一已初始化 Session 中发现并调用受信工具"""
        try:
            async with streamable_http_client(self._url) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._timeout_seconds,
                ) as session:
                    await session.initialize()
                    tools = await self._list_all_tools(session)
                    if not any(tool["name"] == name for tool in tools):
                        raise McpProtocolError("受信 MCP Server 未公开请求的工具")
                    result = await session.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=self._timeout_seconds,
                    )
                    if result.is_error:
                        raise McpToolError(self._safe_content_summary(result.content))
                    structured = result.structured_content
                    if isinstance(structured, dict):
                        summary = structured.get("summary")
                        if isinstance(summary, str) and summary.strip():
                            return _safe_structured_result(structured)
                    raise McpResultValidationError("MCP 工具结果缺少有效 summary")
        except McpGatewayError:
            raise
        except HTTPError as exc:
            raise McpTransportError("MCP HTTP 传输失败") from exc
        except MCPError as exc:
            raise McpProtocolError("MCP 协议消息失败") from exc
        except Exception as exc:
            raise McpProtocolError("MCP 调用未按协议完成") from exc

    async def _list_all_tools(self, session: ClientSession) -> list[McpToolDescriptor]:
        """按 next_cursor 读取完整工具目录并转换为领域描述"""
        cursor: str | None = None
        descriptors: list[McpToolDescriptor] = []
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            descriptors.extend(
                McpToolDescriptor(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                    annotations=_model_dict(tool.annotations),
                )
                for tool in page.tools
            )
            cursor = page.next_cursor
            if cursor is None:
                return descriptors

    def _safe_content_summary(self, content: Sequence[object]) -> str:
        """只提取工具结果中的短文本摘要，避免持久化完整响应"""
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()[:2000]
        return "工具执行完成"


class McpToolProvider:
    """Discover a local MCP server and publish immutable registry snapshots."""

    def __init__(self, gateway: LocalTrustedHttpMcpAdapter, *, provider: str = "mcp") -> None:
        self._gateway = gateway
        self._provider = provider
        self._snapshot: ToolRegistrySnapshot | None = None

    async def refresh(self) -> ToolRegistrySnapshot:
        definitions = [
            ToolDefinition(
                name=descriptor["name"],
                description=descriptor["description"],
                input_schema=deepcopy(descriptor["input_schema"]),
                output_schema=deepcopy(descriptor.get("output_schema")),
                annotations=deepcopy(descriptor.get("annotations")),
                provider=self._provider,
                handler=McpTaskToolAdapter(
                    self._gateway,
                    output_schema=deepcopy(descriptor.get("output_schema")),
                ),
            )
            for descriptor in await self._gateway.list_tools()
        ]
        self._snapshot = ToolRegistry(definitions).snapshot()
        return self._snapshot

    async def snapshot(self) -> ToolRegistrySnapshot:
        if self._snapshot is None:
            return await self.refresh()
        return self._snapshot


class McpTaskToolAdapter:
    """Bridge MCP results into durable TaskObservation facts."""

    def __init__(
        self,
        gateway: LocalTrustedHttpMcpAdapter,
        *,
        output_schema: dict[str, object] | None = None,
    ) -> None:
        self._gateway = gateway
        self._output_schema = output_schema

    def execute(self, claim: TaskClaim, call: TaskToolCall) -> TaskObservationResult:
        del claim
        try:
            result = asyncio.run(self._gateway.call_tool(call.tool_name, call.arguments))
        except McpTransportError:
            return _mcp_failure("mcp_transport", "MCP 传输失败")
        except McpProtocolError:
            return _mcp_failure("mcp_protocol", "MCP 协议失败")
        except McpToolError:
            return _mcp_failure("mcp_tool", "MCP 工具返回失败")
        except McpResultValidationError:
            return _mcp_failure("mcp_result_schema", "MCP 结果未通过校验")
        except Exception:
            return _mcp_failure("mcp_transport", "MCP 传输失败")
        summary = cast(str, result["summary"]) if isinstance(result.get("summary"), str) else None
        normalized = _safe_structured_result(result)
        schema_error = (
            _validate_tool_schema(self._output_schema, normalized, "$")
            if self._output_schema is not None
            else None
        )
        if schema_error is not None:
            return _mcp_failure("mcp_result_schema", "MCP 结果未通过校验")
        observation_key = (
            call.logical_call_ref or call.parameters_hash or call.tool_name
        )
        return TaskObservationResult(
            result_reference=f"mcp://observation/{observation_key}",
            evidence_gain=True,
            summary=summary or "MCP 工具执行成功",
        )

    @staticmethod
    def validate_result(definition: ToolDefinition, result: dict[str, object]) -> str | None:
        """Validate the normalized result against the frozen remote contract."""
        if definition.output_schema is None:
            return None
        return _validate_tool_schema(definition.output_schema, result, "$")


def _mcp_failure(category: str, summary: str) -> TaskObservationResult:
    return TaskObservationResult(
        status="failed",
        failure_ref=category,
        error_category=category,
        summary=summary,
    )


def _model_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else None
    return None


def _safe_structured_result(value: dict[str, object]) -> dict[str, object]:
    """Keep JSON-compatible result data bounded before it reaches an Observation."""
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            result[key] = item[:2000]
        elif isinstance(item, (bool, int, float)) or item is None:
            result[key] = item
        elif isinstance(item, list):
            result[key] = [
                entry[:500] if isinstance(entry, str) else entry
                for entry in item[:100]
            ]
        elif isinstance(item, dict):
            result[key] = _safe_structured_result(item)
    return result
