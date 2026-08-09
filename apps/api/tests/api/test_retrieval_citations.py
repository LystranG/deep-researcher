import time
from io import BytesIO

from deep_researcher.settings import Settings
from deep_researcher.testing import running_worker_client
from reportlab.pdfgen import canvas


class UnknownCitationGateway:
    """模拟模型输出冻结来源之外的引用编号"""

    def stream_answer(self, context):
        """返回一个无法由当前 SourceSet 支持的回答"""
        yield "未经证据支持的结论 [2]"


def test_unknown_citation_falls_back_to_frozen_evidence(tmp_path) -> None:
    """验证未知引用不会进入最终消息和 Citation"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings, model_gateway=UnknownCitationGateway()) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "引用校验"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "冻结来源"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("metric.txt", "可验证指标为 42。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(
                f"/api/v1/attachments/{uploaded['id']}", headers=headers
            ).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)

        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "unknown-citation"},
            json={"content": "可验证指标是多少？"},
        ).json()
        events = client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversation_id}/messages", headers=headers
        ).json()["items"][-1]["content"]
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert answer == "根据资料：可验证指标为 42。 [1]"
    assert "[2]" not in events.text
    assert [citation["label"] for citation in citations] == [1]


def test_promoting_attachment_changes_cross_conversation_retrieval_and_adds_citation(
    tmp_path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "口径研究"}
        ).json()["id"]
        conversation_a = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料上传"},
        ).json()["id"]
        conversation_b = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "资料问答"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_a}/attachments",
            headers=headers,
            files={"file": ("scope.txt", "秘密统计口径为 Alpha-42。", "text/plain")},
        ).json()
        for _ in range(50):
            attachment = client.get(f"/api/v1/attachments/{uploaded['id']}", headers=headers).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)

        private_run = client.post(
            f"/api/v1/conversations/{conversation_b}/messages",
            headers={**headers, "Idempotency-Key": "private-query"},
            json={"content": "秘密统计口径是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{private_run['run_id']}/events", headers=headers)
        private_messages = client.get(
            f"/api/v1/conversations/{conversation_b}/messages", headers=headers
        ).json()["items"]

        client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)
        shared_run = client.post(
            f"/api/v1/conversations/{conversation_b}/messages",
            headers={**headers, "Idempotency-Key": "shared-query"},
            json={"content": "秘密统计口径是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{shared_run['run_id']}/events", headers=headers)
        shared_messages = client.get(
            f"/api/v1/conversations/{conversation_b}/messages", headers=headers
        ).json()["items"]
        citation_list = client.get(
            f"/api/v1/messages/{shared_run['assistant_message_id']}/citations",
            headers=headers,
        )
        citation = client.get(
            f"/api/v1/citations/{citation_list.json()['items'][0]['id']}", headers=headers
        )

    assert "Alpha-42" not in private_messages[-1]["content"]
    assert "Alpha-42" in shared_messages[-1]["content"]
    assert shared_messages[-1]["content"].endswith("[1]")
    assert citation_list.status_code == 200
    assert citation.json()["filename"] == "scope.txt"
    assert citation.json()["document_version"] == 1
    assert citation.json()["evidence_text"] == "秘密统计口径为 Alpha-42。"


def test_same_named_documents_never_cross_workspace_boundary(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )

    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        conversations: list[str] = []
        for workspace_name, content in [
            ("空间 A", "唯一隔离标记为 A-ONLY。"),
            ("空间 B", "唯一隔离标记为 B-SECRET。"),
        ]:
            workspace_id = client.post(
                "/api/v1/workspaces", headers=headers, json={"name": workspace_name}
            ).json()["id"]
            conversation_id = client.post(
                f"/api/v1/workspaces/{workspace_id}/conversations",
                headers=headers,
                json={"title": "隔离验证"},
            ).json()["id"]
            conversations.append(conversation_id)
            uploaded = client.post(
                f"/api/v1/conversations/{conversation_id}/attachments",
                headers=headers,
                files={"file": ("same-name.txt", content, "text/plain")},
            ).json()
            for _ in range(50):
                attachment = client.get(
                    f"/api/v1/attachments/{uploaded['id']}", headers=headers
                ).json()
                if attachment["status"] != "processing":
                    break
                time.sleep(0.01)
            client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)

        run = client.post(
            f"/api/v1/conversations/{conversations[0]}/messages",
            headers={**headers, "Idempotency-Key": "workspace-a-query"},
            json={"content": "唯一隔离标记是什么？"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        answer = client.get(
            f"/api/v1/conversations/{conversations[0]}/messages", headers=headers
        ).json()["items"][-1]["content"]

    assert "A-ONLY" in answer
    assert "B-SECRET" not in answer


def test_pdf_citation_opens_exact_page_and_evidence_span(tmp_path) -> None:
    pdf = BytesIO()
    writer = canvas.Canvas(pdf)
    writer.drawString(72, 720, "The 2025 market size was 30 billion dollars.")
    writer.showPage()
    writer.drawString(72, 720, "This is unrelated second-page material.")
    writer.save()

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        object_store_root=tmp_path / "objects",
    )
    with running_worker_client(settings) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "researcher@example.com", "password": "correct horse battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        workspace_id = client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "PDF 研究"}
        ).json()["id"]
        conversation_id = client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations",
            headers=headers,
            json={"title": "PDF 引用"},
        ).json()["id"]
        uploaded = client.post(
            f"/api/v1/conversations/{conversation_id}/attachments",
            headers=headers,
            files={"file": ("market.pdf", pdf.getvalue(), "application/pdf")},
        ).json()
        for _ in range(50):
            attachment = client.get(f"/api/v1/attachments/{uploaded['id']}", headers=headers).json()
            if attachment["status"] != "processing":
                break
            time.sleep(0.01)
        client.post(f"/api/v1/attachments/{uploaded['id']}/promote", headers=headers)
        run = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={**headers, "Idempotency-Key": "pdf-query"},
            json={"content": "What was the 2025 market size?"},
        ).json()
        client.get(f"/api/v1/runs/{run['run_id']}/events", headers=headers)
        citations = client.get(
            f"/api/v1/messages/{run['assistant_message_id']}/citations", headers=headers
        ).json()["items"]

    assert citations[0]["filename"] == "market.pdf"
    assert citations[0]["page_number"] == 1
    assert "30 billion dollars" in citations[0]["evidence_text"]
