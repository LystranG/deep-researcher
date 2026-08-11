import time
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.models import Memory
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient
from sqlalchemy import select


class SemanticMemoryEmbeddingGateway:
    """让不同措辞的记忆与查询获得相同测试向量"""

    model_name = "memory-semantic-v1"

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """根据语义标记生成稳定向量"""
        return [self._embedding(text) for text in texts]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """为查询生成与对应记忆相同的向量"""
        return self._embedding(text)

    @staticmethod
    def _embedding(text: str) -> tuple[float, ...]:
        """将分点偏好映射到第一维，其余文本映射到第二维"""
        return (1.0, 0.0) if "分点" in text or "bullet" in text else (0.0, 1.0)


class StableMemoryRerankGateway:
    """按融合后的候选顺序返回 Memory 精排结果"""

    def rerank(
        self, query: str, documents: Sequence[str], top_n: int
    ) -> list[tuple[int, float]]:
        """返回预算内的稳定候选下标"""
        del query
        return [(index, 1.0 - index / 100) for index in range(min(len(documents), top_n))]


class EmptyWebSearchGateway:
    """隔离 Memory 测试中的真实网络搜索"""

    def search(self, query: str, *, count: int = 5) -> list[dict[str, str]]:
        """返回空候选，让回答只由 Memory 决定"""
        del query, count
        return []


def running_deterministic_memory_client(
    settings: Settings,
) -> AbstractContextManager[TestClient]:
    """使用本地确定性 Adapter 创建 Memory 行为测试客户端"""
    return running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=EmptyWebSearchGateway(),
        embedding_gateway=SemanticMemoryEmbeddingGateway(),
        rerank_gateway=StableMemoryRerankGateway(),
    )


