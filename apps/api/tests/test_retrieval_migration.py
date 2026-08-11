from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_visibility_migration_quarantines_legacy_private_retrieval_data(tmp_path) -> None:
    """验证升级时保守隔离旧分段和来源为未提升附件的研究记录"""
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    root = Path(__file__).resolve().parents[3]
    config = Config(root / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "m0n1o2p3q4")

    engine = create_engine(database_url)
    workspace_id = uuid4().hex
    conversation_id = uuid4().hex
    run_id = uuid4().hex
    message_id = uuid4().hex
    attachment_id = uuid4().hex
    chunk_id = uuid4().hex
    claim_id = uuid4().hex
    span_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO conversation_segments (
                    id, workspace_id, conversation_id, first_message_id, last_message_id,
                    ordinal, text, content_hash, embedding_status, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :conversation_id, :message_id, :message_id,
                    1, '旧分段', 'legacy-segment', 'ready', :now, :now
                )
                """
            ),
            {
                "id": uuid4().hex,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO attachments (
                    id, workspace_id, conversation_id, filename, storage_key, mime_type,
                    size_bytes, sha256, status, created_at
                ) VALUES (
                    :id, :workspace_id, :conversation_id, 'private.txt', :storage_key,
                    'text/plain', 10, 'legacy-attachment', 'ready', :now
                )
                """
            ),
            {
                "id": attachment_id,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "storage_key": f"legacy/{attachment_id}",
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO source_chunks (
                    id, workspace_id, conversation_id, attachment_id, ordinal, text,
                    start_offset, end_offset, content_hash, embedding_status
                ) VALUES (
                    :id, :workspace_id, :conversation_id, :attachment_id, 1,
                    '私有证据', 0, 4, 'private-hash', 'ready'
                )
                """
            ),
            {
                "id": chunk_id,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "attachment_id": attachment_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO research_claims (
                    id, workspace_id, run_id, claim_text, verdict, status, content_hash, created_at
                ) VALUES (
                    :id, :workspace_id, :run_id, '私有结论', 'verified', 'verified',
                    'claim-hash', :now
                )
                """
            ),
            {"id": claim_id, "workspace_id": workspace_id, "run_id": run_id, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO evidence_spans (
                    id, workspace_id, run_id, source_chunk_id, start_offset,
                    end_offset, content_hash, created_at
                ) VALUES (
                    :id, :workspace_id, :run_id, :chunk_id, 0, 4, 'private-hash', :now
                )
                """
            ),
            {
                "id": span_id,
                "workspace_id": workspace_id,
                "run_id": run_id,
                "chunk_id": chunk_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO research_claim_evidence (
                    id, workspace_id, claim_id, evidence_span_id, relation
                ) VALUES (:id, :workspace_id, :claim_id, :span_id, 'supports')
                """
            ),
            {
                "id": uuid4().hex,
                "workspace_id": workspace_id,
                "claim_id": claim_id,
                "span_id": span_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO research_records (
                    id, workspace_id, claim_id, record_key, version, claim_text, claim_kind,
                    status, content_hash, evidence_refs, embedding_status, created_at
                ) VALUES (
                    :id, :workspace_id, :claim_id, 'legacy-private', 1, '私有结论', 'fact',
                    'verified', 'record-hash', '[]', 'ready', :now
                )
                """
            ),
            {
                "id": uuid4().hex,
                "workspace_id": workspace_id,
                "claim_id": claim_id,
                "now": now,
            },
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        segment_scope = connection.scalar(
            text("SELECT visibility_scope FROM conversation_segments LIMIT 1")
        )
        record_deleted_at = connection.scalar(
            text("SELECT deleted_at FROM research_records WHERE record_key = 'legacy-private'")
        )

    assert segment_scope == "conversation"
    assert record_deleted_at is not None
