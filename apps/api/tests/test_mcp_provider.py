import asyncio

from deep_researcher.mcp_adapter import McpTaskToolAdapter, McpToolProvider
from deep_researcher.task_runtime import TaskToolCall


class FakeMcpGateway:
    def __init__(self) -> None:
        self.tools = [
            {
                "name": "notes.create",
                "description": "Create a note",
                "input_schema": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                },
                "output_schema": {
                    "type": "object",
                    "required": ["summary", "record_id"],
                    "properties": {
                        "summary": {"type": "string"},
                        "record_id": {"type": "string"},
                    },
                },
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            }
        ]

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        return {"summary": f"created {arguments['title']}", "record_id": "record-1"}


def test_mcp_provider_freezes_catalog_and_keeps_annotations_non_authoritative() -> None:
    gateway = FakeMcpGateway()
    provider = McpToolProvider(gateway)

    first = asyncio.run(provider.refresh())
    gateway.tools[0]["input_schema"]["properties"]["title"]["type"] = "integer"
    second = asyncio.run(provider.refresh())

    assert first.snapshot_id != second.snapshot_id
    assert first.definitions[0].input_schema["properties"]["title"]["type"] == "string"
    assert first.definitions[0].annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }
    assert first.definitions[0].risk == "safe"
    assert first.definitions[0].requires_approval is False


def test_mcp_task_adapter_returns_structured_result_after_output_schema_validation() -> None:
    gateway = FakeMcpGateway()
    adapter = McpTaskToolAdapter(
        gateway,
        output_schema=gateway.tools[0]["output_schema"],
    )
    result = adapter.execute(
        None,  # type: ignore[arg-type]
        TaskToolCall(tool_name="notes.create", arguments={"title": "bounded"}),
    )

    assert result.status == "succeeded"
    assert result.result_reference.startswith("mcp://observation/")
    assert result.summary == "created bounded"


def test_mcp_task_adapter_classifies_output_schema_mismatch() -> None:
    class IncompleteMcpGateway(FakeMcpGateway):
        async def call_tool(self, name, arguments):
            del name, arguments
            return {"summary": "created"}

    gateway = IncompleteMcpGateway()
    gateway.tools[0]["output_schema"] = {
        "type": "object",
        "required": ["summary", "record_id"],
    }
    adapter = McpTaskToolAdapter(
        gateway,
        output_schema=gateway.tools[0]["output_schema"],
    )

    result = adapter.execute(
        None,  # type: ignore[arg-type]
        TaskToolCall(tool_name="notes.create", arguments={"title": "bounded"}),
    )

    assert result.status == "failed"
    assert result.failure_ref == "mcp_result_schema"
    assert result.error_category == "mcp_result_schema"