@pytest.mark.parametrize("scope", ["workspace", "user"])
def test_semantic_memory_is_recalled_from_another_conversation(
    tmp_path, scope: str
) -> None:
    """验证 Workspace 和 User 记忆可通过向量语义跨会话召回"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=EmptyWebSearchGateway(),
        embedding_gateway=SemanticMemoryEmbeddingGateway(),
        rerank_gateway=StableMemoryRerankGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"semantic-{scope}@example.com",
                "password": "correct horse battery",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "语义记忆"}
        ).json()["id"]
        memory = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "输出偏好是分点列出",
                "scope": scope,
                "category": "format",
                "risk_level": "low",
            },
        ).json()
        client.post(f"/api/v1/memories/{memory['id']}/confirm", headers=headers)
        embedding_status = None
        for _ in range(100):
            with client.app.state.session_factory() as session:
                embedding_status = session.scalar(
                    select(Memory.embedding_status).where(Memory.id == UUID(memory["id"]))
                )
            if embedding_status == "ready":
                break
            time.sleep(0.01)

        conversation_id = create_conversation(client, headers, workspace_id, "语义追问")
        run = send_question(
            client,
            headers,
            conversation_id,
            "请遵循 bullet 清单格式",
            f"semantic-memory-{scope}",
        )
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert embedding_status == "ready"
    assert "event: memory_used" in events.text
    assert answer == "根据长期记忆：输出偏好是分点列出"


def test_conversation_memory_is_not_recalled_from_another_conversation(tmp_path) -> None:
    """验证 Conversation scope 的向量记忆不会泄漏到其他会话"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=EmptyWebSearchGateway(),
        embedding_gateway=SemanticMemoryEmbeddingGateway(),
        rerank_gateway=StableMemoryRerankGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "private-memory@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "会话记忆隔离"}
        ).json()["id"]
        source_conversation_id = create_conversation(
            client, headers, workspace_id, "私有会话"
        )
        target_conversation_id = create_conversation(
            client, headers, workspace_id, "其他会话"
        )
        memory = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "私有输出偏好是分点列出",
                "scope": "conversation",
                "category": "format",
                "risk_level": "low",
                "conversation_id": source_conversation_id,
            },
        ).json()
        client.post(f"/api/v1/memories/{memory['id']}/confirm", headers=headers)
        embedding_status = None
        for _ in range(100):
            with client.app.state.session_factory() as session:
                embedding_status = session.scalar(
                    select(Memory.embedding_status).where(Memory.id == UUID(memory["id"]))
                )
            if embedding_status == "ready":
                break
            time.sleep(0.01)

        run = send_question(
            client,
            headers,
            target_conversation_id,
            "请遵循 bullet 清单格式",
            "private-memory-isolation",
        )
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{target_conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert embedding_status == "ready"
    assert "event: memory_used" not in events.text
    assert "私有输出偏好" not in answer


def test_updating_active_memory_reindexes_new_content_before_recall(tmp_path) -> None:
    """验证 active Memory 更新后按新内容重建索引并参与语义召回"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "memory-update@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "记忆更新"}
        ).json()["id"]
        updated_memory = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "输出偏好是分点列出",
                "scope": "workspace",
                "category": "primary_layout",
                "risk_level": "low",
            },
        ).json()
        competing_memory = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "输出偏好是段落呈现",
                "scope": "workspace",
                "category": "fallback_layout",
                "risk_level": "low",
            },
        ).json()
        client.post(f"/api/v1/memories/{updated_memory['id']}/confirm", headers=headers)
        client.post(f"/api/v1/memories/{competing_memory['id']}/confirm", headers=headers)
        for _ in range(100):
            with client.app.state.session_factory() as session:
                statuses = session.scalars(
                    select(Memory.embedding_status).where(
                        Memory.id.in_(
                            {UUID(updated_memory["id"]), UUID(competing_memory["id"])}
                        )
                    )
                ).all()
            if statuses == ["ready", "ready"]:
                break
            time.sleep(0.01)

        changed = client.patch(
            f"/api/v1/memories/{updated_memory['id']}",
            headers=headers,
            json={"content": "输出偏好是表格呈现"},
        )
        for _ in range(100):
            with client.app.state.session_factory() as session:
                embedding_status = session.scalar(
                    select(Memory.embedding_status).where(
                        Memory.id == UUID(updated_memory["id"])
                    )
                )
            if embedding_status == "ready":
                break
            time.sleep(0.01)

        conversation_id = create_conversation(client, headers, workspace_id, "更新后召回")
        run = send_question(
            client,
            headers,
            conversation_id,
            "please use matrix",
            "updated-memory-recall",
        )
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert changed.status_code == 200
    assert changed.json()["content"] == "输出偏好是表格呈现"
    assert embedding_status == "ready"
    assert answer == "根据长期记忆：输出偏好是表格呈现"


def test_deactivated_workspace_memory_stops_affecting_new_conversations(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "memory@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "记忆空间"}
        ).json()["id"]
        candidate = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "报告必须使用简体中文",
                "scope": "workspace",
                "category": "constraint",
                "risk_level": "low",
            },
        )
        memory_id = candidate.json()["id"]
        confirmed = client.post(f"/api/v1/memories/{memory_id}/confirm", headers=headers)

        first_conversation = create_conversation(client, headers, workspace_id, "使用记忆")
        first_run = send_question(
            client, headers, first_conversation, "报告应该使用什么语言？", "m1"
        )
        first_events = client.get(f"/api/v1/runs/{first_run['run_id']}/events", headers=headers)
        first_messages = client.get(
            f"/api/v1/conversations/{first_conversation}/messages", headers=headers
        ).json()["items"]

        deactivated = client.post(f"/api/v1/memories/{memory_id}/deactivate", headers=headers)
        second_conversation = create_conversation(client, headers, workspace_id, "撤销记忆")
        second_run = send_question(
            client, headers, second_conversation, "报告应该使用什么语言？", "m2"
        )
        second_events = client.get(f"/api/v1/runs/{second_run['run_id']}/events", headers=headers)
        second_messages = client.get(
            f"/api/v1/conversations/{second_conversation}/messages", headers=headers
        ).json()["items"]

    assert candidate.status_code == 201
    assert candidate.json()["status"] == "candidate"
    assert confirmed.json()["status"] == "active"
    assert "event: memory_used" in first_events.text
    assert first_messages[-1]["content"] == "根据长期记忆：报告必须使用简体中文"
    assert deactivated.json()["status"] == "inactive"
    assert "event: memory_used" not in second_events.text
    assert second_messages[-1]["content"] != "根据长期记忆：报告必须使用简体中文"


def test_explicit_low_risk_preference_becomes_traceable_memory_but_secret_does_not(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "extract@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "自动记忆"}
        ).json()["id"]
        conversation_id = create_conversation(client, headers, workspace_id, "表达偏好")

        preference_run = send_question(
            client,
            headers,
            conversation_id,
            "请记住：报告默认使用简体中文",
            "extract-preference",
        )
        client.get(f"/api/v1/runs/{preference_run['run_id']}/events", headers=headers)
        send_question(
            client,
            headers,
            conversation_id,
            "请记住：API token 是 sk-do-not-store-this",
            "extract-secret",
        )
        memories = client.get(
            f"/api/v1/workspaces/{workspace_id}/memories", headers=headers
        ).json()["items"]
        detail = client.get(f"/api/v1/memories/{memories[0]['id']}", headers=headers)

    assert [(item["content"], item["status"]) for item in memories] == [
        ("报告默认使用简体中文", "active")
    ]
    assert memories[0]["source_message_id"] is not None
    assert detail.status_code == 200
    assert detail.json()["revisions"][0]["change_reason"] == "auto_activated"


def test_workspace_memory_is_not_recalled_in_another_workspace(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "isolated-memory@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_a = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "空间 A"}
        ).json()["id"]
        workspace_b = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "空间 B"}
        ).json()["id"]
        memory = client.post(
            f"/api/v1/workspaces/{workspace_a}/memories",
            headers=headers,
            json={
                "content": "报告必须使用简体中文",
                "scope": "workspace",
                "category": "constraint",
                "risk_level": "low",
            },
        ).json()
        client.post(f"/api/v1/memories/{memory['id']}/confirm", headers=headers)
        conversation_id = create_conversation(client, headers, workspace_b, "隔离验证")
        run = send_question(
            client, headers, conversation_id, "报告应该使用什么语言？", "workspace-isolation"
        )
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert "event: memory_used" not in events.text
    assert messages[-1]["content"] == "已完成对“报告应该使用什么语言？”的初步研究。"


def test_expired_memory_becomes_visible_as_expired_and_is_not_recalled(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "expired-memory@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "时效治理"}
        ).json()["id"]
        memory = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "报告必须使用简体中文",
                "scope": "workspace",
                "category": "constraint",
                "risk_level": "low",
                "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            },
        ).json()
        client.post(f"/api/v1/memories/{memory['id']}/confirm", headers=headers)

        listed = client.get(
            f"/api/v1/workspaces/{workspace_id}/memories", headers=headers
        ).json()["items"]
        conversation_id = create_conversation(client, headers, workspace_id, "过期后追问")
        run = send_question(
            client, headers, conversation_id, "报告应该使用什么语言？", "expired-memory"
        )
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)

    assert listed[0]["status"] == "expired"
    assert "event: memory_used" not in events.text


def test_conflicting_memory_requires_explicit_resolution_before_replacing_old_value(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "conflict@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "冲突治理"}
        ).json()["id"]
        old = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "统计口径只看中国大陆",
                "scope": "workspace",
                "category": "reporting_scope",
                "risk_level": "low",
            },
        ).json()
        client.post(f"/api/v1/memories/{old['id']}/confirm", headers=headers)
        new = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "统计口径包含中国大陆和港澳",
                "scope": "workspace",
                "category": "reporting_scope",
                "risk_level": "low",
            },
        )
        resolved = client.post(
            f"/api/v1/memories/{new.json()['id']}/resolve-conflict",
            headers=headers,
            json={"action": "replace"},
        )
        new_embedding_status = None
        for _ in range(100):
            with client.app.state.session_factory() as session:
                new_embedding_status = session.scalar(
                    select(Memory.embedding_status).where(Memory.id == UUID(new.json()["id"]))
                )
            if new_embedding_status == "ready":
                break
            time.sleep(0.01)
        old_after = client.get(f"/api/v1/memories/{old['id']}", headers=headers)

    assert new.status_code == 201
    assert new.json()["status"] == "conflicted"
    assert new.json()["conflict"]["old_content"] == "统计口径只看中国大陆"
    assert new.json()["conflict"]["new_content"] == "统计口径包含中国大陆和港澳"
    assert resolved.json()["status"] == "active"
    assert old_after.json()["status"] == "inactive"
    assert new_embedding_status == "ready"


def test_coexisting_conflicting_memory_indexes_new_value_and_keeps_old_active(
    tmp_path,
) -> None:
    """验证 coexist 冲突决议会索引新 Memory 且保留旧值有效"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "coexist@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "并存记忆"}
        ).json()["id"]
        old = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "输出偏好是分点列出",
                "scope": "workspace",
                "category": "layout",
                "risk_level": "low",
            },
        ).json()
        client.post(f"/api/v1/memories/{old['id']}/confirm", headers=headers)
        new = client.post(
            f"/api/v1/workspaces/{workspace_id}/memories",
            headers=headers,
            json={
                "content": "输出偏好是表格呈现",
                "scope": "workspace",
                "category": "layout",
                "risk_level": "low",
            },
        ).json()
        resolved = client.post(
            f"/api/v1/memories/{new['id']}/resolve-conflict",
            headers=headers,
            json={"action": "coexist"},
        )
        new_embedding_status = None
        for _ in range(100):
            with client.app.state.session_factory() as session:
                new_embedding_status = session.scalar(
                    select(Memory.embedding_status).where(Memory.id == UUID(new["id"]))
                )
            if new_embedding_status == "ready":
                break
            time.sleep(0.01)
        old_after = client.get(f"/api/v1/memories/{old['id']}", headers=headers)
        conversation_id = create_conversation(client, headers, workspace_id, "并存后召回")
        run = send_question(
            client,
            headers,
            conversation_id,
            "please use matrix",
            "coexisting-memory-recall",
        )
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert resolved.json()["status"] == "active"
    assert old_after.json()["status"] == "active"
    assert new_embedding_status == "ready"
    assert answer == "根据长期记忆：输出偏好是表格呈现"


def test_deleting_source_conversation_invalidates_its_unconfirmed_memory_candidate(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_deterministic_memory_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "source@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "来源失效"}
        ).json()["id"]
        conversation_id = create_conversation(client, headers, workspace_id, "敏感事实")
        send_question(
            client,
            headers,
            conversation_id,
            "请记住：我的年收入是 100 万元",
            "sensitive-candidate",
        )
        before = client.get(f"/api/v1/workspaces/{workspace_id}/memories", headers=headers).json()[
            "items"
        ]
        client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)
        after = client.get(f"/api/v1/workspaces/{workspace_id}/memories", headers=headers).json()[
            "items"
        ]

    assert before[0]["status"] == "candidate"
    assert before[0]["risk_level"] == "sensitive"
    assert after[0]["status"] == "inactive"


def create_conversation(
    client: TestClient, headers: dict[str, str], workspace_id: str, title: str
) -> str:
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/conversations",
        headers=headers,
        json={"title": title},
    ).json()["id"]


def send_question(
    client: TestClient,
    headers: dict[str, str],
    conversation_id: str,
    content: str,
    key: str,
) -> dict[str, str]:
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={**headers, "Idempotency-Key": key},
        json={"content": content},
    ).json()
