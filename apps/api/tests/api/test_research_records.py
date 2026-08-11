import hashlib
import time

from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_search import DisabledWebSearchGateway


class ContradictSecondRunGraphRunner:
    """让第二次研究运行产生结构化冲突核验结果"""

    def __init__(self) -> None:
        """初始化确定性 Graph 委托和运行计数"""
        self._delegate = ResearchGraphRunner()
        self._run_count = 0

    def run(self, *args, **kwargs):
        """执行真实 Graph 并仅修改第二次运行的核验结果"""
        state = self._delegate.run(*args, **kwargs)
        self._run_count += 1
        if self._run_count == 2:
            state["verification"] = {
                "status": "contradicted",
                "summary": "新证据与现有研究记录冲突",
            }
        return state


def test_new_verified_result_supersedes_record_without_changing_old_evidence(
    tmp_path,
) -> None:
    """验证同一研究问题形成不可变版本链并保留旧证据 hash"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "records@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "研究记录版本"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "指标核验"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "核心指标为 10。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        document = client.post(
            f"/api/v1/attachments/{attachment['id']}/promote", headers=headers
        ).json()

        first_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-version-one"},
            json={"content": "核心指标是多少？"},
        ).json()
        client.get(f"/api/v1/runs/{first_run['run_id']}/events", headers=headers)
        old_citation = client.get(
            f"/api/v1/messages/{first_run['assistant_message_id']}/citations",
            headers=headers,
        ).json()["items"][0]

        replaced = client.post(
            f"/api/v1/documents/{document['id']}/versions",
            headers=headers,
            files={"file": ("metric.txt", "核心指标为 20。", "text/plain")},
        )
        second_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-version-two"},
            json={"content": "核心指标是多少？"},
        ).json()
        client.get(f"/api/v1/runs/{second_run['run_id']}/events", headers=headers)
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
        ).json()["items"]
        old_citation_after_replace = client.get(
            f"/api/v1/citations/{old_citation['id']}", headers=headers
        ).json()

    assert replaced.status_code == 201
    assert len(records) == 2
    old_record = next(record for record in records if "10" in record["claim_text"])
    new_record = next(record for record in records if "20" in record["claim_text"])
    assert old_record["record_key"] == new_record["record_key"]
    assert old_record["version"] == 1
    assert old_record["status"] == "superseded"
    assert new_record["version"] == 2
    assert new_record["status"] == "verified"
    old_hash = hashlib.sha256("核心指标为 10。".encode()).hexdigest()
    assert old_record["evidence"][0]["source_hash"] == old_hash
    assert old_citation_after_replace["source_hash"] == old_hash


def test_contradicted_result_keeps_both_record_versions_disputed(tmp_path) -> None:
    """验证冲突结论保留两个不可变版本并公开 disputed 状态"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        graph_runner=ContradictSecondRunGraphRunner(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "disputed@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "冲突研究记录"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "口径冲突"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "外部口径为 30。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        document = client.post(
            f"/api/v1/attachments/{attachment['id']}/promote", headers=headers
        ).json()

        first_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-disputed-one"},
            json={"content": "外部口径是多少？"},
        ).json()
        client.get(f"/api/v1/runs/{first_run['run_id']}/events", headers=headers)
        client.post(
            f"/api/v1/documents/{document['id']}/versions",
            headers=headers,
            files={"file": ("metric.txt", "外部口径为 31。", "text/plain")},
        )
        second_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-disputed-two"},
            json={"content": "外部口径是多少？"},
        ).json()
        client.get(f"/api/v1/runs/{second_run['run_id']}/events", headers=headers)
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
        ).json()["items"]

    assert len(records) == 2
    assert {record["version"] for record in records} == {1, 2}
    assert {record["status"] for record in records} == {"disputed"}
    assert {
        record["evidence"][0]["source_hash"] for record in records
    } == {
        hashlib.sha256("外部口径为 30。".encode()).hexdigest(),
        hashlib.sha256("外部口径为 31。".encode()).hexdigest(),
    }
