import hashlib
import time

from deep_researcher.graph import ResearchGraphRunner
from deep_researcher.model_gateway import ExtractiveModelGateway
from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from deep_researcher.web_page import WebAcquisition
from deep_researcher.web_search import DisabledWebSearchGateway, SearchResult


class DeterministicEmbeddingGateway:
    """为 ResearchRecord 索引测试生成稳定向量"""

    model_name = "test-record-embedding-v1"

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        """按输入文本生成固定维度向量"""
        return [(1.0, float(len(text))) for text in texts]

    def embed_query(self, text: str) -> tuple[float, ...]:
        """为查询生成同维度向量"""
        return (1.0, float(len(text)))


class StableRerankGateway:
    """按候选顺序返回稳定精排分数"""

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """返回预算内的候选下标"""
        del query
        return [(index, 1.0 - index / 100) for index in range(min(len(documents), top_n))]


class ResearchRecordFirstRerankGateway:
    """优先返回 ResearchRecord 格式的候选"""

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> list[tuple[int, float]]:
        """按研究记录格式和原始顺序返回候选下标"""
        del query
        ranked = sorted(
            range(len(documents)),
            key=lambda index: (not documents[index].startswith("根据资料："), index),
        )
        return [(index, 1.0 - rank / 100) for rank, index in enumerate(ranked[:top_n])]


class FailingResearchRecordEmbeddingGateway(DeterministicEmbeddingGateway):
    """让来源文档成功索引后模拟后续 embedding Provider 故障"""

    def __init__(self) -> None:
        """初始化调用计数"""
        self._calls = 0

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        """首次索引来源成功，后续调用模拟 Provider 故障"""
        self._calls += 1
        if self._calls > 1:
            raise RuntimeError("provider unavailable")
        return super().embed_documents(texts)


class ResearchRecordWebSearchGateway:
    """仅为第一轮研究返回可核验网页来源"""

    def search(self, query: str, *, count: int = 5) -> list[SearchResult]:
        """第一轮返回发现结果，后续语义查询不再提供外部结果"""
        del count
        if query != "首次调查天穹协议的认证编号":
            return []
        return [
            {
                "title": "天穹协议公告",
                "url": "https://example.com/sky-protocol",
                "snippet": "认证编号已发布",
            }
        ]


class ResearchRecordWebPageGateway:
    """为第一轮搜索结果提供不可变网页正文"""

    def fetch(self, url: str) -> dict[str, object] | None:
        """返回天穹协议公告的完整证据"""
        if url != "https://example.com/sky-protocol":
            return None
        return {
            "title": "天穹协议公告",
            "content": "天穹协议的认证编号为 ORBIT-7319。",
            "truncated": False,
        }


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


class InsufficientGraphRunner:
    """让研究运行保留引用但返回证据不足的核验结果"""

    def __init__(self) -> None:
        """初始化确定性 Graph 委托"""
        self._delegate = ResearchGraphRunner()

    def run(self, *args, **kwargs):
        """执行真实 Graph 并将核验结果改为证据不足"""
        state = self._delegate.run(*args, **kwargs)
        state["verification"] = {
            "status": "insufficient",
            "summary": "现有证据不足以核验结论",
        }
        return state


