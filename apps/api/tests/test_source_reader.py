import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from deep_researcher.app import create_app
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import SourceChunk, SourceSnapshot
from deep_researcher.retrieval import ContextBudget
from deep_researcher.settings import Settings
from deep_researcher.source_reader import (
    SourceDescriptorRequest,
    SourceLedgerReader,
    SourceWindowRequest,
)
from deep_researcher.web_search import DisabledWebSearchGateway
from fastapi.testclient import TestClient


class ControlledTokenEstimator:
    """为来源分页提供可控 token 统计"""

    def count_tokens(self, text: str) -> int:
        """把每个非空白词项视为一个 token"""
        return len(text.split())


def create_queued_run(client: TestClient) -> tuple[dict[str, str], UUID, UUID]:
    """创建用于校验来源访问范围的 queued Research Run"""
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": "source-reader@example.com", "password": "correct horse battery"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    workspace_id = UUID(
        client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "来源窗口"}
        ).json()["id"]
    )
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "尾部事实"},
    ).json()["id"]
    run_id = UUID(
        client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "source-reader-run"},
            json={"content": "查找 TAIL-FACT"},
        ).json()["run_id"]
    )
    return headers, workspace_id, run_id


def test_descriptor_search_finds_page_tail_and_reports_budget_continuation(tmp_path) -> None:
    """验证尾部 Chunk 优先进入有预算边界的 descriptor page"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    )
    with TestClient(app) as client:
        _, workspace_id, run_id = create_queued_run(client)
        content_parts = [
            "## 概览\n普通背景材料",
            "## 方法\n实验设置与限制",
            "## 附录\nTAIL-FACT 页面尾部的关键结论",
        ]
        content = "\n\n".join(content_parts)
        with app.state.session_factory.begin() as session:
            snapshot = SourceSnapshot(
                workspace_id=workspace_id,
                run_id=run_id,
                source_type="web",
                content_kind="web_page",
                ordinal=1,
                title="长篇报告",
                url="https://example.com/long-report",
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                captured_at=datetime(2026, 8, 12, 5, 0, tzinfo=UTC),
            )
            session.add(snapshot)
            session.flush()
            offset = 0
            chunks = []
            for ordinal, text in enumerate(content_parts, start=1):
                start = content.index(text, offset)
                end = start + len(text)
                chunk = SourceChunk(
                    workspace_id=workspace_id,
                    source_snapshot_id=snapshot.id,
                    ordinal=ordinal,
                    text=text,
                    start_offset=start,
                    end_offset=end,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                )
                session.add(chunk)
                chunks.append(chunk)
                offset = end
            session.flush()
            snapshot_id = snapshot.id
            tail_chunk_id = chunks[-1].id

        reader = SourceLedgerReader(app.state.session_factory, ControlledTokenEstimator())
        page = reader.search_source_chunks(
            SourceDescriptorRequest(
                run_id=run_id,
                snapshot_ids=(snapshot_id,),
                query="TAIL-FACT",
                context_budget=ContextBudget(
                    model_context_tokens=10,
                    policy_and_prompt="policy",
                    compact_conversation="conversation",
                    tool_schema="tools",
                    requested_output_reserve=2,
                    safety_margin=1,
                ),
                result_limit=1,
            )
        )

    assert page.items[0].chunk_id == tail_chunk_id
    assert page.items[0].heading_path == ("附录",)
    assert page.items[0].preview == "TAIL-FACT 页面尾部的关键结论"
    assert page.cursor == "1"
    assert page.omitted_chunk_ids
    assert page.remaining_token_estimate >= 0
    assert page.completeness == "partial"


def test_chunk_window_keeps_heading_neighbor_and_exact_snapshot_locator(tmp_path) -> None:
    """验证稳定 Chunk ID 读取保留邻居并返回不可变来源定位信息"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    )
    with TestClient(app) as client:
        _, workspace_id, run_id = create_queued_run(client)
        content_parts = [
            "## 方法\n前置实验解释",
            "## 结果\n关键事实 EVIDENCE-42",
            "## 限制\n该事实只适用于样本 A",
        ]
        content = "\n\n".join(content_parts)
        with app.state.session_factory.begin() as session:
            snapshot = SourceSnapshot(
                workspace_id=workspace_id,
                run_id=run_id,
                source_type="web",
                content_kind="web_page",
                ordinal=1,
                title="精确定位报告",
                url="https://example.com/evidence-report",
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
            session.add(snapshot)
            session.flush()
            chunks = []
            offset = 0
            for ordinal, text in enumerate(content_parts, start=1):
                start = content.index(text, offset)
                end = start + len(text)
                chunk = SourceChunk(
                    workspace_id=workspace_id,
                    source_snapshot_id=snapshot.id,
                    ordinal=ordinal,
                    text=text,
                    start_offset=start,
                    end_offset=end,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                )
                session.add(chunk)
                chunks.append(chunk)
                offset = end
            session.flush()
            selected = chunks[1]
            expected_window = "\n\n".join(content_parts)

        reader = SourceLedgerReader(app.state.session_factory, ControlledTokenEstimator())
        page = reader.read_source_chunks(
            SourceWindowRequest(
                run_id=run_id,
                chunk_ids=(selected.id,),
                context_budget=ContextBudget(
                    model_context_tokens=30,
                    policy_and_prompt="policy",
                    compact_conversation="conversation",
                    tool_schema="tools",
                    requested_output_reserve=2,
                    safety_margin=1,
                ),
                neighbor_window=1,
            )
        )

    assert len(page.items) == 1
    window = page.items[0]
    assert window.selected_chunk_id == selected.id
    assert window.chunk_ids == tuple(chunk.id for chunk in chunks)
    assert window.heading_path == ("结果",)
    assert window.text == expected_window
    assert window.start_offset == 0
    assert window.end_offset == len(content)
    assert window.snapshot_hash == snapshot.content_hash
    assert window.selected_chunk_hash == selected.content_hash
    assert page.cursor is None
    assert page.omitted_chunk_ids == ()
    assert page.completeness == "complete"


def test_source_windows_stop_at_chunk_boundary_and_continue_without_duplicates(tmp_path) -> None:
    """验证原文窗口预算不足时返回 cursor 且续页不重复"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    )
    with TestClient(app) as client:
        _, workspace_id, run_id = create_queued_run(client)
        content_parts = ["## 第一节\none two", "## 第二节\nthree four"]
        content = "\n\n".join(content_parts)
        with app.state.session_factory.begin() as session:
            snapshot = SourceSnapshot(
                workspace_id=workspace_id,
                run_id=run_id,
                source_type="web",
                content_kind="web_page",
                ordinal=1,
                title="分页报告",
                url="https://example.com/paged-report",
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
            session.add(snapshot)
            session.flush()
            chunks = []
            offset = 0
            for ordinal, text in enumerate(content_parts, start=1):
                start = content.index(text, offset)
                end = start + len(text)
                chunk = SourceChunk(
                    workspace_id=workspace_id,
                    source_snapshot_id=snapshot.id,
                    ordinal=ordinal,
                    text=text,
                    start_offset=start,
                    end_offset=end,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                )
                session.add(chunk)
                chunks.append(chunk)
                offset = end
            session.flush()

        reader = SourceLedgerReader(app.state.session_factory, ControlledTokenEstimator())
        request = SourceWindowRequest(
            run_id=run_id,
            chunk_ids=tuple(chunk.id for chunk in chunks),
            context_budget=ContextBudget(
                model_context_tokens=9,
                policy_and_prompt="policy",
                compact_conversation="conversation",
                tool_schema="tools",
                requested_output_reserve=1,
                safety_margin=1,
            ),
            neighbor_window=0,
        )
        first = reader.read_source_chunks(request)
        second = reader.read_source_chunks(replace(request, cursor=first.cursor))

    assert [item.selected_chunk_id for item in first.items] == [chunks[0].id]
    assert first.items[0].text == content_parts[0]
    assert first.cursor == "1"
    assert first.omitted_chunk_ids == (chunks[1].id,)
    assert first.completeness == "partial"
    assert [item.selected_chunk_id for item in second.items] == [chunks[1].id]
    assert second.items[0].text == content_parts[1]
    assert second.cursor is None
    assert second.omitted_chunk_ids == ()
    assert second.completeness == "complete"
