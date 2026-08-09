from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client


def test_skill_installation_workspace_enable_and_conversation_override_are_separate(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "skills@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "Skill 空间"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "来源比较"},
        ).json()["id"]

        catalog = client.get("/api/v1/skills/catalog", headers=headers)
        source_comparison = next(
            item for item in catalog.json()["items"] if item["slug"] == "source-comparison"
        )
        enabled_before_install = client.post(
            f"/api/v1/workspaces/{workspace_id}/skills/source-comparison/enable",
            headers=headers,
        )
        installed = client.post(
            "/api/v1/skills/source-comparison/install", headers=headers
        )
        enabled = client.post(
            f"/api/v1/workspaces/{workspace_id}/skills/source-comparison/enable",
            headers=headers,
        )
        effective = client.get(
            f"/api/v1/conversations/{conversation_id}/skills", headers=headers
        )
        overridden = client.put(
            f"/api/v1/conversations/{conversation_id}/skills/source-comparison/override",
            headers=headers,
            json={"enabled": False},
        )
        after_override = client.get(
            f"/api/v1/conversations/{conversation_id}/skills", headers=headers
        )

    assert catalog.status_code == 200
    assert source_comparison["manifest"]["executable"] is False
    assert source_comparison["manifest"]["allowed_tools"] == ["document_search"]
    assert enabled_before_install.status_code == 409
    assert installed.status_code == 201
    assert enabled.status_code == 200
    assert effective.json()["items"][0]["enabled"] is True
    assert overridden.json()["enabled"] is False
    assert after_override.json()["items"][0]["enabled"] is False


def test_source_comparison_skill_changes_research_behavior_only_when_effective(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "skill-run@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "来源比较空间"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "方案比较"},
        ).json()["id"]
        client.post("/api/v1/skills/source-comparison/install", headers=headers)
        client.post(
            f"/api/v1/workspaces/{workspace_id}/skills/source-comparison/enable",
            headers=headers,
        )

        enabled_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "skill-enabled"},
            json={"content": "比较两个方案的来源"},
        ).json()
        enabled_events = client.get(
            f"/api/v1/runs/{enabled_run['run_id']}/events", headers=headers
        )
        enabled_messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

        client.put(
            f"/api/v1/conversations/{conversation_id}/skills/source-comparison/override",
            headers=headers,
            json={"enabled": False},
        )
        disabled_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "skill-disabled"},
            json={"content": "再次比较两个方案的来源"},
        ).json()
        disabled_events = client.get(
            f"/api/v1/runs/{disabled_run['run_id']}/events", headers=headers
        )
        disabled_messages = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"]

    assert "event: skill_applied" in enabled_events.text
    assert enabled_messages[1]["content"] == "来源比较：当前没有可定位资料，无法完成多来源对照。"
    assert "event: skill_applied" not in disabled_events.text
    assert disabled_messages[-1]["content"] == "已完成对“再次比较两个方案的来源”的初步研究。"