def test_verified_research_record_becomes_searchable_after_run_commit(tmp_path) -> None:
    """验证已核验研究记录在运行提交后完成异步索引"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "record-index@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "研究记录索引"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "指标核验"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "检索指标为 INDEX-73。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/attachments/{attachment['id']}/promote", headers=headers)

        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-index-ready"},
            json={"content": "检索指标是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        record = None
        for _ in range(500):
            items = client.get(
                f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
            ).json()["items"]
            if items:
                record = items[0]
                if record.get("embedding_status") != "pending":
                    break
            time.sleep(0.01)

    assert record is not None
    assert record.get("embedding_status") == "ready"
    assert record.get("embedding_model") == "test-record-embedding-v1"


def test_insufficient_result_with_citation_is_not_promoted(tmp_path) -> None:
    """验证证据不足的回答即使带引用也不会成为空间研究记录"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        graph_runner=InsufficientGraphRunner(),
        embedding_gateway=DeterministicEmbeddingGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "insufficient@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "证据不足研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "不足核验"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "暂定指标为 MAYBE-17。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/attachments/{attachment['id']}/promote", headers=headers)

        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "insufficient-record"},
            json={"content": "暂定指标是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]
        detail = client.get(
            f"/api/v1/conversations/{conversation_id}/latest-run", headers=headers
        ).json()
        ledger = client.get(
            f"/api/v1/runs/{run['run_id']}/ledger", headers=headers
        ).json()
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
        ).json()["items"]

    assert citations
    assert detail["status"] == "partial"
    assert ledger["coverage"] == {
        "citation_count": 1,
        "verified_claim_count": 0,
        "complete": False,
    }
    assert ledger["gaps"] == [
        {
            "id": ledger["gaps"][0]["id"],
            "description": "Verifier 判定现有证据不足以核验结论",
            "status": "open",
        }
    ]
    assert ledger["stop_decision"] == {
        "reason": "verifier_insufficient",
        "completeness": "partial",
    }
    assert records == []


