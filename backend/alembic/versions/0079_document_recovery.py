"""Persist upload replay keys and document processing recovery state."""
from alembic import op
import sqlalchemy as sa

revision = "0079_document_recovery"
down_revision = "0078_tool_detail"
branch_labels = depends_on = None


def upgrade():
    op.add_column("document_uploads", sa.Column("request_key", sa.Uuid()))
    op.add_column("document_uploads", sa.Column("failure", sa.JSON()))
    op.add_column("document_uploads", sa.Column("verify_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("document_uploads", sa.Column("verify_wait_ms", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("document_uploads", sa.Column("verify_after", sa.DateTime(timezone=True)))
    op.create_unique_constraint("uq_document_upload_request", "document_uploads",
                                ["workspace_id", "user_id", "knowledge_base_id", "request_key"])
    for name, kind in (("failure", sa.JSON()), ("phase", sa.String(16)), ("retry_of", sa.Uuid()),
                       ("metering_id", sa.Uuid())):
        op.add_column("ingestion_jobs", sa.Column(name, kind))
    op.add_column("ingestion_jobs", sa.Column("submission_pending", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.add_column("ingestion_jobs", sa.Column("embedding_submitting", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))
    op.add_column("ingestion_jobs", sa.Column("retry_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ingestion_jobs", sa.Column("retry_after", sa.DateTime(timezone=True)))
    op.create_unique_constraint("uq_ingestion_job_retry_of", "ingestion_jobs", ["retry_of"])
    op.execute(
        "UPDATE ingestion_jobs AS j SET phase = 'indexing' FROM documents AS d "
        "WHERE d.id = j.document_id AND d.parsed_text IS NOT NULL "
        "AND j.state IN ('QUEUED', 'RUNNING', 'FAILED', 'CANCELLED') "
        "AND EXISTS (SELECT 1 FROM chunks AS c WHERE c.document_id = d.id)"
    )
    # Existing ledger events and pricing snapshots already use the original job ID.
    op.execute(
        "UPDATE ingestion_jobs AS j SET metering_id = COALESCE(("
        "SELECT p.id FROM ingestion_jobs AS p JOIN usage_events AS u ON u.request_id = p.id "
        "WHERE p.document_id = j.document_id AND u.capability = 'ocr_pages' "
        "ORDER BY u.created_at LIMIT 1), j.id) "
        "WHERE j.phase IS DISTINCT FROM 'indexing' AND j.state IN ('QUEUED', 'RUNNING', 'FAILED', 'CANCELLED')"
    )
    op.execute(
        "UPDATE ingestion_jobs AS j SET embedding_submitting = TRUE "
        "WHERE j.phase = 'indexing' AND j.state = 'RUNNING'"
    )
    op.execute(
        "UPDATE ingestion_jobs SET submission_pending = TRUE WHERE state = 'QUEUED'"
    )


def downgrade():
    op.drop_constraint("uq_ingestion_job_retry_of", "ingestion_jobs", type_="unique")
    for name in ("retry_after", "retry_attempts", "embedding_submitting", "submission_pending", "metering_id", "retry_of", "phase", "failure"):
        op.drop_column("ingestion_jobs", name)
    op.drop_constraint("uq_document_upload_request", "document_uploads", type_="unique")
    for name in ("verify_after", "verify_wait_ms", "verify_attempts", "failure", "request_key"):
        op.drop_column("document_uploads", name)
