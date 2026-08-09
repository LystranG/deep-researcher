from datetime import UTC, datetime, timedelta

from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from fastapi.testclient import TestClient


def test_deactivated_workspace_memory_stops_affecting_new_conversations(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
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
    assert second_messages[-1]["content"] == "已完成对“报告应该使用什么语言？”的初步研究。"


def test_explicit_low_risk_preference_becomes_traceable_memory_but_secret_does_not(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
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

    with running_worker_client(settings) as client:
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

    with running_worker_client(settings) as client:
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

    with running_worker_client(settings) as client:
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
        old_after = client.get(f"/api/v1/memories/{old['id']}", headers=headers)

    assert new.status_code == 201
    assert new.json()["status"] == "conflicted"
    assert new.json()["conflict"]["old_content"] == "统计口径只看中国大陆"
    assert new.json()["conflict"]["new_content"] == "统计口径包含中国大陆和港澳"
    assert resolved.json()["status"] == "active"
    assert old_after.json()["status"] == "inactive"


def test_deleting_source_conversation_invalidates_its_unconfirmed_memory_candidate(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
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