def test_research_record_index_failure_preserves_verified_evidence(tmp_path) -> None:
    """验证索引失败会显式记录且不丢失已核验主张与证据"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        embedding_gateway=FailingResearchRecordEmbeddingGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "record-failure@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "研究记录失败"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "故障核验"},
        ).json()["id"]
        evidence = "故障指标为 FAIL-91。"
        attachment = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("failure.txt", evidence, "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{attachment['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/attachments/{attachment['id']}/promote", headers=headers)

        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-index-failed"},
            json={"content": "故障指标是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        record = None
        for _ in range(50):
            items = client.get(
                f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
            ).json()["items"]
            if items:
                record = items[0]
                if record["embedding_status"] != "pending":
                    break
            time.sleep(0.01)

    assert record is not None
    assert record["embedding_status"] == "failed"
    assert record["embedding_error"] == "ResearchRecord embedding 失败"
    assert evidence in record["claim_text"]
    assert record["evidence"][0]["source_hash"] == hashlib.sha256(evidence.encode()).hexdigest()


def test_research_record_recall_in_another_conversation_cites_original_web_span(
    tmp_path,
) -> None:
    """验证跨会话研究记录召回仍引用原始网页证据而非记录自身"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    evidence = "天穹协议的认证编号为 ORBIT-7319。"

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=ResearchRecordWebSearchGateway(),
        web_page_gateway=WebAcquisition(
            jina_reader=ResearchRecordWebPageGateway(),
            local_reader=None,
            url_validator=lambda _: None,
        ),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=StableRerankGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "record-recall@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "研究记录复用"}
        ).json()["id"]
        source_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "首次核验"},
        ).json()["id"]
        reuse_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "跨会话复用"},
        ).json()["id"]

        first_run = client.post(
            f"/api/v1/conversations/{source_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-source-web"},
            json={"content": "首次调查天穹协议的认证编号"},
        ).json()
        client.get(f"/api/v1/runs/{first_run['run_id']}/events", headers=headers)
        first_citation = client.get(
            f"/api/v1/messages/{first_run['assistant_message_id']}/citations",
            headers=headers,
        ).json()["items"][0]
        for _ in range(50):
            records = client.get(
                f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
            ).json()["items"]
            if records and records[0]["embedding_status"] == "ready":
                break
            time.sleep(0.01)

        second_run = client.post(
            f"/api/v1/conversations/{reuse_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-reuse-semantic"},
            json={"content": "请复述此前核验的协议凭证代号"},
        ).json()
        client.get(f"/api/v1/runs/{second_run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{reuse_conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]
        second_citations = client.get(
            f"/api/v1/messages/{second_run['assistant_message_id']}/citations",
            headers=headers,
        ).json()["items"]

    assert "ORBIT-7319" in answer
    assert second_citations[0]["source_type"] == "web"
    assert second_citations[0]["evidence_text"] == evidence
    assert second_citations[0]["source_hash"] == first_citation["source_hash"]


def test_research_record_from_replaced_document_version_cannot_be_cited(tmp_path) -> None:
    """验证旧文档版本产生的研究记录不能在后续会话继续生成引用"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(
        settings,
        model_gateway=ExtractiveModelGateway(),
        web_search_gateway=DisabledWebSearchGateway(),
        embedding_gateway=DeterministicEmbeddingGateway(),
        rerank_gateway=ResearchRecordFirstRerankGateway(),
    ) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "stale-record@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "旧版本研究记录"}
        ).json()["id"]
        source_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "旧版来源"},
        ).json()["id"]
        reuse_conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "跨会话复用"},
        ).json()["id"]
        attachment = client.post(
            f"/api/v1/conversations/{source_conversation_id}/attachments",
            headers=headers,
            files={"file": ("version.txt", "旧版本编号为 LEGACY-41。", "text/plain")},
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
            f"/api/v1/conversations/{source_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "stale-record-source"},
            json={"content": "LEGACY-41"},
        ).json()
        client.get(f"/api/v1/runs/{first_run['run_id']}/events", headers=headers)
        for _ in range(50):
            records = client.get(
                f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
            ).json()["items"]
            if records and records[0]["embedding_status"] == "ready":
                break
            time.sleep(0.01)

        client.post(
            f"/api/v1/documents/{document['id']}/versions",
            headers=headers,
            files={"file": ("version.txt", "当前版本编号为 CURRENT-99。", "text/plain")},
        )
        fallback_attachment = client.post(
            f"/api/v1/conversations/{reuse_conversation_id}/attachments",
            headers=headers,
            files={"file": ("fallback.txt", "当前有效编号为 CURRENT-99。", "text/plain")},
        ).json()
        for _ in range(50):
            fallback_attachment = client.get(
                f"/api/v1/attachments/{fallback_attachment['id']}", headers=headers
            ).json()
            if fallback_attachment["status"] != "processing":
                break
            time.sleep(0.01)
        second_run = client.post(
            f"/api/v1/conversations/{reuse_conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "stale-record-reuse"},
            json={"content": "LEGACY-41"},
        ).json()
        client.get(f"/api/v1/runs/{second_run['run_id']}/events", headers=headers)
        citations = client.get(
            f"/api/v1/messages/{second_run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert len(citations) == 1
    assert citations[0]["source_type"] == "conversation_attachment"
    assert citations[0]["evidence_text"] == "当前有效编号为 CURRENT-99。"


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
        embedding_gateway=DeterministicEmbeddingGateway(),
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

        client.post(
            f"/api/v1/documents/{document['id']}/versions",
            headers=headers,
            files={"file": ("metric.txt", "核心指标为 10。", "text/plain")},
        )
        third_run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "record-version-three"},
            json={"content": "核心指标是多少？"},
        ).json()
        client.get(f"/api/v1/runs/{third_run['run_id']}/events", headers=headers)
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/research-records", headers=headers
        ).json()["items"]
        old_citation_after_replace = client.get(
            f"/api/v1/citations/{old_citation['id']}", headers=headers
        ).json()

    assert replaced.status_code == 201
    assert len(records) == 3
    old_record = next(record for record in records if record["version"] == 1)
    changed_record = next(record for record in records if record["version"] == 2)
    repeated_record = next(record for record in records if record["version"] == 3)
    assert {record["record_key"] for record in records} == {old_record["record_key"]}
    assert old_record["version"] == 1
    assert old_record["status"] == "superseded"
    assert "20" in changed_record["claim_text"]
    assert changed_record["status"] == "superseded"
    assert "10" in repeated_record["claim_text"]
    assert repeated_record["status"] == "verified"
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
        embedding_gateway=DeterministicEmbeddingGateway(),
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
