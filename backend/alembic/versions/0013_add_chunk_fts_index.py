"""add PostgreSQL full-text index for BM25 candidate retrieval

Revision ID: 0013_add_chunk_fts_index
Revises: 0012_add_knowledge_base_tags
Create Date: 2026-10-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0013_add_chunk_fts_index"
down_revision: Union[str, None] = "0012_add_knowledge_base_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_chunks_text_fts "
        "ON chunks USING gin (to_tsvector('simple', text))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_fts")
