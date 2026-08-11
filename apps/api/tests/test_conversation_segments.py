import time
from collections.abc import Sequence
from uuid import UUID

from deep_researcher.app import create_app
from deep_researcher.document_processor import ConversationSegmentProcessor
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import ConversationSegment
from deep_researcher.settings import Settings
from deep_researcher.web_search import DisabledWebSearchGateway
from fastapi.testclient import TestClient
from sqlalchemy import select


class DeterministicEmbeddingGateway:
    """为会话分段测试生成稳定向量"""

    model_name = "segment-test-v1"

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """按文本顺序返回固定维度向量"""
        return [(1.0, float(index + 1)) for index, _ in enumerate(texts)]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """返回固定查询向量"""
        del text
        return (1.0, 1.0)


class FailingEmbeddingGateway:
    """模拟会话分段 embedding Provider 失败"""

    model_name = "segment-test-failing"

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """拒绝当前批次以验证失败状态"""
        del texts
        raise ValueError("Provider 拒绝会话分段批次")

    def embed_query(self, text: str) -> tuple[float, ...]:
        """返回不会在本测试中使用的查询向量"""
        del text
        return (1.0, 1.0)


def create_conversation(client: TestClient, email: str) -> tuple[dict[str, str], UUID]:
    """创建测试用户、Workspace 和会话并返回鉴权信息"""
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    workspace_id = client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "会话分段"}
    ).json()["id"]
    conversation_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": "确定性窗口"},
    ).json()["id"]
    return headers, UUID(conversation_id)


def test_rebuilding_messages_produces_same_bounded_ready_segments(tmp_path) -> None:
    """验证相同消息序列始终形成相同的 2000 字符 ready 窗口"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    embedding_gateway = DeterministicEmbeddingGateway()
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        embedding_gateway=embedding_gateway,
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers, conversation_id = create_conversation(client, "segments@example.com")
        for index, content in enumerate(("A" * 1400, "B" * 1400), start=1):
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={**headers, "Idempotency-Key": f"segment-ready-{index}"},
                json={"content": content},
            )
        first_snapshot: list[tuple[int, str, str, str]] = []
        for _ in range(100):
            with app.state.session_factory() as session:
                first_snapshot = [
                    (segment.ordinal, segment.text, segment.content_hash, segment.embedding_status)
                    for segment in session.scalars(
                        select(ConversationSegment)
                        .where(ConversationSegment.conversation_id == conversation_id)
                        .order_by(ConversationSegment.ordinal)
                    ).all()
                ]
            if len(first_snapshot) == 2 and all(
                item[3] == "ready" for item in first_snapshot
            ):
                break
            time.sleep(0.01)

        ConversationSegmentProcessor(
            app.state.session_factory,
            embedding_gateway=embedding_gateway,
        ).process(conversation_id)
        with app.state.session_factory() as session:
            second_snapshot = [
                (segment.ordinal, segment.text, segment.content_hash, segment.embedding_status)
                for segment in session.scalars(
                    select(ConversationSegment)
                    .where(ConversationSegment.conversation_id == conversation_id)
                    .order_by(ConversationSegment.ordinal)
                ).all()
            ]

    assert len(first_snapshot) == 2
    assert all(len(item[1]) <= 2000 for item in first_snapshot)
    assert all(item[3] == "ready" for item in first_snapshot)
    assert second_snapshot == first_snapshot


def test_embedding_failure_marks_conversation_segment_failed(tmp_path) -> None:
    """验证 Provider 失败会留下可重试的 failed 会话分段"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        embedding_gateway=FailingEmbeddingGateway(),
        embedded_worker=False,
    )

    with TestClient(app) as client:
        headers, conversation_id = create_conversation(client, "segment-failure@example.com")
        client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "segment-failed"},
            json={"content": "需要保留的历史消息"},
        )
        failed_snapshot: tuple[str, str | None, str] | None = None
        for _ in range(100):
            with app.state.session_factory() as session:
                segment = session.scalar(
                    select(ConversationSegment).where(
                        ConversationSegment.conversation_id == conversation_id
                    )
                )
                if segment is not None:
                    failed_snapshot = (
                        segment.embedding_status,
                        segment.embedding_error,
                        segment.text,
                    )
            if failed_snapshot is not None and failed_snapshot[0] == "failed":
                break
            time.sleep(0.01)

    assert failed_snapshot == (
        "failed",
        "Provider 拒绝会话分段批次",
        "user: 需要保留的历史消息\nassistant: ",
    )


def test_assistant_final_message_is_reindexed_into_conversation_segment(tmp_path) -> None:
    """验证 assistant 最终消息固化后会重建当前会话分段索引"""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    embedding_gateway = DeterministicEmbeddingGateway()
    app = create_app(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        embedding_gateway=embedding_gateway,
        embedded_worker=True,
    )

    with TestClient(app) as client:
        headers, conversation_id = create_conversation(client, "assistant-segment@example.com")
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "assistant-segment"},
            json={"content": "完成最终回答"},
        ).json()
        client.get(f"/api/v1/runs/{created['run_id']}/events", headers=headers)
        assistant_segment_text = None
        for _ in range(100):
            with app.state.session_factory() as session:
                segment = session.scalar(
                    select(ConversationSegment).where(
                        ConversationSegment.conversation_id == conversation_id,
                        ConversationSegment.text.contains("assistant:"),
                    )
                )
                assistant_segment_text = segment.text if segment is not None else None
            if assistant_segment_text is not None:
                break
            time.sleep(0.01)

    assert assistant_segment_text is not None
    assert "assistant: 已完成对“完成最终回答”的初步研究。" in assistant_segment_text
