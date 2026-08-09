from collections.abc import Sequence
from urllib.parse import urlsplit

from httpx import HTTPError
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

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
                            return {"summary": summary.strip()[:2000]}
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
