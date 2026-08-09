from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

mcp_server = MCPServer("deep-researcher-e2e-mcp")


@mcp_server.tool(name="research_notes.create", structured_output=True)
def create_research_note(title: str, idempotency_key: str) -> dict[str, str]:
    """返回 Playwright 验收使用的幂等研究记录摘要"""
    return {"summary": f"已创建研究记录：{title}"}


async def healthz(_request: Request) -> JSONResponse:
    """提供本机测试 MCP Server 就绪探针"""
    return JSONResponse({"status": "ok"})


app = mcp_server.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    host="127.0.0.1",
)
app.routes.insert(0, Route("/healthz", healthz))
